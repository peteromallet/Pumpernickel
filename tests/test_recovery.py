import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import recovery
from app.services.coalescer_registry import CoalescerRegistry
from app.services.recovery import recover_on_startup
from app.bots.registry import get_relationship_topic_id


pytestmark = pytest.mark.anyio


class CoalescerRecorder:
    def __init__(self) -> None:
        self.add_calls = []
        self.add_burst_calls = []

    async def add(self, user_id, message_id, user, *, source: str = "live", scope):
        self.add_calls.append((user_id, message_id, user, source, scope))

    async def add_burst(self, user_id, message_ids, user, *, scope):
        self.add_burst_calls.append((user_id, message_ids, user, scope))


def _ready_registry(**bots: CoalescerRecorder) -> CoalescerRegistry:
    """Build a ready CoalescerRegistry from named recorders.

    Defaults to a single ``mediator`` recorder when called bare so existing
    tests that only need a single coalescer keep working.
    """
    registry = CoalescerRegistry()
    if not bots:
        bots = {"mediator": CoalescerRecorder()}
    for bot_id, recorder in bots.items():
        registry.register(bot_id, recorder)
        registry.mark_ready(bot_id)
    return registry


def _seed_user(fake_pool) -> User:
    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    return user


def _seed_message(fake_pool, user: User):
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "raw",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC) - timedelta(minutes=1),
        "charge": None,
        "whatsapp_message_id": f"wa-{message_id}",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
        "bot_id": "mediator",
        "topic_id": get_relationship_topic_id(),
    }
    return message_id


async def test_orphan_raw_message_readded_once(fake_pool) -> None:
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user)
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert len(coalescer.add_calls) == 1
    assert coalescer.add_calls[0][:4] == (user.id, message_id, user, "recovery")
    assert coalescer.add_calls[0][4].bot_id == "mediator"


async def test_crashed_turn_marks_failed_and_requeues_full_burst(fake_pool) -> None:
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user), _seed_message(fake_pool, user)]
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "user_in_context": user.id,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None,
        "failure_reason": None,
        "reasoning": "",
        "bot_id": "mediator",
        "topic_id": get_relationship_topic_id(),
    }
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert fake_pool.bot_turns[turn_id]["failure_reason"] == "crashed"
    assert fake_pool.bot_turns[turn_id]["completed_at"] is not None
    assert len(coalescer.add_burst_calls) == 0
    assert coalescer.add_calls == []


async def test_already_marked_crashed_turn_is_requeued_by_v2(fake_pool) -> None:
    """Recovery-v2 re-SELECTs crashed turns each cycle and re-dispatches them.

    SD-A1-T4 split the legacy invariants (which only mutate freshly-crashed
    rows via UPDATE...RETURNING) from the v2 dispatch path (which SELECTs all
    crashed-not-completed-not-sent rows).  A turn already marked ``crashed``
    by a prior cycle is therefore re-dispatched until a successor turn
    completes its burst — that is the durable recovery contract.
    """
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user)]
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "user_in_context": user.id,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None,
        "failure_reason": "crashed",
        "reasoning": "",
        "final_output_message_id": None,
        "bot_id": "mediator",
        "topic_id": get_relationship_topic_id(),
    }
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert fake_pool.bot_turns[turn_id]["completed_at"] is not None
    assert len(coalescer.add_burst_calls) == 0


async def test_failed_raw_message_requeues_to_matching_bot_coalescer(fake_pool) -> None:
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user)
    topic_id = get_relationship_topic_id()
    fake_pool.messages[message_id].update(
        {
            "bot_id": "hector",
            "topic_id": topic_id,
            "processing_state": "raw",
            "handling_result": "failed",
            "processing_attempts": 1,
            "processing_error": "BoundedLoopExceeded: tool iteration cap exceeded: 6",
        }
    )
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": [message_id],
        "user_in_context": user.id,
        "started_at": datetime.now(UTC) - timedelta(minutes=1),
        "completed_at": None,
        "failure_reason": "tool iteration cap exceeded: 6",
        "reasoning": "",
        "final_output_message_id": None,
        "bot_id": "hector",
        "topic_id": topic_id,
    }
    mediator_coalescer = CoalescerRecorder()
    hector_coalescer = CoalescerRecorder()

    await recover_on_startup(
        fake_pool,
        _ready_registry(mediator=mediator_coalescer, hector=hector_coalescer),
    )

    assert mediator_coalescer.add_calls == []
    assert len(hector_coalescer.add_calls) == 1
    assert hector_coalescer.add_calls[0][:4] == (
        user.id,
        message_id,
        user,
        "recovery",
    )
    assert hector_coalescer.add_calls[0][4].bot_id == "hector"


