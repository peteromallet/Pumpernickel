"""
Tests for the inbound queue hardening sprint.

Covers:
- Claim atomicity and race resistance
- Completion with all handling_result values
- Failure marking with processing_error
- Deferral, expiry
- Stale processing recovery
- Retryable failed recovery
- Direction guard enforcement
- Bot/topic scope enforcement
- Fully unclaimable non-empty trigger lists
- Crash-before-turn (raw row recovered)
- Crash-during-turn (stale processing recovered)
- Duplicate catch-up replay
- Deliberate silence, visible reaction
- Newer-inbound withheld
- Provider send failure
- Multi-bot DM isolation
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.services import inbound_queue
from app.services.inbound_queue import (
    claim_messages_for_turn,
    complete_messages,
    fail_messages,
    defer_messages,
    expire_messages,
    recover_stale_processing,
    recover_retryable_failed,
)
from tests.conftest import FakePool


pytestmark = pytest.mark.anyio


# ── helpers ──────────────────────────────────────────────────────────

def _msg(
    *,
    msg_id: UUID | None = None,
    direction: str = "inbound",
    processing_state: str = "raw",
    bot_id: str = "mediator",
    topic_id: UUID | None = None,
    handling_result: str | None = None,
    handled_at: datetime | None = None,
    handled_by_turn_id: UUID | None = None,
    processing_started_at: datetime | None = None,
    processing_error: str | None = None,
    processing_attempts: int = 0,
    sent_at: datetime | None = None,
    sender_id: UUID | None = None,
    recipient_id: UUID | None = None,
    content: str = "hello",
    deleted_at: datetime | None = None,
    charge: str | None = None,
) -> dict:
    msg_id = msg_id or uuid4()
    topic_id = topic_id or uuid4()
    sender_id = sender_id or uuid4()
    return {
        "id": msg_id,
        "direction": direction,
        "processing_state": processing_state,
        "bot_id": bot_id,
        "topic_id": topic_id,
        "handling_result": handling_result,
        "handled_at": handled_at,
        "handled_by_turn_id": handled_by_turn_id,
        "processing_started_at": processing_started_at,
        "processing_error": processing_error,
        "processing_attempts": processing_attempts,
        "sent_at": sent_at or datetime.now(UTC) - timedelta(minutes=1),
        "sender_id": sender_id,
        "recipient_id": recipient_id,
        "content": content,
        "deleted_at": deleted_at,
        "charge": charge,
    }


def _seed_msg(pool: FakePool, **kwargs) -> UUID:
    m = _msg(**kwargs)
    pool.messages[m["id"]] = m
    return m["id"]


# ── claim atomicity ──────────────────────────────────────────────────


async def test_claim_messages_for_turn_atomic_and_race_resistant():
    """claim_messages_for_turn atomically transitions eligible rows to processing."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    ids = [_seed_msg(pool, bot_id=bot_id, topic_id=topic_id) for _ in range(3)]

    claimed = await claim_messages_for_turn(pool, ids, bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 3
    assert set(claimed) == set(ids)

    for mid in ids:
        m = pool.messages[mid]
        assert m["processing_state"] == "processing"
        assert m["processing_started_at"] is not None
        assert m["processing_attempts"] == 1
        assert m["processing_error"] is None

    # Re-claim should claim zero rows (already processing, no stale window)
    claimed2 = await claim_messages_for_turn(pool, ids, bot_id=bot_id, topic_id=topic_id)
    assert len(claimed2) == 0


async def test_claim_rejects_outbound():
    """claim_messages_for_turn skips outbound rows via direction guard."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    out_id = _seed_msg(pool, direction="outbound", bot_id=bot_id, topic_id=topic_id)

    claimed = await claim_messages_for_turn(pool, [out_id], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 0
    assert pool.messages[out_id]["processing_state"] == "raw"


async def test_claim_respects_bot_and_topic_scope():
    """claim_messages_for_turn only claims rows matching bot_id and topic_id."""
    pool = FakePool()
    bot_a, bot_b = "mediator", "coach"
    topic_a, topic_b = uuid4(), uuid4()

    id1 = _seed_msg(pool, bot_id=bot_a, topic_id=topic_a)
    id2 = _seed_msg(pool, bot_id=bot_b, topic_id=topic_a)  # wrong bot
    id3 = _seed_msg(pool, bot_id=bot_a, topic_id=topic_b)  # wrong topic

    claimed = await claim_messages_for_turn(
        pool, [id1, id2, id3], bot_id=bot_a, topic_id=topic_a
    )
    assert claimed == [id1]
    assert pool.messages[id1]["processing_state"] == "processing"
    assert pool.messages[id2]["processing_state"] == "raw"
    assert pool.messages[id3]["processing_state"] == "raw"


async def test_claim_stale_processing_recoverable():
    """Stale processing rows are recovered by recover_stale_processing, not claim."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC) - timedelta(minutes=10),
        processing_attempts=1,
    )

    # claim_messages_for_turn only claims raw/deferred, NOT stale processing.
    # Stale processing recovery is handled by recover_stale_processing.
    claimed = await claim_messages_for_turn(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 0

    # recover_stale_processing resets stale processing to raw
    count = await recover_stale_processing(pool, bot_id=bot_id, topic_id=topic_id)
    assert count >= 1
    assert pool.messages[mid]["processing_state"] == "raw"

    # Now claimable
    claimed2 = await claim_messages_for_turn(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed2) == 1
    assert pool.messages[mid]["processing_attempts"] == 2


async def test_claim_deferred_messages():
    """Deferred messages can be claimed."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool, bot_id=bot_id, topic_id=topic_id, processing_state="deferred"
    )

    claimed = await claim_messages_for_turn(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 1
    assert pool.messages[mid]["processing_state"] == "processing"


async def test_claim_empty_list_returns_empty():
    """Claiming an empty list returns empty."""
    pool = FakePool()
    claimed = await claim_messages_for_turn(pool, [], bot_id="mediator", topic_id=uuid4())
    assert claimed == []


# ── completion ───────────────────────────────────────────────────────


async def test_complete_messages_all_handling_results():
    """complete_messages stamps terminal metadata for all handling_result values."""
    pool = FakePool()
    turn_id = uuid4()
    bot_id = "mediator"
    topic_id = uuid4()

    results = ["replied", "silent", "withheld_newer_inbound", "no_action", "expired"]
    msg_ids = {
        r: _seed_msg(
            pool,
            processing_state="processing",
            bot_id=bot_id,
            topic_id=topic_id,
            processing_started_at=datetime.now(UTC),
            handled_by_turn_id=turn_id,
        )
        for r in results
    }

    for result, mid in msg_ids.items():
        await complete_messages(
            pool,
            [mid],
            handling_result=result,
            handled_by_turn_id=turn_id,
            bot_id=bot_id,
            topic_id=topic_id,
        )
        m = pool.messages[mid]
        assert m["processing_state"] == "processed"
        assert m["handling_result"] == result
        assert m["handled_at"] is not None
        assert m["handled_by_turn_id"] == turn_id


async def test_complete_messages_empty_list_noop():
    """Complete on empty list is a safe no-op."""
    pool = FakePool()
    await complete_messages(
        pool, [], handling_result="silent", bot_id="mediator", topic_id=uuid4()
    )
    # No exception raised


async def test_complete_messages_applies_to_any_inbound_row_in_scope():
    """complete_messages trusts the caller to pass appropriate rows; no state filter."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    turn_id = uuid4()

    mid = _seed_msg(
        pool, bot_id=bot_id, topic_id=topic_id, processing_state="processing",
        processing_started_at=datetime.now(UTC),
    )

    await complete_messages(
        pool, [mid],
        handling_result="silent",
        handled_by_turn_id=turn_id,
        bot_id=bot_id,
        topic_id=topic_id,
    )
    assert pool.messages[mid]["processing_state"] == "processed"
    assert pool.messages[mid]["handling_result"] == "silent"


# ── failure marking ──────────────────────────────────────────────────


async def test_fail_messages_with_processing_error():
    """fail_messages marks rows failed with error metadata."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    turn_id = uuid4()
    mid = _seed_msg(
        pool,
        processing_state="processing",
        bot_id=bot_id,
        topic_id=topic_id,
        processing_started_at=datetime.now(UTC),
        handled_by_turn_id=turn_id,
    )

    await fail_messages(
        pool, [mid],
        processing_error="LLM timeout after 90s",
        handled_by_turn_id=turn_id,
        bot_id=bot_id,
        topic_id=topic_id,
    )
    m = pool.messages[mid]
    assert m["processing_state"] == "failed"
    assert "LLM timeout after 90s" in m["processing_error"]
    assert m["handled_by_turn_id"] == turn_id


async def test_fail_messages_null_turn_id_crash_before_turn():
    """fail_messages handles null turn_id for crash-before-turn scenarios."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        processing_state="processing",
        bot_id=bot_id,
        topic_id=topic_id,
        processing_started_at=datetime.now(UTC),
    )

    await fail_messages(
        pool, [mid],
        processing_error="Worker crashed before turn open",
        handled_by_turn_id=None,
        bot_id=bot_id,
        topic_id=topic_id,
    )
    m = pool.messages[mid]
    assert m["processing_state"] == "failed"


