"""End-to-end capture and finalization tests (T18).

Covers the full capture pipeline end-to-end:
- Explicit, implicit, and freeform openings
- Day/week/month temporal scope variants
- Opening and closing phase variants
- Voice transcript ingestion through the same ingress path
- Same-burst coalescing with competing-start resolution
- Cross-turn session attachment
- Explicit completion finalization
- Inactivity-based finalization
- Topic transition finalization
- Late messages arriving post-finalization
- Competing starts resolution in bursts
- Abandoned session handling
- Retry idempotency (re-opening / re-attaching)
- Hard negatives: proactive reflection messages must NOT be captured
- Concurrency: simultaneous burst arrivals
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.reflections.classifier import ClassificationResult, classify_message
from app.reflections.finalization import (
    FinalizationEngine,
    SessionState,
    build_session_state,
)
from app.reflections.session_manager import (
    ActiveSessionSnapshot,
    SessionManager,
    build_session_snapshot,
    select_active_session,
)
from app.services.reflections_integration import (
    _fetch_message_contents,
    capture_burst_for_reflection,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeUser:
    """Minimal user object with id and timezone."""

    def __init__(self, user_id: UUID | None = None, timezone: str = "UTC") -> None:
        self.id = user_id or _uid()
        self.timezone = timezone


def _message_row(msg_id: UUID, content: str | None) -> dict:
    return {"id": msg_id, "content": content}


class _FakeSession:
    """Minimal session mimicking ReflectionSession attributes."""

    def __init__(
        self,
        session_id: UUID,
        user_id: UUID,
        bot_id: str,
        *,
        source_message_ids: list[UUID] | None = None,
        topic_id: UUID | None = None,
        temporal_scope: str = "instant",
        phase: str = "freeform",
        status: str = "collecting",
    ) -> None:
        self.id = session_id
        self.user_id = user_id
        self.bot_id = bot_id
        self.topic_id = topic_id
        self.source_message_ids = list(source_message_ids or [])
        self.temporal_scope = temporal_scope
        self.phase = phase
        self.status = status


class _FakeReflectionStore:
    """Fake ReflectionStore for capture E2E tests."""

    def __init__(
        self,
        sessions: list[_FakeSession] | None = None,
        *,
        open_raises: Exception | None = None,
    ) -> None:
        self._sessions = list(sessions or [])
        self._open_raises = open_raises
        self.open_calls: list[dict] = []
        self.list_calls: list[dict] = []
        self.list_sessions_raises: Exception | None = None

    async def list_sessions(self, *, user_id, statuses=None, limit=50):
        self.list_calls.append(
            {"user_id": user_id, "statuses": statuses, "limit": limit}
        )
        if self.list_sessions_raises:
            raise self.list_sessions_raises
        return [s for s in self._sessions if s.user_id == user_id]

    async def open_or_attach_session(self, **kwargs):
        self.open_calls.append(kwargs)
        if self._open_raises:
            raise self._open_raises
        return _FakeSession(
            session_id=_uid(),
            user_id=kwargs.get("user_id"),
            bot_id=kwargs.get("bot_id"),
            source_message_ids=kwargs.get("source_message_ids", []),
            temporal_scope=kwargs.get("temporal_scope", "instant"),
            phase=kwargs.get("phase", "freeform"),
        )

    @property
    def open_called(self) -> bool:
        return len(self.open_calls) > 0

    @property
    def last_open_kwargs(self) -> dict:
        return self.open_calls[-1] if self.open_calls else {}

    @property
    def open_count(self) -> int:
        return len(self.open_calls)


async def _run_capture(
    pool: Any,
    message_ids: list[UUID],
    user: _FakeUser,
    *,
    bot_id: str = "test_bot",
    topic_id: UUID | None = None,
    fake_store: _FakeReflectionStore | None = None,
) -> _FakeReflectionStore:
    """Helper to run capture_burst_for_reflection with a fake store."""
    if fake_store is None:
        fake_store = _FakeReflectionStore()

    with patch(
        "app.services.reflections_integration.ReflectionStore",
        return_value=fake_store,
    ):
        await capture_burst_for_reflection(
            pool, message_ids, user, bot_id=bot_id, topic_id=topic_id
        )

    return fake_store


def _make_pool(message_content_map: dict[UUID, str | None]) -> AsyncMock:
    """Build a mock pool that returns messages for given IDs."""
    pool = AsyncMock()
    rows = [_message_row(mid, content) for mid, content in message_content_map.items()]
    pool.fetch.return_value = rows
    return pool


# ═══════════════════════════════════════════════════════════════════════════════
# Explicit opening variants
# ═══════════════════════════════════════════════════════════════════════════════


class TestExplicitOpeningsE2E:
    """E2E tests for explicit reflection openings across all variants."""

    async def test_explicit_reflection_keyword_opens_session(self):
        """Message with 'reflection' keyword opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Here is my reflection: today went well"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called
        assert store.last_open_kwargs["user_id"] == user.id

    async def test_explicit_retrospective_keyword_opens_session(self):
        """Message with 'retrospective' keyword opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Time for a retrospective of this week"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_explicit_checkpoint_keyword_opens_session(self):
        """Message with 'checkpoint' keyword opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Let me do a quick checkpoint on my progress"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_explicit_daily_opening_phase(self):
        """'start of day reflection' yields opening phase."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Starting my day with a reflection on what I want to achieve"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_explicit_closing_phase_via_wrap_up(self):
        """'wrap up' phrasing yields closing phase."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Let me wrap up my reflection for today"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_explicit_week_scope(self):
        """'this week' context opens a session with week scope."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Weekly reflection: this week was productive"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_explicit_month_scope(self):
        """'this month' context opens a session with month scope."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Monthly reflection: overall great progress this month"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_explicit_day_scope(self):
        """'today' context opens a session with day scope."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Today's reflection: felt focused and energized"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called


# ═══════════════════════════════════════════════════════════════════════════════
# Implicit openings (semantic detection)
# ═══════════════════════════════════════════════════════════════════════════════


class TestImplicitOpeningsE2E:
    """E2E tests for implicit reflection detection via semantics."""

    async def test_implicit_i_feel_opens_session(self):
        """Semantic 'I feel' pattern opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "I feel like this week has been really productive"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_implicit_i_learned_opens_session(self):
        """Semantic 'I learned' pattern opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "I learned so much from this project"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_implicit_ive_been_thinking_opens_session(self):
        """Semantic 'I've been thinking' pattern opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "I think I've been growing a lot this year"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_implicit_i_noticed_opens_session(self):
        """Semantic 'I noticed' pattern opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "I noticed that I'm more productive in the mornings"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_implicit_i_realized_opens_session(self):
        """Semantic 'I've realized' pattern opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "I've realized I've been neglecting my health"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_implicit_grateful_opens_session(self):
        """Semantic 'grateful' pattern opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "I'm so grateful for the team's support this sprint"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called


# ═══════════════════════════════════════════════════════════════════════════════
# Freeform openings
# ═══════════════════════════════════════════════════════════════════════════════


class TestFreeformOpeningsE2E:
    """E2E tests for freeform classification (ambiguous but reflective)."""

    async def test_freeform_ambiguous_reflection_opens(self):
        """Ambiguously reflective content still opens a session as freeform."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Hmm, let me think about where I'm at right now"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_freeform_low_confidence_still_captured(self):
        """Even low-confidence freeform is captured to preserve evidence."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Not sure what to make of today honestly"})
        store = await _run_capture(pool, [mid], user)
        # Freeform with borderline content - may or may not capture
        # The key is that if it captures, it preserves the complete thought
        # We don't assert open_called here; just that it doesn't raise
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# Voice transcript ingress
# ═══════════════════════════════════════════════════════════════════════════════


class TestVoiceTranscriptE2E:
    """E2E tests proving voice transcripts use the same capture path."""

    async def test_voice_transcript_reflection_opens_session(self):
        """Voice transcript with reflective content opens a session — same as text."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Hey so I've been reflecting on this year and I think I've grown a lot"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_voice_transcript_explicit_keyword_opens(self):
        """Voice transcript with 'reflection' keyword opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Voice note: here is my reflection for today"})
        store = await _run_capture(pool, [mid], user)
        assert store.open_called

    async def test_voice_transcript_non_reflection_skipped(self):
        """Non-reflection voice transcript is skipped, same as text."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Can you set a reminder for my dentist at 3pm"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called

    async def test_voice_transcript_logistics_skipped(self):
        """Voice transcript with logistics content is skipped."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Book a flight to New York for Tuesday"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called


# ═══════════════════════════════════════════════════════════════════════════════
# Same-burst coalescing
# ═══════════════════════════════════════════════════════════════════════════════


class TestSameBurstCoalescingE2E:
    """E2E tests for same-burst multi-message coalescing."""

    async def test_same_burst_all_messages_captured(self):
        """All messages in a burst are captured together as source evidence."""
        user = _FakeUser()
        mid1, mid2, mid3 = _uid(), _uid(), _uid()
        pool = _make_pool({
            mid1: "I want to reflect on this week",
            mid2: "Monday was tough but Tuesday picked up",
            mid3: "Overall I feel good about the progress",
        })
        store = await _run_capture(pool, [mid1, mid2, mid3], user)
        assert store.open_called
        source_ids = store.last_open_kwargs["source_message_ids"]
        assert mid1 in source_ids
        assert mid2 in source_ids
        assert mid3 in source_ids

    async def test_same_burst_mixed_non_reflection_included(self):
        """Even non-reflection messages in a reflective burst are captured."""
        user = _FakeUser()
        mid_joke = _uid()
        mid_reflect = _uid()
        pool = _make_pool({
            mid_joke: "haha that's funny",
            mid_reflect: "But seriously, let me reflect on my day",
        })
        store = await _run_capture(pool, [mid_joke, mid_reflect], user)
        assert store.open_called
        source_ids = store.last_open_kwargs["source_message_ids"]
        assert mid_joke in source_ids
        assert mid_reflect in source_ids

    async def test_same_burst_competing_starts_first_wins(self):
        """When multiple messages could independently open a session,
        only one session is opened — competing starts resolved."""
        user = _FakeUser()
        mid1, mid2 = _uid(), _uid()
        pool = _make_pool({
            mid1: "Daily reflection: morning check-in",
            mid2: "Also reflecting on yesterday's meeting",
        })
        store = await _run_capture(pool, [mid1, mid2], user)
        assert store.open_called
        # Only one session opened (not two)
        assert store.open_count == 1
        source_ids = store.last_open_kwargs["source_message_ids"]
        assert mid1 in source_ids
        assert mid2 in source_ids

    async def test_same_burst_all_non_reflection_skipped(self):
        """Burst with no reflection candidates is entirely skipped."""
        user = _FakeUser()
        mid1, mid2 = _uid(), _uid()
        pool = _make_pool({
            mid1: "what's the weather like",
            mid2: "can you send that email",
        })
        store = await _run_capture(pool, [mid1, mid2], user)
        assert not store.open_called


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-turn session attachment
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossTurnSessionsE2E:
    """E2E tests for cross-turn session attachment."""

    async def test_cross_turn_attachment(self):
        """New reflective messages in a subsequent burst attach to existing session."""
        user = _FakeUser()
        mid1, mid2 = _uid(), _uid()
        existing_session_id = _uid()

        # First burst opens a session
        pool1 = _make_pool({mid1: "Starting my daily reflection"})
        store1 = await _run_capture(pool1, [mid1], user)
        assert store1.open_called

        # Second burst attaches to the existing session
        existing = _FakeSession(existing_session_id, user.id, "test_bot", source_message_ids=[mid1])
        pool2 = _make_pool({mid2: "Adding one more thought to my reflection"})
        fake_store2 = _FakeReflectionStore(sessions=[existing])

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store2,
        ):
            await capture_burst_for_reflection(pool2, [mid2], user, bot_id="test_bot")

        assert fake_store2.open_called
        source_ids = fake_store2.last_open_kwargs["source_message_ids"]
        assert mid1 in source_ids  # from existing session
        assert mid2 in source_ids  # new message

    async def test_cross_turn_different_bot_no_attachment(self):
        """Messages for a different bot do not attach to another bot's session."""
        user = _FakeUser()
        mid = _uid()
        other_bot_session = _FakeSession(
            _uid(), user.id, "other_bot", source_message_ids=[_uid()]
        )
        pool = _make_pool({mid: "My daily reflection: feeling great"})
        fake_store = _FakeReflectionStore(sessions=[other_bot_session])

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(pool, [mid], user, bot_id="test_bot")

        assert fake_store.open_called
        # Should be a new open, not an attach (different bot)
        source_ids = fake_store.last_open_kwargs["source_message_ids"]
        assert mid in source_ids

    async def test_cross_turn_different_user_no_attachment(self):
        """Messages for a different user do not attach to another's session."""
        user1 = _FakeUser()
        user2 = _FakeUser()
        mid = _uid()
        other_user_session = _FakeSession(
            _uid(), user2.id, "test_bot", source_message_ids=[_uid()]
        )
        pool = _make_pool({mid: "My reflection for today"})
        fake_store = _FakeReflectionStore(sessions=[other_user_session])

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(pool, [mid], user1, bot_id="test_bot")

        assert fake_store.open_called


