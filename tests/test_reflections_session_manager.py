"""Tests for app/reflections/session_manager.py — session attachment logic.

Covers:
  - Single-message attachment decisions: attach, open, skip.
  - Source-message ordering and deduplication.
  - Same-burst attachment: all messages in a burst attached together,
    first reflection candidate drives open vs attach.
  - Cross-turn attachment: messages across multiple turns preserve
    order and attach to the same session.
  - Competing starts: multiple messages in a burst that could each
    independently open a session — only first one opens.
  - Normal non-reflection pacing: non-reflection messages do not
    create or attach to sessions.
  - Active-session selection: deterministic selection by user, bot,
    topic.
  - Edge cases: empty bursts, empty text, duplicate message IDs,
    all-non-reflection bursts.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from uuid import UUID, uuid4

import pytest

from app.reflections.classifier import (
    ClassificationResult,
    classify_message,
)
from app.reflections.session_manager import (
    ActiveSessionSnapshot,
    AttachmentAction,
    SessionAttachment,
    SessionManager,
    build_session_snapshot,
    select_active_session,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid(suffix: str = "a") -> UUID:
    """Deterministic UUID from suffix string for test readability."""
    return uuid4()


def _cr(
    phase: str = "freeform",
    scope: str = "instant",
    confidence: float = 0.95,
    source: str = "explicit_wording",
    metadata: dict | None = None,
) -> ClassificationResult:
    """Create a ClassificationResult with given values."""
    return ClassificationResult(
        phase=phase,
        temporal_scope=scope,
        confidence=confidence,
        source=source,
        metadata=metadata or {},
    )


def _snap(
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "test_bot",
    topic_id: UUID | None = None,
    source_message_ids: list[UUID] | None = None,
    temporal_scope: str = "instant",
    phase: str = "freeform",
) -> ActiveSessionSnapshot:
    """Build an ActiveSessionSnapshot quickly."""
    return ActiveSessionSnapshot(
        session_id=session_id or _uid("session"),
        user_id=user_id or _uid("user"),
        bot_id=bot_id,
        topic_id=topic_id,
        source_message_ids=list(source_message_ids or []),
        temporal_scope=temporal_scope,
        phase=phase,
    )


# ── Basic attachment actions ────────────────────────────────────────────────


class TestSingleMessageAttachment:
    """Single-message evaluate_attachment: attach, open, skip."""

    def test_reflection_message_with_active_session_attaches(self):
        mgr = SessionManager()
        msg_id = _uid("msg")
        snap = _snap(source_message_ids=[_uid("existing")])

        result = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=snap,
            message_id=msg_id,
        )

        assert result.action == "attach"
        assert result.session_id == snap.session_id
        assert result.is_reflection_message is True
        assert msg_id in result.merged_source_ids

    def test_reflection_message_without_active_session_opens(self):
        mgr = SessionManager()
        msg_id = _uid("msg")

        result = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=None,
            message_id=msg_id,
        )

        assert result.action == "open"
        assert result.session_id is None  # caller assigns
        assert result.is_reflection_message is True
        assert result.merged_source_ids == [msg_id]

    def test_non_reflection_message_skips(self):
        mgr = SessionManager()
        msg_id = _uid("msg")

        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative"},
            ),
            active_session=None,
            message_id=msg_id,
        )

        assert result.action == "skip"
        assert result.is_reflection_message is False
        assert result.merged_source_ids == []

    def test_non_reflection_message_skips_even_with_active_session(self):
        """A non-reflection message should skip even if a session is collecting."""
        mgr = SessionManager()
        msg_id = _uid("msg")
        snap = _snap()

        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative"},
            ),
            active_session=snap,
            message_id=msg_id,
        )

        assert result.action == "skip"

    def test_low_confidence_freeform_skips(self):
        mgr = SessionManager()
        msg_id = _uid("msg")

        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.1,
                source="freeform_fallback",
            ),
            active_session=None,
            message_id=msg_id,
        )

        assert result.action == "skip"

    def test_medium_confidence_freeform_is_candidate(self):
        mgr = SessionManager()
        msg_id = _uid("msg")

        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.35,
                source="freeform_fallback",
            ),
            active_session=None,
            message_id=msg_id,
        )

        assert result.action == "open"
        assert result.is_reflection_message is True

    def test_time_based_classification_is_candidate(self):
        mgr = SessionManager()
        msg_id = _uid("msg")

        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.2,
                source="local_time",
            ),
            active_session=None,
            message_id=msg_id,
        )

        # confidence 0.2 < 0.3 but source is not freeform_fallback
        assert result.action == "skip"


# ── Source-message ordering & dedup ─────────────────────────────────────────


class TestSourceMessageOrdering:
    """merge_ordered_deduped: canonical ordering and duplicate prevention."""

    def test_merge_empty_existing(self):
        existing: list[UUID] = []
        a = _uid("a")
        b = _uid("b")
        result = SessionManager.merge_ordered_deduped(existing, [a, b])
        assert result == [a, b]

    def test_merge_onto_existing(self):
        existing = [_uid("x"), _uid("y")]
        new = [_uid("a"), _uid("b")]
        result = SessionManager.merge_ordered_deduped(existing, new)
        assert result == existing + new

    def test_dedup_existing_already_present(self):
        a = _uid("a")
        b = _uid("b")
        c = _uid("c")
        existing = [a, b]
        new = [a, c]
        result = SessionManager.merge_ordered_deduped(existing, new)
        # a should not appear twice
        assert result == [a, b, c]
        # Verify a appears exactly once
        a_count = sum(1 for mid in result if mid == a)
        assert a_count == 1

    def test_dedup_within_new_ids(self):
        existing: list[UUID] = []
        a = _uid("a")
        new = [a, a, a]
        result = SessionManager.merge_ordered_deduped(existing, new)
        assert result == [a]

    def test_dedup_cross_existing_and_new(self):
        a = _uid("a")
        b = _uid("b")
        c = _uid("c")
        d = _uid("d")
        existing = [a, b]
        new = [b, c, a, d]
        result = SessionManager.merge_ordered_deduped(existing, new)
        # b and a are already present; only c and d appended
        assert result == [a, b, c, d]

    def test_preserves_existing_order(self):
        a = _uid("a")
        b = _uid("b")
        c = _uid("c")
        existing = [c, a]
        new = [b]
        result = SessionManager.merge_ordered_deduped(existing, new)
        assert result == [c, a, b]

    def test_preserves_new_order(self):
        existing: list[UUID] = []
        a = _uid("a")
        b = _uid("b")
        c = _uid("c")
        new = [c, a, b]
        result = SessionManager.merge_ordered_deduped(existing, new)
        assert result == [c, a, b]

    def test_idempotent(self):
        existing = [_uid("x")]
        new = [_uid("a")]
        first = SessionManager.merge_ordered_deduped(existing, new)
        second = SessionManager.merge_ordered_deduped(existing, new)
        assert first == second

    def test_is_duplicate_true(self):
        a = _uid("a")
        assert SessionManager.is_duplicate(a, [a, _uid("b")]) is True

    def test_is_duplicate_false(self):
        a = _uid("a")
        assert SessionManager.is_duplicate(a, [_uid("b"), _uid("c")]) is False

    def test_is_duplicate_empty(self):
        assert SessionManager.is_duplicate(_uid("a"), []) is False


# ── Same-burst attachment ───────────────────────────────────────────────────


class TestSameBurstAttachment:
    """Multiple messages arriving in the same burst — all attached together."""

    def test_burst_all_reflection_messages_attached(self):
        mgr = SessionManager()
        ids = [_uid("1"), _uid("2"), _uid("3")]
        classifications = [(mid, _cr(confidence=0.95)) for mid in ids]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=None,
        )

        assert result.action == "open"
        assert result.session_id is None
        assert result.merged_source_ids == ids
        assert result.is_reflection_message is True

    def test_burst_first_reflection_opens_rest_attached(self):
        """First message is reflection, rest are not — all should be attached."""
        mgr = SessionManager()
        ids = [_uid("1"), _uid("2"), _uid("3")]
        classifications = [
            (ids[0], _cr(confidence=0.95)),  # reflection
            (ids[1], _cr(confidence=0.0, source="negative_pattern",
                         metadata={"reason": "negative"})),  # non-reflection
            (ids[2], _cr(confidence=0.1, source="freeform_fallback")),  # non-reflection
        ]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=None,
        )

        assert result.action == "open"
        assert result.merged_source_ids == ids  # all three attached

    def test_burst_middle_reflection_still_attaches_all(self):
        """First message is non-reflection, second is reflection — all attached."""
        mgr = SessionManager()
        ids = [_uid("1"), _uid("2"), _uid("3")]
        classifications = [
            (ids[0], _cr(confidence=0.0, source="negative_pattern",
                         metadata={"reason": "negative"})),
            (ids[1], _cr(confidence=0.95)),  # first reflection candidate
            (ids[2], _cr(confidence=0.0, source="negative_pattern",
                         metadata={"reason": "negative"})),
        ]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=None,
        )

        assert result.action == "open"
        assert result.merged_source_ids == ids

    def test_burst_with_active_session_attaches_all(self):
        mgr = SessionManager()
        existing_id = _uid("existing")
        snap = _snap(source_message_ids=[existing_id])

        ids = [_uid("1"), _uid("2")]
        classifications = [(mid, _cr(confidence=0.95)) for mid in ids]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=snap,
        )

        assert result.action == "attach"
        assert result.session_id == snap.session_id
        # existing + new in order
        assert result.merged_source_ids == [existing_id] + ids

    def test_burst_dedups_against_active_session(self):
        mgr = SessionManager()
        existing_id = _uid("existing")
        snap = _snap(source_message_ids=[existing_id])

        # One new message is same as existing
        ids = [existing_id, _uid("new")]
        classifications = [(mid, _cr(confidence=0.95)) for mid in ids]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=snap,
        )

        assert result.action == "attach"
        # existing_id should appear only once, followed by new
        assert result.merged_source_ids == [existing_id, ids[1]]

    def test_empty_burst_skips(self):
        mgr = SessionManager()
        result = mgr.evaluate_burst_attachment(
            classifications=[],
            active_session=None,
        )
        assert result.action == "skip"
        assert result.merged_source_ids == []
        assert result.is_reflection_message is False

    def test_all_non_reflection_burst_skips(self):
        mgr = SessionManager()
        ids = [_uid("1"), _uid("2")]
        classifications = [
            (mid, _cr(confidence=0.0, source="negative_pattern",
                      metadata={"reason": "negative"}))
            for mid in ids
        ]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=None,
        )

        assert result.action == "skip"
        assert result.merged_source_ids == []


# ── Cross-turn attachment ───────────────────────────────────────────────────


class TestCrossTurnAttachment:
    """Messages across multiple turns — preserve order and attach to same session."""

    def test_cross_turn_second_turn_attaches(self):
        """First turn opens session, second turn attaches to it."""
        mgr = SessionManager()
        user_id = _uid("user")
        bot_id = "test_bot"

        # Turn 1: message opens session
        msg1 = _uid("msg1")
        result1 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=None,
            message_id=msg1,
        )
        assert result1.action == "open"
        assert result1.merged_source_ids == [msg1]

        # Simulate session created after turn 1
        session_id = _uid("session")
        snap = build_session_snapshot(
            session_id=session_id,
            user_id=user_id,
            bot_id=bot_id,
            source_message_ids=[msg1],
        )

        # Turn 2: another message arrives — should attach
        msg2 = _uid("msg2")
        result2 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=snap,
            message_id=msg2,
        )
        assert result2.action == "attach"
        assert result2.session_id == session_id
        assert result2.merged_source_ids == [msg1, msg2]

    def test_cross_turn_order_preserved(self):
        """Multiple turns preserve message order in source_message_ids."""
        mgr = SessionManager()
        user_id = _uid("user")
        bot_id = "test_bot"

        # Turn 1
        msg1 = _uid("msg1")
        r1 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=None,
            message_id=msg1,
        )

        snap = build_session_snapshot(
            session_id=_uid("session"),
            user_id=user_id,
            bot_id=bot_id,
            source_message_ids=r1.merged_source_ids,
        )

        # Turn 2
        msg2 = _uid("msg2")
        r2 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.55, source="message_semantics"),
            active_session=snap,
            message_id=msg2,
        )
        snap = build_session_snapshot(
            session_id=snap.session_id,
            user_id=user_id,
            bot_id=bot_id,
            source_message_ids=r2.merged_source_ids,
        )

        # Turn 3
        msg3 = _uid("msg3")
        r3 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.45, source="conversational_context"),
            active_session=snap,
            message_id=msg3,
        )

        assert r3.merged_source_ids == [msg1, msg2, msg3]

    def test_cross_turn_non_reflection_does_not_break_session(self):
        """A non-reflection message between turns does not attach to session."""
        mgr = SessionManager()
        user_id = _uid("user")
        bot_id = "test_bot"

        # Turn 1: opens session
        msg1 = _uid("msg1")
        r1 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=None,
            message_id=msg1,
        )

        snap = build_session_snapshot(
            session_id=_uid("session"),
            user_id=user_id,
            bot_id=bot_id,
            source_message_ids=r1.merged_source_ids,
        )

        # Turn 2: non-reflection message
        msg2 = _uid("msg2")
        r2 = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative"},
            ),
            active_session=snap,
            message_id=msg2,
        )
        assert r2.action == "skip"
        # Session unchanged
        assert r2.merged_source_ids == []

        # Turn 3: reflection message again — should still attach
        msg3 = _uid("msg3")
        r3 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=snap,  # same session, unchanged
            message_id=msg3,
        )
        assert r3.action == "attach"
        assert r3.merged_source_ids == [msg1, msg3]  # msg2 skipped

    def test_cross_turn_dedup(self):
        """Same message ID in different turns should be deduplicated."""
        mgr = SessionManager()
        user_id = _uid("user")
        bot_id = "test_bot"

        msg1 = _uid("msg1")
        r1 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=None,
            message_id=msg1,
        )

        snap = build_session_snapshot(
            session_id=_uid("session"),
            user_id=user_id,
            bot_id=bot_id,
            source_message_ids=r1.merged_source_ids,
        )

        # Same message ID arrives again (should be deduped)
        r2 = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=snap,
            message_id=msg1,  # same ID
        )
        assert r2.action == "attach"
        assert r2.merged_source_ids == [msg1]  # still just one


# ── Competing starts ────────────────────────────────────────────────────────


class TestCompetingStarts:
    """Multiple messages that could independently open a session — first wins."""

    def test_two_reflection_messages_in_burst_first_opens_second_attaches(self):
        mgr = SessionManager()
        ids = [_uid("1"), _uid("2")]
        classifications = [(mid, _cr(confidence=0.95)) for mid in ids]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=None,
        )

        # First message triggers open, not two separate opens
        assert result.action == "open"
        assert result.session_id is None
        assert result.merged_source_ids == ids

    def test_competing_starts_with_existing_session(self):
        """Even if multiple messages could open, existing session takes priority."""
        mgr = SessionManager()
        snap = _snap(source_message_ids=[_uid("existing")])

        ids = [_uid("1"), _uid("2")]
        classifications = [(mid, _cr(confidence=0.95)) for mid in ids]

        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=snap,
        )

        assert result.action == "attach"
        assert result.session_id == snap.session_id
        assert len(result.merged_source_ids) == 3  # existing + 2 new

    def test_single_message_competing_no_session(self):
        """With no active session, a single reflection message opens."""
        mgr = SessionManager()
        msg_id = _uid("msg")

        result = mgr.evaluate_attachment(
            classification=_cr(confidence=0.95),
            active_session=None,
            message_id=msg_id,
        )

        assert result.action == "open"
        assert result.merged_source_ids == [msg_id]


# ── Normal non-reflection pacing ────────────────────────────────────────────


class TestNonReflectionPacing:
    """Non-reflection messages should not create or attach to sessions."""

    def test_logistics_message_skips(self):
        mgr = SessionManager()
        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative", "matched_patterns": ["shopping"]},
            ),
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "skip"

    def test_joke_message_skips(self):
        mgr = SessionManager()
        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative", "matched_patterns": ["joke"]},
            ),
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "skip"

    def test_question_message_skips(self):
        mgr = SessionManager()
        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative", "matched_patterns": ["clock_check"]},
            ),
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "skip"

    def test_task_message_skips(self):
        mgr = SessionManager()
        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative", "matched_patterns": ["task_create"]},
            ),
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "skip"

    def test_reminder_message_skips(self):
        mgr = SessionManager()
        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative", "matched_patterns": ["reminder_create"]},
            ),
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "skip"

    def test_greeting_only_skips(self):
        mgr = SessionManager()
        result = mgr.evaluate_attachment(
            classification=_cr(
                confidence=0.0,
                source="negative_pattern",
                metadata={"reason": "negative", "matched_patterns": ["greeting_only"]},
            ),
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "skip"

    def test_pacing_non_reflection_in_burst(self):
        """A burst with only non-reflection messages skips entirely."""
        mgr = SessionManager()
        ids = [_uid("1"), _uid("2")]
        classifications = [
            (mid, _cr(confidence=0.0, source="negative_pattern",
                      metadata={"reason": "negative", "matched_patterns": ["joke"]}))
            for mid in ids
        ]
        result = mgr.evaluate_burst_attachment(
            classifications=classifications,
            active_session=None,
        )
        assert result.action == "skip"

    def test_real_classifier_integration_non_reflection(self):
        """Integration: actual classifier marks logistics as non-reflection."""
        mgr = SessionManager()
        cr = classify_message("Book a flight to Chicago for next week")
        result = mgr.evaluate_attachment(
            classification=cr,
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "skip"

    def test_real_classifier_integration_reflection(self):
        """Integration: actual classifier marks explicit reflection as candidate."""
        mgr = SessionManager()
        cr = classify_message("Time for a retrospective on this sprint")
        result = mgr.evaluate_attachment(
            classification=cr,
            active_session=None,
            message_id=_uid("msg"),
        )
        assert result.action == "open"
        assert result.is_reflection_message is True


# ── Active-session selection ────────────────────────────────────────────────


class TestActiveSessionSelection:
    """Deterministic active-session selection by user, bot, topic."""

    def test_select_by_user_bot_exact_match(self):
        user_a = _uid("user_a")
        user_b = _uid("user_b")
        bot_x = "bot_x"
        bot_y = "bot_y"

        sessions = [
            _snap(user_id=user_a, bot_id=bot_x),
            _snap(user_id=user_b, bot_id=bot_x),
            _snap(user_id=user_a, bot_id=bot_y),
        ]

        result = select_active_session(
            sessions=sessions,
            user_id=user_a,
            bot_id=bot_x,
        )

        assert result is not None
        assert result.user_id == user_a
        assert result.bot_id == bot_x

    def test_select_no_match_returns_none(self):
        sessions = [
            _snap(user_id=_uid("ua"), bot_id="bot_x"),
        ]
        result = select_active_session(
            sessions=sessions,
            user_id=_uid("other"),
            bot_id="bot_x",
        )
        assert result is None

    def test_select_prefers_topic_match(self):
        user = _uid("user")
        bot = "bot_x"
        topic_a = _uid("topic_a")
        topic_b = _uid("topic_b")

        sessions = [
            _snap(user_id=user, bot_id=bot, topic_id=topic_a),
            _snap(user_id=user, bot_id=bot, topic_id=None),
            _snap(user_id=user, bot_id=bot, topic_id=topic_b),
        ]

        result = select_active_session(
            sessions=sessions,
            user_id=user,
            bot_id=bot,
            topic_id=topic_b,
        )

        assert result is not None
        assert result.topic_id == topic_b

    def test_select_no_topic_match_falls_back_to_any(self):
        user = _uid("user")
        bot = "bot_x"
        topic_a = _uid("topic_a")

        sessions = [
            _snap(user_id=user, bot_id=bot, topic_id=topic_a),
            _snap(user_id=user, bot_id=bot, topic_id=None),
        ]

        result = select_active_session(
            sessions=sessions,
            user_id=user,
            bot_id=bot,
            topic_id=_uid("other_topic"),  # no match
        )

        # Should fall back to one of the sessions for this user/bot
        assert result is not None
        assert result.user_id == user
        assert result.bot_id == bot

    def test_select_deterministic_tiebreaker(self):
        """When multiple sessions match, result is deterministic."""
        user = _uid("user")
        bot = "bot_x"
        id_a = UUID("00000000-0000-0000-0000-000000000001")
        id_b = UUID("00000000-0000-0000-0000-000000000002")

        sessions = [
            _snap(session_id=id_a, user_id=user, bot_id=bot),
            _snap(session_id=id_b, user_id=user, bot_id=bot),
        ]

        result = select_active_session(
            sessions=sessions,
            user_id=user,
            bot_id=bot,
        )

        # Higher UUID (id_b) sorts last in ascending, so reverse sort picks it
        assert result is not None
        assert result.session_id == id_b

    def test_select_empty_list(self):
        result = select_active_session(
            sessions=[],
            user_id=_uid("user"),
            bot_id="bot_x",
        )
        assert result is None

    def test_select_multiple_users_one_match(self):
        user_match = _uid("match")
        sessions = [
            _snap(user_id=_uid("other1"), bot_id="bot"),
            _snap(user_id=user_match, bot_id="bot"),
            _snap(user_id=_uid("other2"), bot_id="bot"),
        ]
        result = select_active_session(
            sessions=sessions,
            user_id=user_match,
            bot_id="bot",
        )
        assert result is not None
        assert result.user_id == user_match


# ── build_session_snapshot ──────────────────────────────────────────────────


class TestBuildSessionSnapshot:
    """Convenience builder for ActiveSessionSnapshot."""

    def test_build_minimal(self):
        sid = _uid("s")
        uid = _uid("u")
        snap = build_session_snapshot(
            session_id=sid,
            user_id=uid,
            bot_id="test",
        )
        assert snap.session_id == sid
        assert snap.user_id == uid
        assert snap.bot_id == "test"
        assert snap.topic_id is None
        assert snap.source_message_ids == []
        assert snap.temporal_scope == "instant"
        assert snap.phase == "freeform"

    def test_build_full(self):
        sid = _uid("s")
        uid = _uid("u")
        tid = _uid("t")
        mids = [_uid("m1"), _uid("m2")]
        snap = build_session_snapshot(
            session_id=sid,
            user_id=uid,
            bot_id="full_bot",
            topic_id=tid,
            source_message_ids=mids,
            temporal_scope="week",
            phase="retrospective",
        )
        assert snap.topic_id == tid
        assert snap.source_message_ids == mids
        assert snap.temporal_scope == "week"
        assert snap.phase == "retrospective"

    def test_source_message_ids_defaults_to_empty(self):
        snap = build_session_snapshot(
            session_id=_uid("s"),
            user_id=_uid("u"),
            bot_id="test",
        )
        assert snap.source_message_ids == []

    def test_source_message_ids_none_handled(self):
        snap = build_session_snapshot(
            session_id=_uid("s"),
            user_id=_uid("u"),
            bot_id="test",
            source_message_ids=None,
        )
        assert snap.source_message_ids == []


# ── SessionAttachment invariants ────────────────────────────────────────────


class TestSessionAttachmentInvariants:
    """SessionAttachment dataclass invariants."""

    def test_attach_has_session_id(self):
        sid = _uid("s")
        sa = SessionAttachment(
            action="attach",
            session_id=sid,
            reason="test",
            merged_source_ids=[_uid("m")],
            is_reflection_message=True,
        )
        assert sa.session_id == sid
        assert sa.action == "attach"

    def test_open_has_no_session_id(self):
        sa = SessionAttachment(
            action="open",
            session_id=None,
            reason="test",
            merged_source_ids=[_uid("m")],
            is_reflection_message=True,
        )
        assert sa.session_id is None

    def test_skip_has_empty_ids(self):
        sa = SessionAttachment(
            action="skip",
            session_id=None,
            reason="test",
            merged_source_ids=[],
            is_reflection_message=False,
        )
        assert sa.merged_source_ids == []
        assert sa.is_reflection_message is False

    def test_frozen(self):
        sa = SessionAttachment(
            action="skip",
            session_id=None,
            reason="test",
        )
        with pytest.raises(Exception):
            sa.action = "open"  # type: ignore[misc]


# ── ActiveSessionSnapshot invariants ────────────────────────────────────────


class TestActiveSessionSnapshotInvariants:
    """ActiveSessionSnapshot dataclass invariants."""

    def test_frozen(self):
        snap = _snap()
        with pytest.raises(Exception):
            snap.bot_id = "other"  # type: ignore[misc]

    def test_default_temporal_scope(self):
        snap = _snap()
        assert snap.temporal_scope == "instant"

    def test_default_phase(self):
        snap = _snap()
        assert snap.phase == "freeform"

    def test_default_topic_id(self):
        snap = _snap()
        assert snap.topic_id is None

    def test_source_message_ids_are_list(self):
        snap = _snap(source_message_ids=[_uid("a"), _uid("b")])
        assert isinstance(snap.source_message_ids, list)
        assert len(snap.source_message_ids) == 2
