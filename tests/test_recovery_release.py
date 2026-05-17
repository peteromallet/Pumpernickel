import pytest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.services.coalescer_registry import CoalescerRegistry
from app.services.recovery import recover_on_startup
from app.bots.registry import get_relationship_topic_id

pytestmark = pytest.mark.anyio


async def test_recovery_release(fake_pool, monkeypatch) -> None:
    fp = fake_pool
    u = uuid4(); fp.users[u] = {"id": u, "name": "M", "phone": "1", "timezone": "UTC"}
    t = get_relationship_topic_id()
    turn = uuid4()
    mid1, mid2 = uuid4(), uuid4()
    for mid in (mid1, mid2):
        fake_pool.messages[mid] = {
            "id": mid, "direction": "inbound", "sender_id": u,
            "content": "hi", "processing_state": "processing",
            "bot_id": "mediator", "topic_id": t,
            "sent_at": datetime.now(UTC) - timedelta(minutes=6),
            "bot_turn_id": turn, "processing_started_at": datetime.now(UTC),
            "processing_attempts": 1,
        }
    fake_pool.bot_turns[turn] = {
        "id": turn, "triggering_message_ids": [mid1, mid2],
        "user_in_context": u, "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None, "failure_reason": "crashed",
        "final_output_message_id": None, "bot_id": "mediator", "topic_id": t,
    }

    metric_calls = []
    monkeypatch.setattr(
        "app.services.recovery.metrics.incr",
        lambda name, **kw: metric_calls.append((name, kw)),
    )

    registry = CoalescerRegistry()
    await recover_on_startup(fake_pool, registry)

    # Turn marked terminal
    assert fake_pool.bot_turns[turn]["failure_reason"] == "crashed"
    assert fake_pool.bot_turns[turn]["completed_at"] is not None

    # Messages released to raw, bot_turn_id cleared
    for mid in (mid1, mid2):
        assert fake_pool.messages[mid]["processing_state"] == "raw"
        assert fake_pool.messages[mid]["bot_turn_id"] is None

    # Exactly one recovery_released metric, no recovery_requeued
    released = [c for c in metric_calls if c[0] == "recovery_released"]
    requeued = [c for c in metric_calls if c[0] == "recovery_requeued"]
    assert len(released) == 1
    assert released[0][1]["value"] == 2
    assert len(requeued) == 0

    # No new bot_turn created
    assert len(fake_pool.bot_turns) == 1