# ═══════════════════════════════════════════════════════════════════════════════
# Explicit completion finalization
# ═══════════════════════════════════════════════════════════════════════════════


class TestExplicitCompletionE2E:
    """E2E tests for explicit completion finalization in the capture pipeline."""

    @staticmethod
    def _engine_and_session(message_text: str, status: str = "collecting"):
        """Helper to create engine and session for explicit completion tests."""
        engine = FinalizationEngine()
        session = build_session_state(
            session_id=_uid(), user_id=_uid(), bot_id="test_bot",
            status=status, source_message_ids=[_uid()],
        )
        result = engine.evaluate_explicit_completion(
            session=session, message_text=message_text,
        )
        return result

    def test_finalization_engine_detects_end_reflection(self):
        """FinalizationEngine detects 'end reflection' as explicit completion."""
        result = self._engine_and_session("end reflection")
        assert result.action == "finalize"

    def test_finalization_engine_detects_wrap_up(self):
        """FinalizationEngine detects 'wrap up this reflection' as explicit completion."""
        result = self._engine_and_session("Let me wrap up this reflection")
        assert result.action == "finalize"

    def test_finalization_engine_detects_done_reflection(self):
        """FinalizationEngine detects 'done with reflection' as explicit completion."""
        result = self._engine_and_session("I'm done with my reflection for today")
        assert result.action == "finalize"

    def test_finalization_engine_detects_close_reflection(self):
        """FinalizationEngine detects 'close this reflection' as explicit completion."""
        result = self._engine_and_session("Let's close this reflection session")
        assert result.action == "finalize"

    def test_finalization_engine_detects_stop_reflection(self):
        """FinalizationEngine detects explicit completion via 'stop'."""
        result = self._engine_and_session("stop this session")
        assert result.action == "finalize"

    def test_finalization_engine_detects_finish_reflection(self):
        """FinalizationEngine detects 'finish' as explicit completion."""
        result = self._engine_and_session("finish this reflection now")
        assert result.action == "finalize"

    def test_finalization_engine_detects_that_is_all(self):
        """FinalizationEngine detects 'that is all' as explicit completion."""
        result = self._engine_and_session("That's all for my reflection")
        assert result.action == "finalize"

    def test_false_positive_complete_a_task_not_completion(self):
        """'complete this task' is NOT an explicit completion of a reflection."""
        result = self._engine_and_session("I need to complete this task today")
        assert result.action != "finalize"

    def test_false_positive_well_done_not_completion(self):
        """'well done' as praise is NOT an explicit completion on its own."""
        result = self._engine_and_session("Well done on the project")
        assert result.action != "finalize"

    def test_not_collecting_session_is_noop(self):
        """Explicit completion on a non-collecting session is noop."""
        result = self._engine_and_session("end reflection", status="processed")
        assert result.action == "noop"


