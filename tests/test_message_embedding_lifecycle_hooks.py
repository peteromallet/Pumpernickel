from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.embeddings import canonical_content_hash
from app.services import hooks
from app.services.inbound import process_inbound
from app.services.messaging import send_outbound
from app.services.scope import InboundScope
from app.services.tools import read_tools
from app.services.tools import write_tools
from app.services.turn_context import TurnContext
from tool_schemas import (
    DeleteOutboundMessageInput,
    EditOutboundMessageInput,
    ExplainMediaItemInput,
    SearchMessagesInput,
)


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def reset_hooks():
    hooks.check_oob = None


def _forbid_provider_calls(monkeypatch) -> None:
    async def fail_embedder_use(*args, **kwargs):
        raise AssertionError("write paths must not call embedding providers")

    monkeypatch.setattr("app.services.embeddings.embedder_from_settings", fail_embedder_use)
    monkeypatch.setattr("app.services.embeddings.OpenAIEmbedder.embed_texts", fail_embedder_use)
    monkeypatch.setattr("app.services.embeddings.LocalBgeSmallEmbedder.embed_texts", fail_embedder_use)
    monkeypatch.setattr("app.services.embeddings.DeterministicFakeEmbedder.embed_texts", fail_embedder_use)


def _user(fake_pool) -> User:
    row = {"id": uuid4(), "name": "Maya", "phone": "15555550100", "timezone": "UTC"}
    fake_pool.users[row["id"]] = row
    return User(**row)


def _scope(user: User) -> InboundScope:
    return InboundScope(
        bot_id="mediator",
        transport="discord",
        user_id=user.id,
        topic_id=uuid4(),
        channel_id=None,
        binding_id=uuid4(),
        dyad_id=uuid4(),
    )


def _turn_ctx(fake_pool, user: User, partner: User) -> TurnContext:
    return TurnContext(
        turn_id=uuid4(),
        pool=fake_pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        turn_started_at=datetime.now(UTC),
        current_step="respond",
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=uuid4(),
        dyad_id=uuid4(),
    )


def _partner(fake_pool) -> User:
    row = {"id": uuid4(), "name": "Noor", "phone": "15555550101", "timezone": "UTC"}
    fake_pool.users[row["id"]] = row
    return User(**row)


def _outbound_row(fake_pool, *, user: User, content: str = "original text") -> object:
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": user.id,
        "content": content,
        "content_encrypted": None,
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": "discord-message-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
        "bot_id": "mediator",
        "topic_id": None,
    }
    return message_id


