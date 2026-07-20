"""Tests for app/reflections/finalization.py — deterministic finalization rules.

Covers:
  - Explicit completion: user messages that signal session end.
  - Topic transition: messages that shift to a new topic.
  - Race-safe inactivity: auto-finalization when idle_finalize_at passes.
  - Late messages: messages arriving after session is already closed.
  - Abandoned sessions: forced abandonment, extended inactivity, safety valve.
  - Retry idempotency: calling finalize on already-terminal sessions.
  - Composite evaluation: evaluate_full priority ordering.
  - Edge cases: empty text, missing timestamps, multiple transitions.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.reflections.finalization import (
    FinalizationAction,
    FinalizationDecision,
    FinalizationEngine,
    SessionState,
    build_session_state,
    compute_idle_deadline,
    is_explicit_completion,
    is_idempotent_finalize,
    is_topic_transition,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "bot-1",
    status: str = "collecting",
    source_message_ids: list[UUID] | None = None,
    opened_at: datetime | None = None,
    idle_finalize_at: datetime | None = None,
    finalized_at: datetime | None = None,
    abandoned_at: datetime | None = None,
    topic_id: UUID | None = None,
    phase: str = "freeform",
) -> SessionState:
    return build_session_state(
        session_id=session_id or _uid(),
        user_id=user_id or _uid(),
        bot_id=bot_id,
        status=status,
        source_message_ids=source_message_ids or [],
        opened_at=opened_at,
        idle_finalize_at=idle_finalize_at,
        finalized_at=finalized_at,
        abandoned_at=abandoned_at,
        topic_id=topic_id,
        phase=phase,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Explicit completion tests
# ═══════════════════════════════════════════════════════════════════════════


class TestExplicitCompletion:
    """Tests for is_explicit_completion() and evaluate_explicit_completion()."""

    # ── Positive matches ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "text",
        [
            "end reflection",
            "finish reflection",
            "wrap up reflection",
            "wrap it up reflection",
            "close reflection",
            "stop reflection",
            "done reflection",
            "complete reflection",
            "end session",
            "finish session",
            "wrap up session",
            "close this session",
            "end this",
            "finish here",
            "close now",
            "that's all",
            "that's it",
            "that's everything",
            "I'm done",
            "all done",
            "nothing else",
            "no more",
            "finalize",
            "close out",
            "sign off",
            "signing off",
            "end",
            "done",
        ],
    )
    def test_positive_matches(self, text: str) -> None:
        """All these texts should be detected as explicit completions."""
        assert is_explicit_completion(text), f"'{text}' should match"

    # ── Negative matches ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "hello world",
            "I'm thinking about the end of the movie",
            "finish the project",
            "close the door",
            "wrap the gift",
            "end of the line is a song",
            "how do I complete this task",
            "what a beautiful ending",
            "I want to finish my homework",
            "done with the dishes",
            "that's a good idea",
            "all good here",
        ],
    )
    def test_negative_matches(self, text: str) -> None:
        """These texts should NOT be detected as explicit completions."""
        assert not is_explicit_completion(text), f"'{text}' should NOT match"

    # ── Engine evaluation ───────────────────────────────────────────────

    def test_evaluate_collecting_session_with_completion(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_explicit_completion(
            session=session, message_text="end reflection"
        )
        assert decision.action == "finalize"
        assert decision.session_id == session.session_id
        assert "explicit completion" in decision.reason.lower()

    def test_evaluate_collecting_session_without_completion(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_explicit_completion(
            session=session, message_text="hello world"
        )
        assert decision.action == "noop"

    def test_evaluate_non_collecting_session(self) -> None:
        engine = FinalizationEngine()
        for status in ("finalizing", "processed", "abandoned", "processing_failed"):
            session = _state(status=status)
            decision = engine.evaluate_explicit_completion(
                session=session, message_text="end reflection"
            )
            assert decision.action == "noop", f"status={status} should be noop"
            assert "not collecting" in decision.reason.lower()

    def test_evaluate_empty_text(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_explicit_completion(
            session=session, message_text=""
        )
        assert decision.action == "noop"

    def test_evaluate_whitespace_only_text(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_explicit_completion(
            session=session, message_text="   \n\t  "
        )
        assert decision.action == "noop"


# ═══════════════════════════════════════════════════════════════════════════
# Topic transition tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTopicTransition:
    """Tests for is_topic_transition() and evaluate_topic_transition()."""

    # ── Positive matches ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "text",
        [
            "now let's talk about something else",
            "moving on to a new topic",
            "new topic: project planning",
            "different topic entirely",
            "switching gears now",
            "switch topics",
            "change of subject",
            "okay so here is the thing",
            "alright so about the budget",
            "anyway another thing",
            "so about yesterday",
            "let's talk about the deadline",
            "let's discuss the proposal",
            "let's focus on the design",
        ],
    )
    def test_positive_matches(self, text: str) -> None:
        assert is_topic_transition(text), f"'{text}' should match"

    # ── Negative matches ────────────────────────────────────────────────

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "   ",
            "hello world",
            "I feel good today",
            "reflection on my day",
            "ok thanks",
            "alright I understand",
            "so what do you think",
            "let's go to the park",
            "moving the furniture",
            "I switched jobs",
            "a change of clothes",
        ],
    )
    def test_negative_matches(self, text: str) -> None:
        assert not is_topic_transition(text), f"'{text}' should NOT match"

    # ── Engine evaluation ───────────────────────────────────────────────

    def test_topic_transition_finalizes_collecting(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_topic_transition(
            session=session, message_text="moving on to a new topic"
        )
        assert decision.action == "finalize"

    def test_no_topic_transition_noop(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_topic_transition(
            session=session, message_text="I feel good today"
        )
        assert decision.action == "noop"

    def test_explicit_topic_id_change(self) -> None:
        engine = FinalizationEngine()
        old_topic = _uid()
        new_topic = _uid()
        session = _state(status="collecting", topic_id=old_topic)
        decision = engine.evaluate_topic_transition(
            session=session,
            message_text="hello",
            new_topic_id=new_topic,
        )
        assert decision.action == "finalize"
        assert "topic transition" in decision.reason.lower()

    def test_same_topic_id_no_transition(self) -> None:
        engine = FinalizationEngine()
        topic = _uid()
        session = _state(status="collecting", topic_id=topic)
        decision = engine.evaluate_topic_transition(
            session=session,
            message_text="hello",
            new_topic_id=topic,
        )
        assert decision.action == "noop"

    def test_topic_id_change_without_existing_topic(self) -> None:
        """If session has no topic_id, a new one doesn't trigger transition."""
        engine = FinalizationEngine()
        session = _state(status="collecting", topic_id=None)
        decision = engine.evaluate_topic_transition(
            session=session,
            message_text="hello",
            new_topic_id=_uid(),
        )
        assert decision.action == "noop"

    def test_non_collecting_session_noop(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="finalizing")
        decision = engine.evaluate_topic_transition(
            session=session, message_text="moving on"
        )
        assert decision.action == "noop"