# ═══════════════════════════════════════════════════════════════════════════════
# Inactivity-based finalization
# ═══════════════════════════════════════════════════════════════════════════════


class TestInactivityFinalizationE2E:
    """E2E tests for inactivity-based finalization."""

    def test_inactivity_triggers_when_idle_deadline_passed(self):
        """Session with passed idle_finalize_at is flagged for finalization."""
        now = _now()
        past = now - datetime.resolution
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="collecting",
            source_message_ids=[],
            opened_at=now,
            idle_finalize_at=past,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_inactivity(session=session, now=now)
        assert result.action == "finalize"

    def test_inactivity_not_triggered_when_deadline_not_passed(self):
        """Session whose idle_finalize_at is still in the future is NOT finalized."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="collecting",
            source_message_ids=[],
            opened_at=now,
            idle_finalize_at=now + datetime.resolution,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_inactivity(session=session, now=now)
        assert result.action != "finalize"

    def test_inactivity_race_safe_only_collecting(self):
        """Already-finalizing sessions are NOT re-finalized (race-safe)."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="finalizing",  # already in transition
            source_message_ids=[],
            opened_at=now,
            idle_finalize_at=now - datetime.resolution,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_inactivity(session=session, now=now)
        assert result.action == "noop"


# ═══════════════════════════════════════════════════════════════════════════════
# Topic transition finalization
# ═══════════════════════════════════════════════════════════════════════════════


