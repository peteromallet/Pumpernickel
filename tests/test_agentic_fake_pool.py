from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.retrieval import RetrievalQuery, hybrid_search
from app.services.tools.read_tools import (
    messages_after,
    messages_before,
    scroll,
    search,
    search_messages,
    topic_recent,
)
from app.services.tools.registry import call_tool
from app.services.turn_context import TurnContext
from tests.agentic.fake_pool import AgenticFakePool
from tool_schemas import (
    MessagesAfterInput,
    MessagesBeforeInput,
    ScrollInput,
    SearchInput,
    SearchMessagesInput,
    TopicRecentInput,
)

pytestmark = pytest.mark.anyio


class _FixedEmbedder:
    model_name = "fixture-semantic"
    dimension = 3

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = mapping

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.mapping[text] for text in texts]


def _message(
    *,
    message_id: UUID,
    sender_id: UUID,
    recipient_id: UUID,
    direction: str,
    sent_at: str,
    content: str,
    topic_id: UUID,
    bot_id: str = "mediator",
    charge: str = "routine",
) -> dict[str, object]:
    return {
        "id": str(message_id),
        "sender_id": str(sender_id),
        "recipient_id": str(recipient_id),
        "thread_owner_user_id": str(sender_id if direction == "inbound" else recipient_id),
        "thread_owner_partner_share": "opt_in",
        "direction": direction,
        "sent_at": sent_at,
        "content": content,
        "bot_id": bot_id,
        "topic_id": str(topic_id),
        "charge": charge,
    }


def _build_pool() -> tuple[AgenticFakePool, TurnContext, list[UUID]]:
    topic_id = uuid4()
    turn_id = uuid4()
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    message_ids = [uuid4() for _ in range(6)]
    messages = [
        _message(
            message_id=message_ids[0],
            sender_id=user.id,
            recipient_id=partner.id,
            direction="inbound",
            sent_at="2026-05-30T08:00:00+00:00",
            content="Let's plan a quiet coast weekend.",
            topic_id=topic_id,
        ),
        _message(
            message_id=message_ids[1],
            sender_id=partner.id,
            recipient_id=user.id,
            direction="outbound",
            sent_at="2026-05-30T08:05:00+00:00",
            content="I found a small harbor hotel with ocean views.",
            topic_id=topic_id,
        ),
        _message(
            message_id=message_ids[2],
            sender_id=user.id,
            recipient_id=partner.id,
            direction="inbound",
            sent_at="2026-05-30T08:10:00+00:00",
            content="Book the seafood dinner too.",
            topic_id=topic_id,
        ),
        _message(
            message_id=message_ids[3],
            sender_id=partner.id,
            recipient_id=user.id,
            direction="outbound",
            sent_at="2026-05-30T08:15:00+00:00",
            content="Done. Friday at seven on the pier.",
            topic_id=topic_id,
        ),
        _message(
            message_id=message_ids[4],
            sender_id=user.id,
            recipient_id=partner.id,
            direction="inbound",
            sent_at="2026-05-30T08:20:00+00:00",
            content="Also remind me about the car insurance.",
            topic_id=topic_id,
        ),
        _message(
            message_id=message_ids[5],
            sender_id=partner.id,
            recipient_id=user.id,
            direction="outbound",
            sent_at="2026-05-30T08:25:00+00:00",
            content="Sure, I will nudge you Thursday.",
            topic_id=topic_id,
        ),
    ]
    pool = AgenticFakePool(
        messages=messages,
        viewer_user_id=user.id,
        partner_user_id=partner.id,
        bot_id="mediator",
        topic_id=topic_id,
        turn_id=turn_id,
    )
    ctx = TurnContext(
        turn_id=turn_id,
        pool=pool,
        user=user,
        partner=partner,
        triggering_message_ids=[message_ids[-1]],
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=topic_id,
        current_step="read",
        turn_started_at=datetime(2026, 5, 30, 8, 30, tzinfo=UTC),
        hot_context_window_edge={
            "message_id": str(message_ids[3]),
            "sent_at": "2026-05-30T08:15:00+00:00",
        },
    )
    pool.bot_turns[turn_id] = {"id": turn_id}
    return pool, ctx, message_ids