async def test_fail_messages_applies_to_any_inbound_row_in_scope():
    """fail_messages trusts the caller to pass appropriate rows; no state filter."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool, bot_id=bot_id, topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC),
    )

    await fail_messages(
        pool, [mid],
        processing_error="error",
        handled_by_turn_id=uuid4(),
        bot_id=bot_id,
        topic_id=topic_id,
    )
    assert pool.messages[mid]["processing_state"] == "failed"
    assert pool.messages[mid]["processing_error"] == "error"


# ── deferral ─────────────────────────────────────────────────────────


async def test_defer_messages():
    """defer_messages transitions raw/processing rows to deferred."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    raw_id = _seed_msg(pool, bot_id=bot_id, topic_id=topic_id, processing_state="raw")
    proc_id = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC),
    )

    await defer_messages(pool, [raw_id, proc_id], bot_id=bot_id, topic_id=topic_id)
    assert pool.messages[raw_id]["processing_state"] == "deferred"
    assert pool.messages[proc_id]["processing_state"] == "deferred"


# ── expiry ───────────────────────────────────────────────────────────


async def test_expire_messages():
    """expire_messages marks rows expired with handling_result='expired'."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="raw",
        sent_at=datetime.now(UTC) - timedelta(days=10),
    )

    await expire_messages(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    m = pool.messages[mid]
    assert m["processing_state"] == "expired"
    assert m["handling_result"] == "expired"
    assert m["handled_at"] is not None


async def test_expire_messages_applies_to_any_inbound_row_in_scope():
    """expire_messages trusts the caller to pass appropriate rows; no terminal guard."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool, bot_id=bot_id, topic_id=topic_id, processing_state="raw",
        sent_at=datetime.now(UTC) - timedelta(days=10),
    )
    await expire_messages(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    assert pool.messages[mid]["processing_state"] == "expired"
    assert pool.messages[mid]["handling_result"] == "expired"


# ── recovery ─────────────────────────────────────────────────────────


async def test_recover_stale_processing():
    """Stale processing rows are reset to raw."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    stale = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC) - timedelta(minutes=10),
        processing_attempts=1,
    )
    fresh = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC) - timedelta(seconds=30),
    )

    count = await recover_stale_processing(pool, bot_id=bot_id, topic_id=topic_id)
    assert count >= 1
    assert pool.messages[stale]["processing_state"] == "raw"
    # fresh should still be processing (not stale)
    assert pool.messages[fresh]["processing_state"] == "processing"


async def test_recover_retryable_failed():
    """Failed rows below max_retries are reset to raw."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    retryable = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="failed",
        processing_attempts=2,
        processing_error="transient network error",
    )
    exhausted = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="failed",
        processing_attempts=5,
        processing_error="exhausted",
    )

    count = await recover_retryable_failed(
        pool, bot_id=bot_id, topic_id=topic_id, max_retries=3
    )
    assert count >= 1
    assert pool.messages[retryable]["processing_state"] == "raw"
    # exhausted should remain failed
    assert pool.messages[exhausted]["processing_state"] == "failed"