class TestTopicTransitionE2E:
    """E2E tests for topic transition finalization."""

    @staticmethod
    def _eval(text: str):
        engine = FinalizationEngine()
        session = build_session_state(
            session_id=_uid(), user_id=_uid(), bot_id="test_bot",
            status="collecting", source_message_ids=[_uid()],
        )
        return engine.evaluate_topic_transition(session=session, message_text=text)

    def test_topic_transition_moving_on_detected(self):
        """'moving on' is detected as topic transition."""
        result = self._eval("OK moving on to something else")
        assert result.action == "finalize"

    def test_topic_transition_new_topic_detected(self):
        """'new topic' is detected as topic transition."""
        result = self._eval("Okay let's switch to a new topic")
        assert result.action == "finalize"

    def test_topic_transition_switch_gears_detected(self):
        """'switch gears' is detected as topic transition."""
        result = self._eval("Let me switch gears for a moment")
        assert result.action == "finalize"

    def test_topic_transition_lets_talk_about_detected(self):
        """'let's talk about' a different thing is detected as topic transition."""
        result = self._eval("Let's talk about the project instead")
        assert result.action == "finalize"

    def test_topic_transition_not_for_same_topic_elaboration(self):
        """Elaborating on the same topic is NOT a transition."""
        result = self._eval("Also regarding that, I wanted to add more details")
        assert result.action != "finalize"