async def test_turn_that_crashed_after_send_is_not_requeued(fake_pool) -> None:
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user)]
    outbound_id = uuid4()
    fake_pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": user.id,
        "content": "Already sent",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC) - timedelta(minutes=6),
        "charge": None,
        "deleted_at": None,
    }
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "user_in_context": user.id,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None,
        "failure_reason": None,
        "reasoning": "",
        "final_output_message_id": outbound_id,
        "bot_id": "mediator",
        "topic_id": get_relationship_topic_id(),
    }
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert fake_pool.bot_turns[turn_id]["failure_reason"] == "crashed_after_send"
    assert coalescer.add_burst_calls == []


async def test_crashed_turn_without_turn_scope_identity_is_skipped(fake_pool) -> None:
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user)]
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "user_in_context": user.id,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": None,
        "failure_reason": None,
        "reasoning": "",
        "bot_id": None,
        "topic_id": get_relationship_topic_id(),
    }
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert fake_pool.bot_turns[turn_id]["failure_reason"] == "crashed"
    assert coalescer.add_burst_calls == []


async def test_raw_message_without_scope_identity_is_skipped(fake_pool) -> None:
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user)
    fake_pool.messages[message_id]["bot_id"] = None
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert coalescer.add_calls == []
    assert coalescer.add_burst_calls == []