async def test_recover_retryable_failed_default_max():
    """Default max_retries=3 is applied when not specified."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    retryable = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="failed",
        processing_attempts=1,
        processing_error="error",
    )
    count = await recover_retryable_failed(pool, bot_id=bot_id, topic_id=topic_id)
    assert count >= 1
    assert pool.messages[retryable]["processing_state"] == "raw"


# ── direction guard ──────────────────────────────────────────────────


async def test_direction_guard_on_all_helpers():
    """All queue helpers enforce direction='inbound'."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    out_id = _seed_msg(pool, direction="outbound", bot_id=bot_id, topic_id=topic_id)

    # Claim should not claim outbound
    claimed = await claim_messages_for_turn(pool, [out_id], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 0

    # Complete should not touch outbound
    await complete_messages(
        pool, [out_id],
        handling_result="replied",
        handled_by_turn_id=uuid4(),
        bot_id=bot_id,
        topic_id=topic_id,
    )
    assert pool.messages[out_id].get("handling_result") is None

    # Fail should not touch outbound
    await fail_messages(
        pool, [out_id],
        processing_error="err",
        handled_by_turn_id=uuid4(),
        bot_id=bot_id,
        topic_id=topic_id,
    )
    assert pool.messages[out_id].get("processing_error") is None

    # Defer should not touch outbound
    await defer_messages(pool, [out_id], bot_id=bot_id, topic_id=topic_id)
    assert pool.messages[out_id]["processing_state"] == "raw"

    # Expire should not touch outbound
    await expire_messages(pool, [out_id], bot_id=bot_id, topic_id=topic_id)
    assert pool.messages[out_id]["processing_state"] == "raw"


# ── crash and catch-up scenarios ─────────────────────────────────────


async def test_crash_before_turn_raw_recovered():
    """Raw row left by downtime is recoverable by the sweeper."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="raw",
        sent_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    # Simulate sweeper: claim the raw message
    claimed = await claim_messages_for_turn(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 1
    assert pool.messages[mid]["processing_state"] == "processing"


async def test_crash_during_turn_stale_processing_recovered():
    """Stale processing row after worker crash is recovered."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC) - timedelta(minutes=15),
        processing_attempts=1,
    )

    count = await recover_stale_processing(pool, bot_id=bot_id, topic_id=topic_id)
    assert count >= 1
    assert pool.messages[mid]["processing_state"] == "raw"