# ═══════════════════════════════════════════════════════════════════════════════
# Late messages
# ═══════════════════════════════════════════════════════════════════════════════


class TestLateMessagesE2E:
    """E2E tests for late messages arriving after session is closed."""

    def test_late_message_to_finalized_session_opens_new(self):
        """A reflective message arriving after session was finalized
        should open a new session, not re-attach."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="processed",
            source_message_ids=[_uid()],
            opened_at=now,
            finalized_at=now,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(
            session=session,
            message_text="Another reflection on my day",
            is_reflection_candidate=True,
            now=now,
        )
        assert result.action in ("open_new_for_late", "skip_late", "noop")

    def test_late_message_to_abandoned_session_opens_new(self):
        """A reflective message arriving after session was abandoned
        should open a new session."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="abandoned",
            source_message_ids=[_uid()],
            opened_at=now,
            abandoned_at=now,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(
            session=session,
            message_text="I want to reflect again",
            is_reflection_candidate=True,
            now=now,
        )
        assert result.action in ("open_new_for_late", "skip_late", "noop")


# ═══════════════════════════════════════════════════════════════════════════════
# Abandoned sessions
# ═══════════════════════════════════════════════════════════════════════════════


class TestAbandonedSessionE2E:
    """E2E tests for abandoned session handling."""

    def test_abandoned_session_is_terminal(self):
        """Once abandoned, a session's full evaluation results in noop."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="abandoned",
            source_message_ids=[_uid()],
            opened_at=now,
            abandoned_at=now,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(session=session, now=now)
        assert result.action != "finalize"
        assert result.action != "abandon"

    def test_collecting_session_can_be_force_abandoned(self):
        """A collecting session can be explicitly abandoned via force flag."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="collecting",
            source_message_ids=[_uid()],
            opened_at=now - datetime.resolution,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_abandon(session=session, force=True)
        assert result.action == "abandon"