async def test_nav_tools_run_against_agentic_fake_pool() -> None:
    pool, ctx, message_ids = _build_pool()

    before = await messages_before(
        ctx, MessagesBeforeInput(anchor="current", n=2)
    )
    after = await messages_after(
        ctx, MessagesAfterInput(anchor=message_ids[2], n=2)
    )
    recent = await topic_recent(ctx, TopicRecentInput(n=3))
    older = await scroll(
        ctx, ScrollInput(cursor=before.cursor, direction="older", n=2)
    )

    assert [hit.message_id for hit in before.messages] == message_ids[1:3]
    assert [hit.message_id for hit in after.messages] == message_ids[3:5]
    assert [hit.message_id for hit in recent.messages] == list(reversed(message_ids[3:6]))
    assert [hit.message_id for hit in older.messages] == message_ids[:1]
    assert pool.infrastructure_status()["infrastructure_failed"] is False


async def test_search_and_registry_audit_writes_use_supported_sql() -> None:
    pool, ctx, message_ids = _build_pool()

    exact = await search(
        ctx,
        SearchInput(query="coast weekend", mode="exact", scope="topic", limit=2),
    )
    filtered = await search_messages(
        ctx,
        SearchMessagesInput(text_contains="insurance", limit=5),
    )
    audited = await call_tool("topic_recent", {"n": 2}, ctx)

    assert [hit.message_id for hit in exact.hits] == [message_ids[0]]
    assert [hit.id for hit in filtered.hits] == [message_ids[4]]
    assert [UUID(item["message_id"]) for item in audited["messages"]] == list(
        reversed(message_ids[4:6])
    )
    assert [row["event_type"] for row in pool.turn_audit_events] == [
        "tool.requested",
        "tool.completed",
    ]
    assert [row["tool_name"] for row in pool.tool_calls] == ["topic_recent"]
    assert pool.infrastructure_status()["infrastructure_failed"] is False


async def test_hybrid_retrieval_sql_runs_against_agentic_fake_pool() -> None:
    pool, ctx, message_ids = _build_pool()
    embedder = _FixedEmbedder({"shoreline escape": [1.0, 0.0, 0.0]})
    pool.seed_embedding(
        message_ids[1], embedding=[1.0, 0.0, 0.0], model=embedder.model_name, dimension=3
    )
    pool.seed_embedding(
        message_ids[4], embedding=[0.0, 1.0, 0.0], model=embedder.model_name, dimension=3
    )

    results = await hybrid_search(
        pool,
        RetrievalQuery(
            query="shoreline escape",
            viewer_user_id=ctx.user.id,
            partner_user_id=ctx.partner.id,
            bot_id=ctx.bot_id or "mediator",
            topic_id=ctx.primary_topic_id,
            mode="hybrid",
            limit=3,
        ),
        embedder=embedder,
    )

    assert [result.message_id for result in results[:2]] == [message_ids[1], message_ids[4]]
    assert results[0].match_type == "semantic"
    assert pool.infrastructure_status()["infrastructure_failed"] is False


async def test_unsupported_sql_is_captured_as_infrastructure_failure(tmp_path: Path) -> None:
    pool, _, _ = _build_pool()

    result = await pool.fetchval("SELECT count(*) FROM not_supported_table")
    infra_path = tmp_path / "infrastructure.json"
    pool.write_infrastructure_json(infra_path)
    payload = json.loads(infra_path.read_text(encoding="utf-8"))

    assert result is None
    assert payload["infrastructure_failed"] is True
    assert payload["status"] == "infrastructure"
    assert payload["issues"][0]["kind"] == "fetchval"