async def test_recovery_loop_rechecks_orphan_raw_messages(fake_pool, monkeypatch) -> None:
    user = _seed_user(fake_pool)
    message_id = _seed_message(fake_pool, user)
    coalescer = CoalescerRecorder()
    calls = 0

    async def fake_sleep(seconds):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(recovery.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await recovery.run_recovery_forever(
            fake_pool, _ready_registry(mediator=coalescer), interval_seconds=0
        )

    assert len(coalescer.add_calls) == 1
    assert coalescer.add_calls[0][:4] == (user.id, message_id, user, "recovery")


# ── recovery-v2 lifecycle filters (T9 (c)/(d)) ────────────────────────


async def test_terminal_post_send_rows_excluded_from_v2_recovery(fake_pool) -> None:
    """terminal_post_send failure_class blocks recovery_retryable_failed and
    raw-message re-dispatch, even when retries remain.

    The live mapping pins crashed_after_send → terminal_post_send (see
    inbound_queue.FAILURE_REASON_TO_CLASS); we set the class directly on the
    seeded row rather than fabricating a fail_messages call site, because
    crashed_after_send is forward-compat / dead today (SD-A1-T5).
    """
    user = _seed_user(fake_pool)
    mid = _seed_message(fake_pool, user)
    fake_pool.messages[mid].update(
        {
            "processing_state": "failed",
            "processing_attempts": 1,
            "handling_result": "failed",
            "failure_class": "terminal_post_send",
            "next_retry_at": None,
        }
    )
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert fake_pool.messages[mid]["processing_state"] == "failed"
    assert coalescer.add_calls == []


async def test_infra_bug_rows_excluded_from_v2_recovery(fake_pool) -> None:
    """infra_bug failure_class is treated as terminal for automatic recovery."""
    user = _seed_user(fake_pool)
    mid = _seed_message(fake_pool, user)
    fake_pool.messages[mid].update(
        {
            "processing_state": "failed",
            "processing_attempts": 1,
            "handling_result": "failed",
            "failure_class": "infra_bug",
            "next_retry_at": None,
        }
    )
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert fake_pool.messages[mid]["processing_state"] == "failed"
    assert coalescer.add_calls == []


async def test_retryable_pre_send_rows_flip_back_to_raw(fake_pool) -> None:
    """A retryable_pre_send row (mapped from any live reason — provider_send_failed,
    llm_timeout, transcription_failed, vision_failed, crashed,
    tool_validation_recoverable_exhausted) is flipped back to raw by
    recover_retryable_failed.  The dispatch picks it up on the next tick via
    the raw-message SELECT; the per-tick flip is the unit asserted here."""
    user = _seed_user(fake_pool)
    mid = _seed_message(fake_pool, user)
    fake_pool.messages[mid].update(
        {
            "processing_state": "failed",
            "processing_attempts": 1,
            "handling_result": "failed",
            "failure_class": "retryable_pre_send",
            "next_retry_at": None,
        }
    )
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    assert fake_pool.messages[mid]["processing_state"] == "raw"


async def test_schema_acceptance_lifecycle_columns_round_trip(fake_pool) -> None:
    """SD-004: a single message row exposes processing_state, failure_class,
    next_retry_at, processing_attempts, handled_by_turn_id after a fail_messages
    write — keeping recovery's column contract testable in-process."""
    from app.services.inbound_queue import fail_messages

    user = _seed_user(fake_pool)
    mid = _seed_message(fake_pool, user)
    fake_pool.messages[mid]["processing_state"] = "processing"
    fake_pool.messages[mid]["processing_started_at"] = datetime.now(UTC)
    fake_pool.messages[mid]["processing_attempts"] = 2

    turn_id = uuid4()
    await fail_messages(
        fake_pool, [mid],
        processing_error="x",
        handled_by_turn_id=turn_id,
        bot_id=fake_pool.messages[mid]["bot_id"],
        topic_id=fake_pool.messages[mid]["topic_id"],
        failure_class="retryable_pre_send",
        failure_reason="llm_timeout",
    )
    m = fake_pool.messages[mid]
    for col in (
        "processing_state",
        "failure_class",
        "next_retry_at",
        "processing_attempts",
        "handled_by_turn_id",
    ):
        assert col in m, f"missing lifecycle column: {col}"
    assert m["processing_state"] == "failed"
    assert m["failure_class"] == "retryable_pre_send"
    assert m["next_retry_at"] is not None
    assert m["processing_attempts"] == 2
    assert m["handled_by_turn_id"] == turn_id


async def test_post_send_phase_cap_turn_is_not_picked_up_by_recovery(
    fake_pool,
) -> None:
    """Regression for Project A2 / FLAG-A2-correctness-1.

    A turn that hit a post-send (record / schedule) phase cap looks like a
    SUCCESSFUL replied turn at the bot_turns level:

      completed_at IS NOT NULL
      final_output_message_id IS NOT NULL
      failure_reason IS NULL

    The cap signal lives only in turn_audit_events.  Such a turn MUST NOT
    be picked up by recovery's crashed-turn requeue path
    (``WHERE failure_reason='crashed' AND completed_at IS NULL ...``).
    """
    user = _seed_user(fake_pool)
    ids = [_seed_message(fake_pool, user)]
    outbound_id = uuid4()
    fake_pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": user.id,
        "content": "reply",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC) - timedelta(minutes=6),
        "charge": None,
        "deleted_at": None,
    }
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "triggering_message_ids": ids,
        "user_in_context": user.id,
        "started_at": datetime.now(UTC) - timedelta(minutes=6),
        "completed_at": datetime.now(UTC) - timedelta(minutes=5),
        "failure_reason": None,  # NOT set — post-send cap is audit-only
        "reasoning": "",
        "final_output_message_id": outbound_id,
        "bot_id": "mediator",
        "topic_id": get_relationship_topic_id(),
    }
    # Simulate the audit-event row that the cap path writes.
    fake_pool.turn_audit_events.append(
        {
            "turn_id": turn_id,
            "event_type": "phase_cap.post_send_exceeded",
            "step": "record",
            "severity": "warning",
            "metadata": {"cap": 8, "tool_iteration_count": 9},
        }
    )
    coalescer = CoalescerRecorder()

    await recover_on_startup(fake_pool, _ready_registry(mediator=coalescer))

    # Recovery must NOT touch this turn: failure_reason stays NULL, no requeue.
    assert fake_pool.bot_turns[turn_id]["failure_reason"] is None
    assert coalescer.add_burst_calls == []
    assert coalescer.add_calls == []