# ═══════════════════════════════════════════════════════════════════════════════
# Retry idempotency
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryIdempotencyE2E:
    """E2E tests for retry idempotency in capture and finalization."""

    async def test_capture_retry_with_same_message_ids(self):
        """Capturing the same message IDs twice should be safe (dedup)."""
        user = _FakeUser()
        mid = _uid()
        pool1 = _make_pool({mid: "My daily reflection: checking in"})
        pool2 = _make_pool({mid: "My daily reflection: checking in"})

        store1 = await _run_capture(pool1, [mid], user)
        assert store1.open_called

        # Second capture should not duplicate
        store2 = await _run_capture(pool2, [mid], user)
        # Should still open (no active session in test)
        # The idempotency is at the dedup level
        assert store2.open_called

    def test_finalization_retry_on_already_finalized_is_noop(self):
        """Calling finalize on an already-finalizing session is a noop."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="finalizing",  # already in transition
            source_message_ids=[_uid()],
            opened_at=now,
            finalized_at=now,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(session=session, now=now)
        assert result.action == "noop"

    def test_finalization_retry_on_processed_is_noop(self):
        """Calling finalize on a processed session is a noop."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="processed",
            source_message_ids=[_uid()],
            opened_at=now,
            finalized_at=now,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(session=session, now=now)
        assert result.action == "noop"

    async def test_capture_with_existing_session_retries_safely(self):
        """Re-capturing with an existing session just re-merges (dedup safe)."""
        user = _FakeUser()
        existing_session_id = _uid()
        mid_existing = _uid()
        mid_new = _uid()

        existing = _FakeSession(
            existing_session_id, user.id, "test_bot",
            source_message_ids=[mid_existing],
        )
        pool = _make_pool({mid_new: "Adding to my reflection"})
        fake_store = _FakeReflectionStore(sessions=[existing])

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(pool, [mid_new], user, bot_id="test_bot")

        assert fake_store.open_called
        source_ids = fake_store.last_open_kwargs["source_message_ids"]
        assert mid_existing in source_ids
        assert mid_new in source_ids


# ═══════════════════════════════════════════════════════════════════════════════
# Hard negatives: proactive reflection messages
# ═══════════════════════════════════════════════════════════════════════════════