# ═══════════════════════════════════════════════════════════════════════════
# Inactivity tests
# ═══════════════════════════════════════════════════════════════════════════


class TestInactivity:
    """Tests for evaluate_inactivity() — race-safe idle timeout."""

    def test_idle_deadline_passed_finalize(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        deadline = now - timedelta(seconds=60)
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "finalize"
        assert "idle deadline passed" in decision.reason.lower()

    def test_idle_deadline_not_passed_noop(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        deadline = now + timedelta(seconds=3600)  # 1 hour in future
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "noop"

    def test_no_idle_deadline_uses_opened_at(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        opened = now - timedelta(seconds=1000)  # > 15 min default
        session = _state(
            status="collecting",
            idle_finalize_at=None,
            opened_at=opened,
        )
        engine.DEFAULT_IDLE_TIMEOUT_SECONDS = 900
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "finalize"

    def test_no_idle_deadline_recently_opened_noop(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        opened = now - timedelta(seconds=60)  # only 1 min old
        session = _state(
            status="collecting",
            idle_finalize_at=None,
            opened_at=opened,
        )
        engine.DEFAULT_IDLE_TIMEOUT_SECONDS = 900
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "noop"

    def test_no_idle_no_opened_at_noop(self) -> None:
        engine = FinalizationEngine()
        session = _state(
            status="collecting",
            idle_finalize_at=None,
            opened_at=None,
        )
        decision = engine.evaluate_inactivity(session=session, now=_now())
        assert decision.action == "noop"
        assert "cannot compute deadline" in decision.reason.lower()

    def test_non_collecting_session_noop(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        deadline = now - timedelta(hours=1)
        session = _state(
            status="finalizing",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "noop"

    def test_extended_inactivity_abandons(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        # Deadline passed long ago — beyond abandon threshold
        deadline = now - timedelta(seconds=engine.DEFAULT_ABANDON_TIMEOUT_SECONDS + 60)
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "abandon"

    def test_exact_deadline_boundary(self) -> None:
        """At exact deadline moment, it should still be finalize (now >= deadline)."""
        engine = FinalizationEngine()
        now = _now()
        session = _state(
            status="collecting",
            idle_finalize_at=now,  # exactly now
        )
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "finalize"

    def test_one_second_before_deadline_noop(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        deadline = now + timedelta(seconds=1)
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "noop"

    def test_one_second_past_deadline_finalize(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        deadline = now - timedelta(seconds=1)
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_inactivity(session=session, now=now)
        assert decision.action == "finalize"


# ═══════════════════════════════════════════════════════════════════════════
# Late message tests
# ═══════════════════════════════════════════════════════════════════════════


class TestLateMessages:
    """Tests for evaluate_late_message() — messages after session closed."""

    def test_collecting_session_not_late(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_late_message(
            session=session,
            message_text="reflection on my day",
            is_reflection_candidate=True,
        )
        assert decision.action == "noop"
        assert "still collecting" in decision.reason.lower()

    def test_finalized_session_reflection_candidate(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="finalizing")
        decision = engine.evaluate_late_message(
            session=session,
            message_text="reflection on my day",
            is_reflection_candidate=True,
        )
        assert decision.action == "open_new_for_late"

    def test_finalized_session_non_reflection(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="finalizing")
        decision = engine.evaluate_late_message(
            session=session,
            message_text="hello",
            is_reflection_candidate=False,
        )
        assert decision.action == "skip_late"

    @pytest.mark.parametrize("status", ["processed", "abandoned", "processing_failed"])
    def test_non_collecting_statuses_reflection_candidate(self, status: str) -> None:
        engine = FinalizationEngine()
        session = _state(status=status)
        decision = engine.evaluate_late_message(
            session=session,
            message_text="reflection time",
            is_reflection_candidate=True,
        )
        assert decision.action == "open_new_for_late"

    @pytest.mark.parametrize("status", ["processed", "abandoned", "processing_failed"])
    def test_non_collecting_statuses_non_reflection(self, status: str) -> None:
        engine = FinalizationEngine()
        session = _state(status=status)
        decision = engine.evaluate_late_message(
            session=session,
            message_text="hello world",
            is_reflection_candidate=False,
        )
        assert decision.action == "skip_late"


# ═══════════════════════════════════════════════════════════════════════════
# Abandonment tests
# ═══════════════════════════════════════════════════════════════════════════


class TestAbandonment:
    """Tests for evaluate_abandon()."""

    def test_forced_abandon_collecting(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_abandon(session=session, force=True)
        assert decision.action == "abandon"
        assert "forced" in decision.reason.lower()

    def test_forced_abandon_non_collecting_noop(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="finalizing")
        decision = engine.evaluate_abandon(session=session, force=True)
        assert decision.action == "noop"

    def test_max_messages_safety_valve(self) -> None:
        engine = FinalizationEngine()
        msg_ids = [_uid() for _ in range(engine.MAX_SOURCE_MESSAGES)]
        session = _state(status="collecting", source_message_ids=msg_ids)
        decision = engine.evaluate_abandon(session=session)
        assert decision.action == "abandon"
        assert "max" in decision.reason.lower()

    def test_below_max_messages_noop(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="collecting", source_message_ids=[_uid()])
        decision = engine.evaluate_abandon(session=session)
        assert decision.action == "noop"

    def test_extended_idle_abandons(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        very_old_deadline = now - timedelta(
            seconds=engine.DEFAULT_ABANDON_TIMEOUT_SECONDS + 3600
        )
        session = _state(
            status="collecting",
            idle_finalize_at=very_old_deadline,
        )
        decision = engine.evaluate_abandon(session=session, now=now)
        assert decision.action == "abandon"

    def test_idle_but_not_abandon_threshold_noop(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        recent_deadline = now - timedelta(seconds=60)
        session = _state(
            status="collecting",
            idle_finalize_at=recent_deadline,
        )
        decision = engine.evaluate_abandon(session=session, now=now)
        assert decision.action == "noop"


# ═══════════════════════════════════════════════════════════════════════════
# Idempotency tests
# ═══════════════════════════════════════════════════════════════════════════


class TestIdempotency:
    """Tests for is_idempotent_finalize() and retry safety."""

    @pytest.mark.parametrize(
        "status,expected",
        [
            ("collecting", False),
            ("finalizing", True),
            ("processed", True),
            ("processing_failed", True),
            ("abandoned", True),
        ],
    )
    def test_idempotent_statuses(self, status: str, expected: bool) -> None:
        session = _state(status=status)
        assert is_idempotent_finalize(session=session) == expected

    def test_engine_evaluate_on_already_finalized(self) -> None:
        """Calling evaluate_explicit_completion on finalized session returns noop."""
        engine = FinalizationEngine()
        session = _state(status="finalizing")
        decision = engine.evaluate_explicit_completion(
            session=session, message_text="end reflection"
        )
        assert decision.action == "noop"

    def test_engine_evaluate_on_already_abandoned(self) -> None:
        """Calling evaluate_inactivity on abandoned session returns noop."""
        engine = FinalizationEngine()
        session = _state(status="abandoned")
        decision = engine.evaluate_inactivity(session=session, now=_now())
        assert decision.action == "noop"

    def test_multiple_finalize_calls_same_result(self) -> None:
        """Calling finalize decisions twice on same state gives same result."""
        engine = FinalizationEngine()
        now = _now()
        deadline = now - timedelta(seconds=60)
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        d1 = engine.evaluate_inactivity(session=session, now=now)
        d2 = engine.evaluate_inactivity(session=session, now=now)
        assert d1.action == d2.action == "finalize"
        assert d1.reason == d2.reason


# ═══════════════════════════════════════════════════════════════════════════
# Composite evaluation tests
# ═══════════════════════════════════════════════════════════════════════════


class TestEvaluateFull:
    """Tests for evaluate_full() — composite priority-ordered evaluation."""

    def test_explicit_completion_wins_over_inactivity(self) -> None:
        """Explicit completion should fire even if idle deadline has passed."""
        engine = FinalizationEngine()
        now = _now()
        deadline = now - timedelta(hours=1)  # past deadline
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_full(
            session=session,
            message_text="end reflection now",
            now=now,
        )
        assert decision.action == "finalize"
        assert "explicit completion" in decision.reason.lower()

    def test_topic_transition_wins_over_inactivity(self) -> None:
        """Topic transition should fire even if idle deadline has passed."""
        engine = FinalizationEngine()
        now = _now()
        deadline = now - timedelta(hours=1)
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_full(
            session=session,
            message_text="moving on to a new topic",
            now=now,
        )
        assert decision.action == "finalize"
        assert "topic transition" in decision.reason.lower()

    def test_inactivity_when_no_message(self) -> None:
        """Without a message, inactivity becomes the deciding factor."""
        engine = FinalizationEngine()
        now = _now()
        deadline = now - timedelta(hours=1)
        session = _state(
            status="collecting",
            idle_finalize_at=deadline,
        )
        decision = engine.evaluate_full(session=session, now=now)
        assert decision.action == "finalize"
        assert "idle" in decision.reason.lower()

    def test_late_message_handled(self) -> None:
        """Session already finalized → late message check takes priority."""
        engine = FinalizationEngine()
        session = _state(status="finalizing")
        decision = engine.evaluate_full(
            session=session,
            message_text="reflection on my day",
            is_reflection_candidate=True,
        )
        assert decision.action == "open_new_for_late"

    def test_late_non_reflection_handled(self) -> None:
        engine = FinalizationEngine()
        session = _state(status="processed")
        decision = engine.evaluate_full(
            session=session,
            message_text="hello",
            is_reflection_candidate=False,
        )
        assert decision.action == "skip_late"

    def test_collecting_no_triggers_noop(self) -> None:
        engine = FinalizationEngine()
        now = _now()
        session = _state(
            status="collecting",
            idle_finalize_at=now + timedelta(hours=2),
        )
        decision = engine.evaluate_full(
            session=session,
            message_text="I feel good today",
            now=now,
        )
        assert decision.action == "noop"

    def test_max_messages_safety_valve_in_full(self) -> None:
        engine = FinalizationEngine()
        msg_ids = [_uid() for _ in range(engine.MAX_SOURCE_MESSAGES)]
        session = _state(status="collecting", source_message_ids=msg_ids)
        decision = engine.evaluate_full(session=session, now=_now())
        assert decision.action == "abandon"
        assert "max" in decision.reason.lower()

    def test_no_message_non_collecting_noop(self) -> None:
        """When session is non-collecting and no message, just noop."""
        engine = FinalizationEngine()
        session = _state(status="processed")
        decision = engine.evaluate_full(session=session)
        assert decision.action == "noop"


# ═══════════════════════════════════════════════════════════════════════════
# Helper function tests
# ═══════════════════════════════════════════════════════════════════════════


class TestComputeIdleDeadline:
    """Tests for compute_idle_deadline()."""

    def test_explicit_idle_finalize_at(self) -> None:
        deadline = _now() + timedelta(minutes=5)
        session = _state(idle_finalize_at=deadline)
        result = compute_idle_deadline(session=session)
        assert result == deadline

    def test_fallback_to_opened_at(self) -> None:
        opened = _now() - timedelta(minutes=5)
        session = _state(idle_finalize_at=None, opened_at=opened)
        result = compute_idle_deadline(session=session, default_timeout_seconds=600)
        expected = opened + timedelta(seconds=600)
        assert result == expected

    def test_no_idle_no_opened_returns_none(self) -> None:
        session = _state(idle_finalize_at=None, opened_at=None)
        result = compute_idle_deadline(session=session)
        assert result is None


class TestBuildSessionState:
    """Tests for build_session_state()."""

    def test_defaults(self) -> None:
        sid = _uid()
        uid = _uid()
        state = build_session_state(session_id=sid, user_id=uid, bot_id="b1")
        assert state.session_id == sid
        assert state.user_id == uid
        assert state.bot_id == "b1"
        assert state.status == "collecting"
        assert state.source_message_ids == []
        assert state.opened_at is None
        assert state.idle_finalize_at is None

    def test_full_state(self) -> None:
        sid = _uid()
        uid = _uid()
        opened = _now() - timedelta(hours=1)
        deadline = _now() + timedelta(minutes=15)
        msg_ids = [_uid(), _uid()]
        topic = _uid()
        state = build_session_state(
            session_id=sid,
            user_id=uid,
            bot_id="b2",
            status="collecting",
            source_message_ids=msg_ids,
            opened_at=opened,
            idle_finalize_at=deadline,
            finalized_at=None,
            abandoned_at=None,
            topic_id=topic,
            phase="retrospective",
        )
        assert state.session_id == sid
        assert state.user_id == uid
        assert state.bot_id == "b2"
        assert state.status == "collecting"
        assert state.source_message_ids == msg_ids
        assert state.opened_at == opened
        assert state.idle_finalize_at == deadline
        assert state.finalized_at is None
        assert state.abandoned_at is None
        assert state.topic_id == topic
        assert state.phase == "retrospective"


# ═══════════════════════════════════════════════════════════════════════════
# Edge case tests
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge cases and boundary behavior."""

    def test_rapid_explicit_then_inactivity(self) -> None:
        """Even after explicit completion, inactivity check on same session is noop."""
        engine = FinalizationEngine()
        session = _state(status="collecting")
        # First: explicit completion
        d1 = engine.evaluate_explicit_completion(
            session=session, message_text="end reflection"
        )
        assert d1.action == "finalize"
        # After finalization (simulated), inactivity should be noop
        session_finalized = _state(status="finalizing", session_id=session.session_id)
        d2 = engine.evaluate_inactivity(
            session=session_finalized,
            now=_now(),
        )
        assert d2.action == "noop"

    def test_competing_completion_and_transition(self) -> None:
        """Text matching both completion and transition patterns → completion wins."""
        text = "let's wrap up this reflection and move on to a new topic"
        assert is_explicit_completion(text)  # matches
        assert is_topic_transition(text)  # also matches
        # In evaluate_full, explicit completion is checked first
        engine = FinalizationEngine()
        session = _state(status="collecting")
        decision = engine.evaluate_full(session=session, message_text=text)
        assert decision.action == "finalize"
        assert "explicit completion" in decision.reason.lower()

    def test_very_long_message_text(self) -> None:
        """Long messages should still be checked for completion signals."""
        long_text = "blah " * 1000 + " end reflection"
        assert is_explicit_completion(long_text)

    def test_unicode_and_whitespace_variants(self) -> None:
        """Non-ASCII whitespace should be handled."""
        assert is_explicit_completion("\u00a0end reflection\u00a0")
        assert is_explicit_completion("end\u2003reflection")

    def test_case_insensitivity(self) -> None:
        assert is_explicit_completion("END REFLECTION")
        assert is_explicit_completion("End Reflection")
        assert is_explicit_completion("enD reFlecTion")
        assert is_topic_transition("MOVING ON to something new")
        assert is_topic_transition("Moving On")

    def test_punctuation_surrounding(self) -> None:
        assert is_explicit_completion("...end reflection...")
        assert is_explicit_completion("(end reflection)")
        assert is_explicit_completion('"end reflection"')
        assert is_topic_transition("so, moving on...")

    def test_abandon_with_no_idle_deadline_safety_valve_only(self) -> None:
        """Without idle_finalize_at, only safety valve triggers abandon."""
        engine = FinalizationEngine()
        session = _state(status="collecting", idle_finalize_at=None)
        decision = engine.evaluate_abandon(session=session, now=_now())
        assert decision.action == "noop"

    def test_evaluate_full_with_topic_transition_and_new_topic_id(self) -> None:
        """Both text and topic_id change should finalize."""
        engine = FinalizationEngine()
        old_topic = _uid()
        new_topic = _uid()
        session = _state(status="collecting", topic_id=old_topic)
        decision = engine.evaluate_full(
            session=session,
            message_text="moving on to new stuff",
            new_topic_id=new_topic,
        )
        assert decision.action == "finalize"