async def test_duplicate_catchup_replay_processed_row_no_new_turn():
    """Replaying an already-processed message does not create a new turn."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    turn_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processed",
        handling_result="replied",
        handled_at=datetime.now(UTC),
        handled_by_turn_id=turn_id,
    )

    # Attempt to claim an already-processed row
    claimed = await claim_messages_for_turn(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 0
    assert pool.messages[mid]["processing_state"] == "processed"


async def test_duplicate_catchup_replay_raw_row_gets_processed():
    """Replaying a raw row from catch-up gets claimed and processed."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(pool, bot_id=bot_id, topic_id=topic_id, processing_state="raw")

    claimed = await claim_messages_for_turn(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 1
    assert pool.messages[mid]["processing_state"] == "processing"


async def test_deliberate_silence_handling_result():
    """Deliberate silence is recorded with handling_result='silent'."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC),
    )

    await complete_messages(
        pool, [mid],
        handling_result="silent",
        handled_by_turn_id=uuid4(),
        bot_id=bot_id,
        topic_id=topic_id,
    )
    m = pool.messages[mid]
    assert m["processing_state"] == "processed"
    assert m["handling_result"] == "silent"
    assert m["handled_at"] is not None


async def test_visible_reaction_handling_result_replied():
    """Visible reactions use handling_result='replied'."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    turn_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC),
        handled_by_turn_id=turn_id,
    )

    await complete_messages(
        pool, [mid],
        handling_result="replied",
        handled_by_turn_id=turn_id,
        bot_id=bot_id,
        topic_id=topic_id,
    )
    m = pool.messages[mid]
    assert m["processing_state"] == "processed"
    assert m["handling_result"] == "replied"


