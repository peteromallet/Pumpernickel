import asyncio
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.debouncer import BurstCoalescer
from app.services.pacer import PacingDecision
from tests._scope_helpers import make_resolved_scope


pytestmark = pytest.mark.anyio


async def test_rapid_messages_coalesce_to_one_burst() -> None:
    calls = []

    async def callback(message_ids, user, *, scope):
        calls.append((message_ids, user))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    coalescer = BurstCoalescer(callback, debounce_seconds=0.01, max_seconds=0.1)
    ids = [uuid4() for _ in range(5)]
    for message_id in ids:
        await coalescer.add(user.id, message_id, user, scope=scope)

    await asyncio.sleep(0.03)
    assert calls == [(ids, user)]


async def test_max_window_forces_second_burst() -> None:
    calls = []

    async def callback(message_ids, user, *, scope):
        calls.append(message_ids)

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    coalescer = BurstCoalescer(callback, debounce_seconds=0.04, max_seconds=0.06)
    first = [uuid4(), uuid4()]
    await coalescer.add(user.id, first[0], user, scope=scope)
    await asyncio.sleep(0.03)
    await coalescer.add(user.id, first[1], user, scope=scope)
    await asyncio.sleep(0.05)
    second = uuid4()
    await coalescer.add(user.id, second, user, scope=scope)
    await asyncio.sleep(0.06)

    assert calls == [first, [second]]


async def test_add_burst_fires_callback_with_supplied_user() -> None:
    calls = []

    async def callback(message_ids, user, *, scope):
        calls.append((message_ids, user))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    coalescer = BurstCoalescer(callback)
    ids = [uuid4(), uuid4()]

    await coalescer.add_burst(user.id, ids, user, scope=scope)

    assert calls == [(ids, user)]


class _FakePacer:
    def __init__(self, decisions, *, pool=None) -> None:
        self.decisions = list(decisions)
        self.calls = []
        self.pool = pool

    async def decide_and_record(self, user, message_ids, *, source: str):
        self.calls.append((user, list(message_ids), source))
        return self.decisions.pop(0)


def _seed_raw_message(fake_pool, user: User, *, bot_id: str = "mediator", topic_id: UUID | None = None):
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "ok",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": f"wa-{message_id}",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "bot_id": bot_id,
        "topic_id": topic_id,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
        # 0041 inbound queue metadata
        "handled_at": None,
        "handled_by_turn_id": None,
        "handling_result": None,
        "processing_started_at": None,
        "processing_error": None,
        "processing_attempts": 0,
    }
    return message_id


async def test_paced_answer_callback_receives_decision_and_source() -> None:
    legacy_calls = []
    paced_calls = []

    async def legacy_callback(message_ids, user, *, scope):
        legacy_calls.append((message_ids, user))

    async def paced_answer(message_ids, user, decision, *, scope):
        paced_calls.append((message_ids, user, decision))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    decision = PacingDecision(action="answer", reason="ready")
    pacer = _FakePacer([decision])
    coalescer = BurstCoalescer(
        legacy_callback,
        debounce_seconds=0.01,
        max_seconds=0.1,
        pacer=pacer,
        on_paced_answer=paced_answer,
    )
    message_id = uuid4()

    await coalescer.add(user.id, message_id, user, source="catch_up", scope=scope)
    await asyncio.sleep(0.03)

    assert legacy_calls == []
    assert paced_calls == [([message_id], user, decision)]
    assert pacer.calls == [(user, [message_id], "catch_up")]


async def test_live_typing_starts_during_coalescing_and_stops_before_answer() -> None:
    paced_calls = []
    typing_calls = []

    async def legacy_callback(message_ids, user, *, scope):
        raise AssertionError("paced answer callback should be used")

    async def paced_answer(message_ids, user, decision, *, scope):
        paced_calls.append((message_ids, user, decision))

    async def live_typing(user, stop_event, *, scope):
        typing_calls.append((user, stop_event.is_set()))
        await stop_event.wait()
        typing_calls.append((user, stop_event.is_set()))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    decision = PacingDecision(action="answer", reason="ready")
    pacer = _FakePacer([decision])
    coalescer = BurstCoalescer(
        legacy_callback,
        debounce_seconds=0.01,
        max_seconds=0.1,
        pacer=pacer,
        on_paced_answer=paced_answer,
        on_live_typing=live_typing,
    )
    message_id = uuid4()

    await coalescer.add(user.id, message_id, user, source="live", scope=scope)
    await asyncio.sleep(0.03)

    assert typing_calls == [(user, False), (user, True)]
    assert paced_calls == [([message_id], user, decision)]