def _payload(sender: str, wa_id: str, content: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": "Maya"}}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": wa_id,
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "text",
                                    "text": {"body": content},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def _edit_payload(sender: str, target_wa_id: str, content: str) -> dict:
    payload = _payload(sender, f"edit.{target_wa_id}", content)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["context"] = {"message_id": target_wa_id}
    return payload


def _delete_payload(sender: str, target_wa_id: str) -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": sender, "profile": {"name": "Maya"}}],
                            "errors": [{"code": 131051, "message_id": target_wa_id}],
                            "messages": [
                                {
                                    "from": sender,
                                    "id": f"delete.{target_wa_id}",
                                    "timestamp": str(int(datetime.now(UTC).timestamp())),
                                    "type": "unsupported",
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def test_inbound_insert_edit_and_delete_enqueue_lifecycle_jobs(fake_pool, monkeypatch, app_env) -> None:
    calls: list[tuple[str, object, str | None, str | None]] = []

    async def record_embed_job(pool, *, message_id, content_hash, model, dimension):
        calls.append(("embed", message_id, content_hash, None))

    async def record_reembed_job(pool, *, message_id, content_hash, model, dimension):
        calls.append(("reembed", message_id, content_hash, None))

    async def record_drop_job(pool, *, message_id):
        calls.append(("drop", message_id, None, None))

    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_embed_job", record_embed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_drop_embedding_job", record_drop_job)
    _forbid_provider_calls(monkeypatch)

    async def classify_charge(pool, content):
        return type("Charge", (), {"charge": "routine"})()

    monkeypatch.setattr("app.services.inbound.classify_charge", classify_charge)

    first = await process_inbound(
        fake_pool,
        _payload("15555550100", "wamid.lifecycle", "first text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    duplicate = await process_inbound(
        fake_pool,
        _payload("15555550100", "wamid.lifecycle", "first text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _edit_payload("15555550100", "wamid.lifecycle", "first text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _edit_payload("15555550100", "wamid.lifecycle", "changed text"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _delete_payload("15555550100", "wamid.lifecycle"),
        transport="whatsapp",
        bot_id="mediator",
    )

    message_id = next(iter(fake_pool.messages))
    first_hash = canonical_content_hash("first text")
    changed_hash = canonical_content_hash("changed text")
    assert first.inserted == 1
    assert duplicate.skipped_existing == 1
    assert calls == [
        ("embed", message_id, first_hash, None),
        ("reembed", message_id, changed_hash, None),
        ("drop", message_id, None, None),
    ]


async def test_send_outbound_preserves_oob_return_shape_and_enqueues_after_row_creation(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    inbound_id = uuid4()
    fake_pool.messages[inbound_id] = {
        "id": inbound_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "hi",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC) - timedelta(minutes=5),
        "charge": None,
        "whatsapp_message_id": "inbound",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }
    calls: list[tuple[object, str | None, str | None]] = []

    async def record_embed_job(pool, *, message_id, content_hash, model, dimension):
        calls.append((message_id, content_hash, None))

    async def block_oob(*args, **kwargs):
        return {
            "verdict": "block",
            "reason": "private",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_embed_job", record_embed_job)
    _forbid_provider_calls(monkeypatch)
    hooks.check_oob = block_oob

    result = await send_outbound(fake_pool, user, "blocked text", scope=_scope(user))

    assert result == {
        "status": "blocked",
        "message_id": calls[0][0],
        "visible_to_user": False,
        "provider_message_id": None,
    }
    assert calls == [(result["message_id"], canonical_content_hash("blocked text"), None)]
    assert fake_pool.messages[result["message_id"]]["processing_state"] == "withheld"


async def test_tool_outbound_edit_delete_and_media_explanation_enqueue_lifecycle_jobs(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    ctx = _turn_ctx(fake_pool, user, partner)
    ctx.primary_topic_id = uuid4()
    message_id = _outbound_row(fake_pool, user=user)
    fake_pool.messages[message_id]["topic_id"] = ctx.primary_topic_id

    calls: list[tuple[str, object, str | None]] = []

    async def record_reembed_job(pool, *, message_id, content_hash, model, dimension):
        calls.append(("reembed", message_id, content_hash))

    async def record_drop_job(pool, *, message_id):
        calls.append(("drop", message_id, None))

    async def fake_edit_text(*args, **kwargs):
        return None

    async def fake_delete_text(*args, **kwargs):
        return None

    async def fake_explain_stored_image(pool, message_id):
        return {"explanation": "A diagram showing the changed plan."}

    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_drop_embedding_job", record_drop_job)
    monkeypatch.setattr("app.services.tools.write_tools.discord.edit_text", fake_edit_text)
    monkeypatch.setattr("app.services.tools.write_tools.discord.delete_text", fake_delete_text)
    monkeypatch.setattr("app.services.tools.write_tools.explain_stored_image", fake_explain_stored_image)
    _forbid_provider_calls(monkeypatch)

    edited = await write_tools.edit_outbound_message(
        ctx,
        EditOutboundMessageInput(
            message_id=str(message_id),
            content="edited text",
            reason="fix typo",
        ),
    )
    fake_pool.messages[message_id]["media_type"] = "image"
    fake_pool.messages[message_id]["media_url"] = "s3://bucket/image.png"
    explained = await write_tools.explain_media_item(
        ctx,
        ExplainMediaItemInput(message_id=str(message_id), reason="needs durable explanation"),
    )
    deleted = await write_tools.delete_outbound_message(
        ctx,
        DeleteOutboundMessageInput(message_id=str(message_id), reason="cleanup"),
    )

    assert edited.action == "edited"
    assert explained.action == "explained"
    assert deleted.action == "deleted"
    assert calls == [
        ("reembed", message_id, canonical_content_hash("edited text")),
        (
            "reembed",
            message_id,
            canonical_content_hash(
                "edited text",
                {"explanation": "A diagram showing the changed plan."},
            ),
        ),
        ("drop", message_id, None),
    ]


async def test_cross_path_enqueue_and_search_suppression_contract(
    fake_pool,
    monkeypatch,
    app_env,
) -> None:
    user = _user(fake_pool)
    partner = _partner(fake_pool)
    topic_id = uuid4()
    ctx = _turn_ctx(fake_pool, user, partner)
    ctx.primary_topic_id = topic_id
    calls: list[tuple[str, object, str | None]] = []

    async def record_embed_job(pool, *, message_id, content_hash, model, dimension):
        calls.append(("embed", message_id, content_hash))

    async def record_reembed_job(pool, *, message_id, content_hash, model, dimension):
        calls.append(("reembed", message_id, content_hash))

    async def record_drop_job(pool, *, message_id):
        calls.append(("drop", message_id, None))

    async def classify_charge(pool, content):
        return type("Charge", (), {"charge": "routine"})()

    async def block_oob(*args, **kwargs):
        return {
            "verdict": "block",
            "reason": "private",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    async def fake_edit_text(*args, **kwargs):
        return None

    async def fake_delete_text(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.inbound.classify_charge", classify_charge)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_embed_job", record_embed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_reembed_job", record_reembed_job)
    monkeypatch.setattr("app.services.message_embedding_lifecycle.enqueue_drop_embedding_job", record_drop_job)
    monkeypatch.setattr("app.services.tools.write_tools.discord.edit_text", fake_edit_text)
    monkeypatch.setattr("app.services.tools.write_tools.discord.delete_text", fake_delete_text)
    _forbid_provider_calls(monkeypatch)

    inbound = await process_inbound(
        fake_pool,
        _payload("15555550100", "wamid.cross-path", "inbound stable needle"),
        transport="whatsapp",
        bot_id="mediator",
    )
    inbound_message_id = next(
        message_id
        for message_id, row in fake_pool.messages.items()
        if row.get("whatsapp_message_id") == "wamid.cross-path"
    )
    await process_inbound(
        fake_pool,
        _edit_payload("15555550100", "wamid.cross-path", "inbound edited needle"),
        transport="whatsapp",
        bot_id="mediator",
    )
    await process_inbound(
        fake_pool,
        _delete_payload("15555550100", "wamid.cross-path"),
        transport="whatsapp",
        bot_id="mediator",
    )

    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    hooks.check_oob = block_oob
    outbound = await send_outbound(fake_pool, user, "blocked outbound needle", scope=_scope(user))
    hooks.check_oob = None

    visible_tool_message_id = _outbound_row(fake_pool, user=user, content="visible tool needle")
    suppressed_tool_message_id = _outbound_row(fake_pool, user=user, content="suppressed tool needle")
    for message_id in (visible_tool_message_id, suppressed_tool_message_id):
        fake_pool.messages[message_id]["topic_id"] = topic_id
    await write_tools.edit_outbound_message(
        ctx,
        EditOutboundMessageInput(
            message_id=str(visible_tool_message_id),
            content="visible tool edited needle",
            reason="integration edit",
        ),
    )
    fake_pool.messages[suppressed_tool_message_id]["search_suppressed_at"] = datetime.now(UTC)

    first_search = await read_tools.search_messages(
        ctx, SearchMessagesInput(text_contains="tool", limit=10)
    )
    second_search = await read_tools.search_messages(
        ctx, SearchMessagesInput(text_contains="tool", limit=10)
    )
    await write_tools.delete_outbound_message(
        ctx,
        DeleteOutboundMessageInput(
            message_id=str(visible_tool_message_id),
            reason="integration delete",
        ),
    )

    assert inbound.inserted == 1
    assert outbound["status"] == "blocked"
    assert [hit.id for hit in first_search.hits] == [visible_tool_message_id]
    assert [hit.id for hit in second_search.hits] == [visible_tool_message_id]
    assert suppressed_tool_message_id not in [hit.id for hit in second_search.hits]
    assert calls == [
        ("embed", inbound_message_id, canonical_content_hash("inbound stable needle")),
        ("reembed", inbound_message_id, canonical_content_hash("inbound edited needle")),
        ("drop", inbound_message_id, None),
        ("embed", outbound["message_id"], canonical_content_hash("blocked outbound needle")),
        ("reembed", visible_tool_message_id, canonical_content_hash("visible tool edited needle")),
        ("drop", visible_tool_message_id, None),
    ]