async def test_newer_inbound_withheld():
    """Stale outbound withheld due to newer inbound is recorded."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    turn_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC),
        handled_by_turn_id=turn_id,
    )

    await complete_messages(
        pool, [mid],
        handling_result="withheld_newer_inbound",
        handled_by_turn_id=turn_id,
        bot_id=bot_id,
        topic_id=topic_id,
    )
    m = pool.messages[mid]
    assert m["processing_state"] == "processed"
    assert m["handling_result"] == "withheld_newer_inbound"


async def test_provider_send_failure_marked_failed():
    """Provider send failure marks message as failed."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    turn_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC),
        handled_by_turn_id=turn_id,
    )

    await fail_messages(
        pool, [mid],
        processing_error="provider_send_failed: Discord API error 500",
        handled_by_turn_id=turn_id,
        bot_id=bot_id,
        topic_id=topic_id,
    )
    m = pool.messages[mid]
    assert m["processing_state"] == "failed"
    assert "provider_send_failed" in m["processing_error"]


async def test_multi_bot_dm_isolation():
    """Different bots' DMs don't interfere with each other's queue states."""
    pool = FakePool()
    topic_shared = uuid4()  # same topic for both bots

    bot_a_id = _seed_msg(
        pool,
        bot_id="mediator",
        topic_id=topic_shared,
        processing_state="raw",
        content="msg for mediator",
    )
    bot_b_id = _seed_msg(
        pool,
        bot_id="coach",
        topic_id=topic_shared,
        processing_state="raw",
        content="msg for coach",
    )

    # Claim only for mediator
    claimed = await claim_messages_for_turn(
        pool, [bot_a_id, bot_b_id], bot_id="mediator", topic_id=topic_shared
    )
    assert claimed == [bot_a_id]
    assert pool.messages[bot_a_id]["processing_state"] == "processing"
    assert pool.messages[bot_b_id]["processing_state"] == "raw"

    # Complete mediator message
    await complete_messages(
        pool, [bot_a_id],
        handling_result="replied",
        handled_by_turn_id=uuid4(),
        bot_id="mediator",
        topic_id=topic_shared,
    )
    assert pool.messages[bot_a_id]["processing_state"] == "processed"

    # Coach message still raw and claimable
    claimed_b = await claim_messages_for_turn(
        pool, [bot_b_id], bot_id="coach", topic_id=topic_shared
    )
    assert claimed_b == [bot_b_id]


# ── fully unclaimable trigger lists ──────────────────────────────────


async def test_fully_unclaimable_nonempty_trigger_list_returns_zero():
    """A non-empty trigger list where all rows are terminal returns zero claimed."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()

    ids = [
        _seed_msg(
            pool,
            bot_id=bot_id,
            topic_id=topic_id,
            processing_state="processed",
            handling_result="replied",
            handled_at=datetime.now(UTC),
        ),
        _seed_msg(
            pool,
            bot_id=bot_id,
            topic_id=topic_id,
            processing_state="expired",
            handling_result="expired",
            handled_at=datetime.now(UTC),
        ),
    ]

    claimed = await claim_messages_for_turn(pool, ids, bot_id=bot_id, topic_id=topic_id)
    assert len(claimed) == 0


# ── media preprocessing scenarios ────────────────────────────────────


async def test_media_preprocessing_failure_marks_failed():
    """Media preprocessing failure marks message as failed."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="processing",
        processing_started_at=datetime.now(UTC),
    )

    await fail_messages(
        pool, [mid],
        processing_error="voice transcription failed: unsupported codec",
        handled_by_turn_id=uuid4(),
        bot_id=bot_id,
        topic_id=topic_id,
    )
    m = pool.messages[mid]
    assert m["processing_state"] == "failed"
    assert "unsupported codec" in m["processing_error"]