@pytest.mark.parametrize(
    ("sources", "expected_source"),
    [
        (["live", "catch_up"], "catch_up"),
        (["live", "recovery"], "recovery"),
        (["live", "media"], "media"),
        (["media", "catch_up"], "catch_up"),
    ],
)
async def test_paced_burst_preserves_high_safety_source_semantics(sources: list[str], expected_source: str) -> None:
    async def legacy_callback(message_ids, user, *, scope):
        raise AssertionError("paced answer callback should be used")

    async def paced_answer(message_ids, user, decision, *, scope):
        return None

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    ids = [uuid4() for _ in sources]
    decision = PacingDecision(action="answer", reason="ready")
    pacer = _FakePacer([decision])
    coalescer = BurstCoalescer(
        legacy_callback,
        debounce_seconds=0.01,
        max_seconds=0.1,
        pacer=pacer,
        on_paced_answer=paced_answer,
    )

    for message_id, source in zip(ids, sources, strict=True):
        await coalescer.add(user.id, message_id, user, source=source, scope=scope)
    await asyncio.sleep(0.03)

    assert pacer.calls == [(user, ids, expected_source)]


async def test_paced_wait_reschedules_without_losing_message_ids() -> None:
    paced_calls = []
    outcome_calls = []

    async def callback(message_ids, user, *, scope):
        paced_calls.append((message_ids, user))

    async def paced_answer(message_ids, user, decision, *, scope):
        paced_calls.append((message_ids, user, decision))

    async def paced_ready(message_ids, user, *, scope):
        outcome_calls.append((message_ids, user, scope))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    first_decision = PacingDecision(action="wait", reason="still composing", wait_s=0.01)
    second_decision = PacingDecision(action="answer", reason="ready")
    pacer = _FakePacer([first_decision, second_decision])
    coalescer = BurstCoalescer(
        callback,
        debounce_seconds=0.01,
        max_seconds=0.1,
        pacer=pacer,
        on_paced_answer=paced_answer,
        on_paced_ready=paced_ready,
    )
    ids = [uuid4(), uuid4()]

    await coalescer.add(user.id, ids[0], user, scope=scope)
    await coalescer.add(user.id, ids[1], user, scope=scope)
    await asyncio.sleep(0.06)

    assert paced_calls == [(ids, user, second_decision)]
    assert outcome_calls == [(ids, user, scope), (ids, user, scope)]
    assert [call[1] for call in pacer.calls] == [ids, ids]


@pytest.mark.parametrize("action", ["answer", "react", "silence"])
async def test_terminal_paced_ready_callback_runs_before_each_outcome(action: str) -> None:
    outcome_calls = []
    dispatch_order = []

    async def callback(message_ids, user, *, scope):
        raise AssertionError("paced callback should be used")

    async def paced_answer(message_ids, user, decision, *, scope):
        dispatch_order.append("answer")

    async def paced_reaction(message_ids, user, decision, *, scope):
        dispatch_order.append("react")

    async def paced_ready(message_ids, user, *, scope):
        outcome_calls.append((message_ids, user, scope))
        dispatch_order.append("ready")

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    decision = PacingDecision(
        action=action,
        reason=action,
        reaction="👍" if action == "react" else None,
    )
    coalescer = BurstCoalescer(
        callback,
        debounce_seconds=0.01,
        max_seconds=0.1,
        pacer=_FakePacer([decision]),
        on_paced_answer=paced_answer,
        on_paced_reaction=paced_reaction,
        on_paced_ready=paced_ready,
    )
    message_id = uuid4()

    await coalescer.add(user.id, message_id, user, scope=scope)
    await asyncio.sleep(0.03)

    assert outcome_calls == [([message_id], user, scope)]
    assert dispatch_order == (
        ["ready", action] if action in {"answer", "react"} else ["ready"]
    )


@pytest.mark.parametrize("action", ["react", "silence"])
async def test_paced_react_or_silence_marks_processed_without_agentic_turn(fake_pool, action: str) -> None:
    answer_calls = []
    reaction_calls = []

    async def callback(message_ids, user, *, scope):
        answer_calls.append((message_ids, user))

    async def paced_reaction(message_ids, user, decision, *, scope):
        reaction_calls.append((message_ids, user, decision))

    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    scope = make_resolved_scope(user_id=user.id)
    fake_pool.users[user.id] = {"id": user.id, "name": user.name, "phone": user.phone, "timezone": user.timezone}
    message_id = _seed_raw_message(fake_pool, user, bot_id=scope.bot_id, topic_id=scope.topic_id)
    decision = PacingDecision(action=action, reason=action, reaction="👍" if action == "react" else None)
    pacer = _FakePacer([decision], pool=fake_pool)
    coalescer = BurstCoalescer(
        callback,
        debounce_seconds=0.01,
        max_seconds=0.1,
        pacer=pacer,
        on_paced_reaction=paced_reaction,
    )

    await coalescer.add(user.id, message_id, user, scope=scope)
    await asyncio.sleep(0.03)

    assert answer_calls == []
    assert fake_pool.messages[message_id]["processing_state"] == "processed"
    if action == "react":
        assert reaction_calls == [([message_id], user, decision)]
    else:
        assert reaction_calls == []