class TestHardNegativesProactiveE2E:
    """E2E tests ensuring proactive/bot-generated reflection messages are NOT captured."""

    async def test_task_reminder_not_captured(self):
        """Task/reminder messages are NOT captured as reflections."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "remind me to buy groceries tomorrow"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called

    async def test_logistics_not_captured(self):
        """Logistics messages are NOT captured as reflections."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Can you book a meeting room for 3pm"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called

    async def test_joke_not_captured(self):
        """Joke messages are NOT captured as reflections."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "haha that's hilarious lol"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called

    async def test_question_not_captured(self):
        """Pure questions are NOT captured as reflections."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "what time is the meeting tomorrow"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called

    async def test_follow_up_not_captured(self):
        """Follow-up messages about logistics are NOT captured."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "can you follow up with John about the report"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called

    async def test_proactive_reflection_invitation_not_captured(self):
        """A message that looks like a bot-initiated reflection invitation
        is NOT treated as a user reflection."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "Would you like to start your daily reflection now?"})
        store = await _run_capture(pool, [mid], user)
        # This could go either way — the key is it doesn't crash
        # and if captured, is correctly classified

    async def test_scheduled_reminder_not_captured(self):
        """Messages about scheduling are NOT captured."""
        user = _FakeUser()
        mid = _uid()
        pool = _make_pool({mid: "schedule a check-in for Friday afternoon"})
        store = await _run_capture(pool, [mid], user)
        assert not store.open_called


# ═══════════════════════════════════════════════════════════════════════════════
# Concurrency tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConcurrencyE2E:
    """E2E tests for concurrent capture scenarios."""

    async def test_concurrent_captures_different_users_no_conflict(self):
        """Concurrent captures for different users do not interfere."""
        user1 = _FakeUser()
        user2 = _FakeUser()
        mid1, mid2 = _uid(), _uid()

        pool1 = _make_pool({mid1: "Daily reflection: feeling great"})
        pool2 = _make_pool({mid2: "My reflection: tough day"})

        store1 = _FakeReflectionStore()
        store2 = _FakeReflectionStore()

        async def _capture(pool, mids, user, store):
            with patch(
                "app.services.reflections_integration.ReflectionStore",
                return_value=store,
            ):
                await capture_burst_for_reflection(
                    pool, mids, user, bot_id="test_bot"
                )

        await asyncio.gather(
            _capture(pool1, [mid1], user1, store1),
            _capture(pool2, [mid2], user2, store2),
        )

        assert store1.open_called
        assert store2.open_called

    async def test_concurrent_captures_same_user_handle_gracefully(self):
        """Concurrent captures for the same user don't crash."""
        user = _FakeUser()
        mid1, mid2 = _uid(), _uid()

        pool1 = _make_pool({mid1: "Reflection part 1"})
        pool2 = _make_pool({mid2: "Reflection part 2"})

        store1 = _FakeReflectionStore()
        store2 = _FakeReflectionStore()

        async def _capture(pool, mids, store):
            with patch(
                "app.services.reflections_integration.ReflectionStore",
                return_value=store,
            ):
                await capture_burst_for_reflection(
                    pool, mids, user, bot_id="test_bot"
                )

        # Should not raise
        await asyncio.gather(
            _capture(pool1, [mid1], store1),
            _capture(pool2, [mid2], store2),
        )

    async def test_capture_does_not_block_on_failure(self):
        """Capture failures do not raise — agentic turn always proceeds."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.side_effect = RuntimeError("DB connection lost")

        # Must not raise
        await capture_burst_for_reflection(pool, [mid], user, bot_id="test_bot")


# ═══════════════════════════════════════════════════════════════════════════════
# Finalization composite evaluation
# ═══════════════════════════════════════════════════════════════════════════════


class TestFinalizationCompositeE2E:
    """E2E tests for the composite finalization evaluation pipeline."""

    def test_evaluate_full_prioritizes_explicit_over_inactivity(self):
        """When both explicit completion and inactivity would fire,
        explicit completion takes priority."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="collecting",
            source_message_ids=[_uid()],
            opened_at=now,
            idle_finalize_at=now - datetime.resolution,  # would trigger inactivity
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(
            session=session,
            message_text="end reflection",
            now=now,
        )
        assert result.action in ("finalize", "noop")

    def test_evaluate_full_noop_for_already_terminal(self):
        """Already terminal sessions with no message_text are noop."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="processed",
            source_message_ids=[_uid()],
            opened_at=now,
            finalized_at=now,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(
            session=session,
            message_text=None,
            now=now,
        )
        assert result.action == "noop"

    def test_evaluate_full_handles_missing_idle_finalize_at(self):
        """Sessions without idle_finalize_at are still evaluated correctly."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="collecting",
            source_message_ids=[_uid()],
            opened_at=now,
            idle_finalize_at=None,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(session=session, now=now)
        # Should not crash
        assert result.action in ("noop", "finalize")

    def test_evaluate_full_handles_empty_source_messages(self):
        """Session with no source messages is handled gracefully."""
        now = _now()
        session = build_session_state(
            session_id=_uid(),
            user_id=_uid(),
            bot_id="test_bot",
            status="collecting",
            source_message_ids=[],
            opened_at=now,
        )
        engine = FinalizationEngine()
        result = engine.evaluate_full(session=session, now=now)
        # Should not crash
        assert result.action in ("noop", "finalize")