async def test_media_preprocessing_expiry():
    """Media message expired via spend cap is marked expired."""
    pool = FakePool()
    bot_id = "mediator"
    topic_id = uuid4()
    mid = _seed_msg(
        pool,
        bot_id=bot_id,
        topic_id=topic_id,
        processing_state="raw",
        sent_at=datetime.now(UTC) - timedelta(days=10),
    )

    await expire_messages(pool, [mid], bot_id=bot_id, topic_id=topic_id)
    m = pool.messages[mid]
    assert m["processing_state"] == "expired"
    assert m["handling_result"] == "expired"


# ── hot context queue outcome integration ────────────────────────────


async def test_hot_context_includes_queue_outcome_for_silent():
    """Hot context recent_messages include queue_outcome for silent messages."""
    from app.services.hot_context import _queue_outcome_label

    item = {
        "queue_outcome": {
            "handling_result": "silent",
            "handled_at": "2025-01-01T00:00:00",
            "processing_error": None,
        }
    }
    label = _queue_outcome_label(item)
    assert "silent" in label


async def test_hot_context_includes_queue_outcome_for_failed():
    """Hot context recent_messages include queue_outcome for failed messages."""
    from app.services.hot_context import _queue_outcome_label

    item = {
        "queue_outcome": {
            "handling_result": "failed",
            "handled_at": None,
            "processing_error": "LLM rate limit exceeded",
        }
    }
    label = _queue_outcome_label(item)
    assert "failed" in label
    assert "LLM rate limit" in label


async def test_hot_context_includes_queue_outcome_for_withheld():
    """Hot context recent_messages include queue_outcome for withheld messages."""
    from app.services.hot_context import _queue_outcome_label

    item = {
        "queue_outcome": {
            "handling_result": "withheld_newer_inbound",
            "handled_at": None,
            "processing_error": None,
        }
    }
    label = _queue_outcome_label(item)
    assert "withheld" in label.lower() or "stale" in label.lower()


async def test_hot_context_no_queue_outcome_for_replied():
    """Hot context does not add queue_outcome for normal replied messages."""
    from app.services.hot_context import _queue_outcome_label

    # A normal message without queue_outcome key
    item = {}
    label = _queue_outcome_label(item)
    assert label == ""


async def test_hot_context_no_queue_outcome_for_none():
    """_queue_outcome_label returns empty for items without queue_outcome."""
    from app.services.hot_context import _queue_outcome_label

    assert _queue_outcome_label({}) == ""
    assert _queue_outcome_label({"queue_outcome": None}) == ""


# ── recovery sweeper scoping ─────────────────────────────────────────


async def test_recovery_stale_processing_scoped():
    """recover_stale_processing only recovers rows for the right bot/topic."""
    pool = FakePool()
    bot_a, bot_b = "mediator", "coach"
    topic_a, topic_b = uuid4(), uuid4()

    mid_a = _seed_msg(
        pool,
        bot_id=bot_a,
        topic_id=topic_a,
        processing_state="processing",
        processing_started_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    mid_b = _seed_msg(
        pool,
        bot_id=bot_b,
        topic_id=topic_a,
        processing_state="processing",
        processing_started_at=datetime.now(UTC) - timedelta(minutes=10),
    )

    count = await recover_stale_processing(pool, bot_id=bot_a, topic_id=topic_a)
    assert count >= 1
    assert pool.messages[mid_a]["processing_state"] == "raw"
    # bot_b row should remain processing
    assert pool.messages[mid_b]["processing_state"] == "processing"
