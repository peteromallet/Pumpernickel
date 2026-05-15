import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import NamedTuple
from uuid import uuid4

import pytest

from app.bots.registry import BOT_SPECS
from app.bots.registry import get_relationship_topic_id
from app.bots.tante_rosi import build_tante_rosi_spec
from app.models.user import User
from app.services import agentic
from app.services.inbound import process_inbound


pytestmark = pytest.mark.anyio


class _Charge(NamedTuple):
    charge: str


class _Coalescer:
    def __init__(self) -> None:
        self.calls = []

    async def add(self, user_id, message_id, user, *, source: str = "live", scope) -> None:
        self.calls.append((user_id, message_id, user, source, scope))


def _payload(message_id: str = "wamid.source") -> dict:
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "15555550100", "profile": {"name": "Maya"}}],
                            "messages": [
                                {
                                    "from": "15555550100",
                                    "id": message_id,
                                    "timestamp": str(int(time.time())),
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


async def test_process_inbound_defaults_coalescer_source_to_live(fake_pool, monkeypatch) -> None:
    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(fake_pool, _payload(), coalescer, transport="whatsapp", bot_id="mediator")

    assert len(coalescer.calls) == 1
    assert coalescer.calls[0][3] == "live"


async def test_process_inbound_forwards_explicit_coalescer_source(fake_pool, monkeypatch) -> None:
    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(
        fake_pool, _payload("wamid.catchup"), coalescer,
        transport="whatsapp", bot_id="mediator", coalescer_source="catch_up",
    )

    assert len(coalescer.calls) == 1
    assert coalescer.calls[0][3] == "catch_up"


async def test_process_inbound_requires_bot_id_and_transport(fake_pool) -> None:
    """Both transport and bot_id are required keyword-only — no silent defaults.

    Regression guard: the old code silently routed unknown senders to bot_id
    "mediator", which caused Tante Rosi inbound DMs to be answered by Véas.
    """
    with pytest.raises(TypeError):
        await process_inbound(fake_pool, _payload(), None)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        await process_inbound(fake_pool, _payload(), None, transport="whatsapp")  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        await process_inbound(fake_pool, _payload(), None, bot_id="mediator")  # type: ignore[call-arg]


async def test_discord_inbound_writes_correct_bot_id(fake_pool, monkeypatch) -> None:
    """When the Discord gateway threads its own bot_id, the inbound row gets tagged
    with THAT bot_id — not the silent "mediator" default."""

    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    coalescer = _Coalescer()

    await process_inbound(
        fake_pool, _payload("wamid.rosi"), coalescer,
        transport="discord", bot_id="tante_rosi",
    )

    rows = [m for m in fake_pool.messages.values() if m["whatsapp_message_id"] == "wamid.rosi"]
    assert len(rows) == 1
    assert rows[0]["bot_id"] == "tante_rosi", (
        f"expected bot_id='tante_rosi', got {rows[0]['bot_id']!r} — "
        "regression of the silent-mediator-default bug"
    )


async def test_process_inbound_builds_scope_after_user_identity_and_preserves_channel(fake_pool, monkeypatch) -> None:
    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    payload = _payload("discord.chan")
    payload["entry"][0]["changes"][0]["value"]["channel_id"] = "channel-1"
    coalescer = _Coalescer()

    await process_inbound(
        fake_pool,
        payload,
        coalescer,
        transport="discord",
        bot_id="mediator",
    )

    assert len(coalescer.calls) == 1
    user_id, _message_id, _user, _source, scope = coalescer.calls[0]
    assert scope.bot_id == "mediator"
    assert scope.transport == "discord"
    assert scope.user_id == user_id
    assert scope.channel_id == "channel-1"
    assert fake_pool.user_identities[("discord", "15555550100")] == user_id


async def test_first_time_sender_identity_exists_before_scope_binding_lookup(fake_pool, monkeypatch) -> None:
    """First-time senders must be durable before scope is resolved.

    Regression guard: resolving binding before user_identities exists can miss
    first-time Discord senders and route them through fallback mediator state.
    """
    topic_id = uuid4()

    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    async def fake_primary_topic_id_for(pool, bot_spec):
        assert bot_spec.bot_id == "tante_rosi"
        return topic_id

    async def fake_resolve_binding(pool, *, bot_id, user_id):
        assert bot_id == "tante_rosi"
        assert pool.user_identities[("discord", "15555550100")] == user_id
        return SimpleNamespace(binding_id=uuid4(), dyad_id=None)

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    monkeypatch.setattr("app.services.inbound.primary_topic_id_for", fake_primary_topic_id_for)
    monkeypatch.setattr("app.services.inbound.routing.resolve_binding", fake_resolve_binding)
    monkeypatch.setitem(BOT_SPECS, "tante_rosi", build_tante_rosi_spec())
    coalescer = _Coalescer()

    await process_inbound(fake_pool, _payload("first.discord.rosi"), coalescer, transport="discord", bot_id="tante_rosi")

    assert len(coalescer.calls) == 1
    scope = coalescer.calls[0][4]
    assert scope.bot_id == "tante_rosi"
    assert scope.topic_id == topic_id
    assert scope.binding_id is not None
    assert scope.dyad_id is None


async def test_solo_pause_does_not_lookup_partner(fake_pool, app_env, monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.inbound.get_bot_spec",
        lambda bot_id: SimpleNamespace(
            bot_id=bot_id,
            primary_topic_slug="relationship",
            participants_shape="solo",
        ),
    )

    async def fail_partner_of(pool, user):
        raise AssertionError("solo pause must not call partner_of")

    sent = []

    async def fake_send(pool, recipient, content, *, template_fallback=None, ignore_pause=False, scope, **kwargs):
        sent.append((recipient.id, scope.bot_id, scope.topic_id, ignore_pause))
        return uuid4()

    monkeypatch.setattr("app.services.inbound.partner_of", fail_partner_of)
    monkeypatch.setattr("app.services.inbound.send_outbound", fake_send)
    payload = _payload("solo.pause")
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["text"]["body"] = "/pause"

    await process_inbound(fake_pool, payload, _Coalescer(), transport="discord", bot_id="solo_bot")

    assert len(sent) == 1
    assert sent[0][1] == "solo_bot"
    assert sent[0][2] == get_relationship_topic_id()
    assert sent[0][3] is True


async def test_rosi_inbound_scope_reaches_agentic_and_outbound(fake_pool, app_env, monkeypatch) -> None:
    pregnancy_topic_id = uuid4()
    rosi_spec = build_tante_rosi_spec()
    monkeypatch.setitem(BOT_SPECS, rosi_spec.bot_id, rosi_spec)

    async def fake_classify_charge(pool, content):
        return _Charge("routine")

    async def fake_primary_topic_id_for(pool, bot_spec):
        assert bot_spec.bot_id == "tante_rosi"
        return pregnancy_topic_id

    async def fake_resolve_binding(pool, *, bot_id, user_id):
        assert bot_id == "tante_rosi"
        return SimpleNamespace(binding_id=uuid4(), dyad_id=None)

    async def forbidden_partner_of(*args, **kwargs):
        raise AssertionError("Rosi solo inbound path must not resolve a dyadic partner")

    async def fake_build_hot_context_solo(pool, ctx_user, message_ids, trigger_metadata, **kwargs):
        assert kwargs["bot_id"] == "tante_rosi"
        assert kwargs["primary_topic_id"] == pregnancy_topic_id
        return SimpleNamespace(
            trigger_metadata=trigger_metadata or {"kind": "inbound"},
            recent_messages=[],
            open_watch_items=[],
            active_oob=[],
        )

    async def fake_run_step(client, ctx, system_prompt, hot_context_rendered, allowed_tools, seed_messages, **kwargs):
        assert ctx.bot_id == "tante_rosi"
        assert ctx.primary_topic_id == pregnancy_topic_id
        assert ctx.participants_shape == "solo"
        assert ctx.dyad_id is None
        if ctx.current_step == "respond":
            return "Hallo, ich bin Tante Rosi.", [], 0
        return "", [], 0

    sent = []

    async def fake_send(pool, recipient, content, *, scope, **kwargs):
        assert scope.bot_id == "tante_rosi"
        assert scope.topic_id == pregnancy_topic_id
        sent.append((recipient.id, content, scope))
        out_id = uuid4()
        pool.messages[out_id] = {
            "id": out_id,
            "direction": "outbound",
            "sender_id": None,
            "recipient_id": recipient.id,
            "content": content,
            "processing_state": "processed",
            "sent_at": datetime.now(UTC),
            "charge": None,
            "deleted_at": None,
            "bot_id": scope.bot_id,
            "topic_id": scope.topic_id,
        }
        return {"status": "sent", "message_id": out_id, "visible_to_user": True, "provider_message_id": None}

    class AgenticCoalescer:
        async def add(self, user_id, message_id, user: User, *, source: str = "live", scope) -> None:
            assert source == "live"
            assert scope.bot_id == "tante_rosi"
            assert scope.topic_id == pregnancy_topic_id
            await agentic.run_agentic_turn([message_id], user, scope=scope)

    monkeypatch.setattr("app.services.inbound.classify_charge", fake_classify_charge)
    monkeypatch.setattr("app.services.inbound.primary_topic_id_for", fake_primary_topic_id_for)
    monkeypatch.setattr("app.services.inbound.routing.resolve_binding", fake_resolve_binding)
    monkeypatch.setattr(agentic, "partner_of", forbidden_partner_of)
    monkeypatch.setattr(agentic, "build_hot_context_solo", fake_build_hot_context_solo)
    monkeypatch.setattr(agentic, "render_hot_context_solo", lambda hot_context: "Rosi hot context")
    monkeypatch.setattr(agentic, "run_step", fake_run_step)
    monkeypatch.setattr(agentic, "send_outbound", fake_send)
    agentic.set_pool(fake_pool)

    await process_inbound(fake_pool, _payload("discord.rosi.full-path"), AgenticCoalescer(), transport="discord", bot_id="tante_rosi")

    inbound = next(row for row in fake_pool.messages.values() if row.get("whatsapp_message_id") == "discord.rosi.full-path")
    outbound = next(row for row in fake_pool.messages.values() if row.get("direction") == "outbound")
    turn = next(iter(fake_pool.bot_turns.values()))
    assert inbound["bot_id"] == "tante_rosi"
    assert inbound["topic_id"] == pregnancy_topic_id
    assert turn["bot_id"] == "tante_rosi"
    assert turn["topic_id"] == pregnancy_topic_id
    assert outbound["bot_id"] == "tante_rosi"
    assert outbound["topic_id"] == pregnancy_topic_id
    assert sent[0][1].startswith("Hallo")