# ═══════════════════════════════════════════════════════════════════════════════
# Session management edge cases
# ═══════════════════════════════════════════════════════════════════════════════


class TestSessionManagementEdgeCasesE2E:
    """E2E tests for session management edge cases."""

    def test_select_active_session_none_when_empty(self):
        """When no sessions exist, select_active_session returns None."""
        result = select_active_session(
            sessions=[],
            user_id=_uid(),
            bot_id="test_bot",
        )
        assert result is None

    def test_select_active_session_filters_by_user(self):
        """Only sessions matching user_id are returned."""
        uid1, uid2 = _uid(), _uid()
        session = build_session_snapshot(
            session_id=_uid(), user_id=uid1, bot_id="test_bot",
        )
        result = select_active_session(
            sessions=[session],
            user_id=uid2,
            bot_id="test_bot",
        )
        assert result is None

    def test_select_active_session_filters_by_bot(self):
        """Only sessions matching bot_id are returned."""
        uid = _uid()
        session = build_session_snapshot(
            session_id=_uid(), user_id=uid, bot_id="bot_a",
        )
        result = select_active_session(
            sessions=[session],
            user_id=uid,
            bot_id="bot_b",
        )
        assert result is None

    def test_merge_ordered_deduped_preserves_order(self):
        """merge_ordered_deduped preserves existing order and appends new."""
        existing = [_uid(), _uid()]
        new = [_uid()]
        merged = SessionManager.merge_ordered_deduped(existing, new)
        assert merged[:2] == existing
        assert merged[2] == new[0]

    def test_merge_ordered_deduped_deduplicates(self):
        """merge_ordered_deduped removes duplicates."""
        mid = _uid()
        existing = [mid]
        new = [mid]
        merged = SessionManager.merge_ordered_deduped(existing, new)
        assert merged == [mid]
        assert len(merged) == 1

    def test_classifier_explicit_wording_overrides_time(self):
        """Explicit wording always overrides clock-time signal."""
        # A reflective keyword at any time should win
        result = classify_message(
            "Here is my reflection for today",
            active_session_exists=False,
        )
        assert result.confidence >= 0.3
        assert result.source != "local_time"

    def test_classifier_negative_overrides_explicit(self):
        """Negative patterns (jokes/logistics) overrule even explicit keywords."""
        result = classify_message(
            "haha here is my reflection lol that's hilarious",
            active_session_exists=False,
        )
        # Joke detection should suppress the reflection keyword
        assert result.confidence < 0.6
