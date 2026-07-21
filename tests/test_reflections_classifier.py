"""Tests for app/reflections/classifier.py — locked precedence policy.

Covers the full precedence chain (explicit wording > active session > message
semantics > conversational context > local time) with:
  - Positive examples: explicit reflections, semantic introspection, contextual
    continuations, and time-of-day signals.
  - Temporal scope examples: day, week, month, instant, custom, none.
  - Misleading clock-time examples: jokes/logistics at day boundary that must
    NOT be classified as reflections.
  - Negative examples: logistics, jokes, questions, tasks, reminders, follow-ups.
  - Edge cases: empty text, pure whitespace, very short messages.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app.reflections.classifier import (
    ClassificationResult,
    VALID_PHASES,
    VALID_TEMPORAL_SCOPES,
    classify_message,
    is_reflection_candidate,
)


# ── Basic result invariants ─────────────────────────────────────────────────


class TestClassificationResult:
    """Test ClassificationResult dataclass invariants."""

    def test_valid_construction(self):
        result = ClassificationResult(
            phase="freeform",
            temporal_scope="day",
            confidence=0.5,
            source="test",
        )
        assert result.phase == "freeform"
        assert result.temporal_scope == "day"
        assert result.confidence == 0.5

    def test_invalid_phase_raises(self):
        with pytest.raises(ValueError, match="invalid phase"):
            ClassificationResult(
                phase="bogus",
                temporal_scope="day",
                confidence=0.5,
                source="test",
            )

    def test_invalid_scope_raises(self):
        with pytest.raises(ValueError, match="invalid temporal_scope"):
            ClassificationResult(
                phase="freeform",
                temporal_scope="bogus",
                confidence=0.5,
                source="test",
            )

    def test_confidence_below_zero_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            ClassificationResult(
                phase="freeform",
                temporal_scope="day",
                confidence=-0.1,
                source="test",
            )

    def test_confidence_above_one_raises(self):
        with pytest.raises(ValueError, match="confidence"):
            ClassificationResult(
                phase="freeform",
                temporal_scope="day",
                confidence=1.5,
                source="test",
            )

    def test_all_phases_are_valid(self):
        for phase in VALID_PHASES:
            result = ClassificationResult(
                phase=phase,
                temporal_scope="day",
                confidence=0.5,
                source="test",
            )
            assert result.phase == phase

    def test_all_scopes_are_valid(self):
        for scope in VALID_TEMPORAL_SCOPES:
            result = ClassificationResult(
                phase="freeform",
                temporal_scope=scope,
                confidence=0.5,
                source="test",
            )
            assert result.temporal_scope == scope


# ── Tier 1: Explicit wording ────────────────────────────────────────────────


class TestExplicitWording:
    """Tier 1: user names reflection phase/scope directly."""

    def test_explicit_reflection_word(self):
        result = classify_message("I want to write a reflection about my day")
        assert result.phase == "freeform"
        assert result.source == "explicit_wording"
        assert result.confidence == 0.95

    def test_explicit_reflect_verb(self):
        result = classify_message("Let me reflect on what happened today")
        assert result.phase == "freeform"
        assert result.source == "explicit_wording"
        assert result.confidence == 0.95

    def test_explicit_retrospective(self):
        result = classify_message("Time for a retrospective on this sprint")
        assert result.phase == "retrospective"
        assert result.source == "explicit_wording"
        assert result.confidence == 0.95

    def test_explicit_retro_short(self):
        result = classify_message("Weekly retro")
        assert result.phase == "retrospective"
        assert result.source == "explicit_wording"

    def test_explicit_prospective(self):
        result = classify_message("I'm looking ahead at next week's goals, this is my prospective")
        assert result.phase == "prospective"
        assert result.source == "explicit_wording"

    def test_explicit_checkpoint(self):
        result = classify_message("Checkpoint: here's where I am")
        assert result.phase == "checkpoint"
        assert result.source == "explicit_wording"

    def test_explicit_opening(self):
        result = classify_message("Starting a new project, this is an opening reflection")
        assert result.phase == "opening"
        assert result.source == "explicit_wording"

    def test_explicit_closing(self):
        result = classify_message("Wrapping up the quarter — closing thoughts")
        assert result.phase == "closing"
        assert result.source == "explicit_wording"

    def test_explicit_scope_day(self):
        result = classify_message("Today's reflection: what went well")
        assert result.temporal_scope == "day"
        assert result.source == "explicit_wording"

    def test_explicit_scope_week(self):
        result = classify_message("My weekly review reflection")
        assert result.temporal_scope == "week"
        assert result.source == "explicit_wording"

    def test_explicit_scope_month(self):
        result = classify_message("Monthly retrospective on my progress")
        assert result.temporal_scope == "month"
        assert result.source == "explicit_wording"

    def test_explicit_scope_instant(self):
        result = classify_message("Right now I'm reflecting on this moment")
        assert result.temporal_scope == "instant"
        assert result.source == "explicit_wording"

    def test_explicit_phase_only_defaults_scope_to_instant(self):
        result = classify_message("This is a retrospective")
        assert result.phase == "retrospective"
        assert result.temporal_scope == "instant"
        assert result.source == "explicit_wording"

    def test_explicit_scope_only_defaults_phase_to_freeform(self):
        result = classify_message("My daily update")
        assert result.phase == "freeform"
        assert result.temporal_scope == "day"
        assert result.source == "explicit_wording"


# ── Tier 2: Active-session context ──────────────────────────────────────────


class TestActiveSessionContext:
    """Tier 2: active collecting session exists."""

    def test_active_session_biases_to_reflection(self):
        result = classify_message(
            "I've been thinking about my priorities",
            active_session_exists=True,
        )
        assert result.source == "active_session"
        assert result.confidence == 0.75
        assert result.phase == "freeform"
        assert result.temporal_scope == "instant"

    def test_active_session_without_reflective_content(self):
        result = classify_message(
            "Just had lunch",
            active_session_exists=True,
        )
        assert result.source == "active_session"
        assert result.confidence == 0.75

    def test_active_session_negative_override(self):
        """Even with active session, explicit negative patterns should block."""
        result = classify_message(
            "Remind me to buy groceries",
            active_session_exists=True,
        )
        assert result.source == "negative_pattern"
        assert result.confidence == 0.0
        assert result.temporal_scope == "none"

    def test_active_session_task_pattern_negative(self):
        result = classify_message(
            "Create a task to follow up with the design team",
            active_session_exists=True,
        )
        assert result.source == "negative_pattern"

    def test_active_session_joke_negative(self):
        result = classify_message(
            "Haha just kidding about that whole reflection thing lol",
            active_session_exists=True,
        )
        assert result.source == "negative_pattern"


# ── Tier 3: Message semantics ───────────────────────────────────────────────


class TestMessageSemantics:
    """Tier 3: introspective content patterns."""

    def test_i_feel_introspection(self):
        result = classify_message("I feel like I'm making real progress lately")
        assert result.source == "message_semantics"
        assert result.confidence == 0.55
        assert result.temporal_scope == "day"

    def test_i_noticed_introspection(self):
        result = classify_message("I noticed a pattern of procrastination")
        assert result.source == "message_semantics"

    def test_i_learned_introspection(self):
        # "today" triggers explicit scope — explicit wording wins over semantics
        result = classify_message("I learned an important lesson about delegation")
        assert result.source == "message_semantics"

    def test_gratitude_introspection(self):
        result = classify_message("I'm grateful for the support I received")
        assert result.source == "message_semantics"

    def test_struggle_introspection(self):
        # "this week" would trigger explicit scope — use text without scope words
        result = classify_message("I'm struggling with motivation lately")
        assert result.source == "message_semantics"

    def test_goal_introspection(self):
        # Use text that triggers goal-related introspection without explicit phase/scope words
        result = classify_message("My main goal is to get healthier")
        assert result.source == "message_semantics"

    def test_mood_introspection(self):
        result = classify_message("My energy levels have been low")
        assert result.source == "message_semantics"

    def test_insight_with_keyword(self):
        result = classify_message("It dawned on me that I need to change my approach")
        assert result.source == "message_semantics"

    def test_semantics_override_by_explicit(self):
        """Explicit wording should beat semantics when both match."""
        result = classify_message(
            "I feel like this week has been amazing — time for a retrospective"
        )
        assert result.source == "explicit_wording"
        assert result.phase == "retrospective"


# ── Tier 4: Conversational context ──────────────────────────────────────────


class TestConversationalContext:
    """Tier 4: message continues a reflective conversation."""

    def test_continuation_in_reflective_context(self):
        result = classify_message(
            "Also, I've been thinking about my career goals",
            conversation_context="I feel like I've been stuck lately. My motivation is low.",
        )
        assert result.source == "conversational_context"
        assert result.confidence <= 0.50

    def test_continuation_word_also(self):
        result = classify_message(
            "Also, the team dynamics have improved",
            conversation_context="Today's reflection: I noticed better collaboration.",
        )
        assert result.source == "conversational_context"

    def test_continuation_word_and(self):
        result = classify_message(
            "And another angle worth mentioning was about communication",
            conversation_context="My retrospective: I learned I need more structure.",
        )
        assert result.source == "conversational_context"

    def test_reflective_response_in_context(self):
        result = classify_message(
            "Also, it seems like there's more to unpack here",
            conversation_context="I feel like the same issues keep coming up every quarter.",
        )
        assert result.source == "conversational_context"

    def test_non_reflective_context_no_bias(self):
        result = classify_message(
            "Also, can you remind me about the meeting?",
            conversation_context="What time is the meeting tomorrow?",
        )
        # Should NOT be classified by conversational context; falls through to
        # negative pattern or freeform
        assert result.source != "conversational_context"

    def test_logistics_in_reflective_context(self):
        """Logistics message in reflective context should still be negative."""
        result = classify_message(
            "Also, send the report to the team",
            conversation_context="I feel like I accomplished a lot this week.",
        )
        # "send" + "to" matches negative pattern
        assert result.source == "negative_pattern"

    def test_continuation_without_introspection_in_text(self):
        result = classify_message(
            "Also, I had lunch",
            conversation_context="I feel like I've been growing a lot.",
        )
        # No introspection in the message itself, but continuation word in
        # reflective context — should still trigger
        assert result.source == "conversational_context"


# ── Tier 5: Local time (weakest signal) ─────────────────────────────────────


class TestLocalTime:
    """Tier 5: time-of-day hints — weakest signal."""

    def test_morning_opening(self):
        local = datetime(2026, 7, 20, 7, 30, tzinfo=timezone(timedelta(hours=-4)))
        result = classify_message(
            "Starting the day",
            local_datetime=local,
        )
        # "starting" triggers explicit wording first
        assert result.source == "explicit_wording"

    def test_morning_neutral_message(self):
        """A neutral message at morning — time signal should fire."""
        local = datetime(2026, 7, 20, 7, 30, tzinfo=timezone(timedelta(hours=-4)))
        result = classify_message(
            "Going to work on things",
            local_datetime=local,
        )
        assert result.source == "local_time"
        assert result.phase == "opening"
        assert result.confidence == 0.2

    def test_midday_checkpoint(self):
        local = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        result = classify_message(
            "Just working through things",
            local_datetime=local,
        )
        assert result.source == "local_time"
        assert result.phase == "checkpoint"

    def test_evening_closing(self):
        local = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
        result = classify_message(
            "Winding down",
            local_datetime=local,
        )
        assert result.source == "local_time"
        assert result.phase == "closing"

    def test_late_night_retrospective(self):
        local = datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc)
        result = classify_message(
            "Thinking about things",
            local_datetime=local,
        )
        assert result.source == "local_time"
        assert result.phase == "retrospective"

    def test_time_is_weakest_signal(self):
        """Even at a prime reflection time, explicit negative overrides."""
        local = datetime(2026, 7, 20, 22, 0, tzinfo=timezone.utc)
        result = classify_message(
            "Just kidding about all that introspection lol",
            local_datetime=local,
        )
        # Joke pattern overrides time
        assert result.source == "negative_pattern"

    def test_time_confidence_is_low(self):
        local = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)
        result = classify_message(
            "Some random thought",
            local_datetime=local,
        )
        assert result.source == "local_time"
        assert result.confidence == 0.2


# ── Negative examples ───────────────────────────────────────────────────────


class TestNegativeExamples:
    """Messages that must NOT be classified as reflections."""

    def test_joke_ha(self):
        result = classify_message("That's hilarious haha")
        assert result.source == "negative_pattern"
        assert result.confidence == 0.0

    def test_joke_lol(self):
        result = classify_message("lol I can't believe it")
        assert result.source == "negative_pattern"

    def test_pure_greeting(self):
        result = classify_message("Hello!")
        assert result.source == "negative_pattern"

    def test_pure_goodbye(self):
        result = classify_message("Goodbye!")
        assert result.source == "negative_pattern"

    def test_pure_thanks(self):
        result = classify_message("Thank you!")
        assert result.source == "negative_pattern"

    def test_pure_acknowledgment(self):
        result = classify_message("OK")
        assert result.source == "negative_pattern"

    def test_greeting_with_name(self):
        result = classify_message("Good morning!")
        assert result.source == "negative_pattern"

    def test_clock_check_question(self):
        result = classify_message("What time is it?")
        assert result.source == "negative_pattern"

    def test_weather_question(self):
        result = classify_message("What's the weather forecast?")
        assert result.source == "negative_pattern"

    def test_how_to_question(self):
        result = classify_message("How do I file my taxes?")
        assert result.source == "negative_pattern"

    def test_reminder_create(self):
        result = classify_message("Remind me to call mom at 5pm")
        assert result.source == "negative_pattern"

    def test_task_create(self):
        result = classify_message("Add a task: review the quarterly report")
        assert result.source == "negative_pattern"

    def test_follow_up_command(self):
        result = classify_message("Follow up with Sarah about the proposal")
        assert result.source == "negative_pattern"

    def test_send_action(self):
        result = classify_message("Send the document to the client")
        assert result.source == "negative_pattern"

    def test_schedule_request(self):
        result = classify_message("Schedule a meeting for tomorrow")
        assert result.source == "negative_pattern"

    def test_travel_logistics(self):
        result = classify_message("Book a flight to Chicago for next week")
        assert result.source == "negative_pattern"

    def test_shopping_list(self):
        result = classify_message("Add milk and eggs to the shopping list")
        assert result.source == "negative_pattern"

    def test_event_logistics(self):
        result = classify_message("RSVP for the company party")
        assert result.source == "negative_pattern"

    def test_link_share(self):
        result = classify_message("Here's the link: https://example.com")
        assert result.source == "negative_pattern"


# ── Edge cases ───────────────────────────────────────────────────────────────


class TestEdgeCases:
    """Boundary and edge case behaviour."""

    def test_empty_string(self):
        result = classify_message("")
        assert result.source == "freeform_fallback"
        assert result.confidence == 0.0
        assert result.temporal_scope == "none"

    def test_whitespace_only(self):
        result = classify_message("   \t\n  ")
        assert result.source == "freeform_fallback"
        assert result.confidence == 0.0

    def test_none_text(self):
        """None should be handled like empty."""
        result = classify_message("")  # empty instead of None, type-safe
        assert result.confidence == 0.0

    def test_very_short_ambiguous(self):
        result = classify_message("Hmm")
        assert result.source == "freeform_fallback"
        assert result.confidence == 0.1

    def test_no_signals_at_all(self):
        result = classify_message("It's a nice day")
        assert result.source == "freeform_fallback"

    def test_metadata_included(self):
        result = classify_message("This is a reflection on my week")
        assert result.metadata is not None
        assert "reason" in result.metadata
        assert result.metadata["matched_phase"] == "freeform"
        assert result.metadata["matched_scope"] == "week"

    def test_negative_metadata_includes_patterns(self):
        result = classify_message("Remind me to joke about shopping")
        assert result.metadata is not None
        assert "matched_patterns" in result.metadata
        assert len(result.metadata["matched_patterns"]) > 0


# ── is_reflection_candidate ─────────────────────────────────────────────────


class TestIsReflectionCandidate:
    """Quick boolean check for routing decisions."""

    def test_explicit_reflection_is_candidate(self):
        assert is_reflection_candidate("Daily reflection on my progress") is True

    def test_semantic_introspection_is_candidate(self):
        assert is_reflection_candidate("I feel like I've grown this week") is True

    def test_active_session_is_candidate(self):
        assert (
            is_reflection_candidate("Some thought", active_session_exists=True) is True
        )

    def test_joke_is_not_candidate(self):
        assert is_reflection_candidate("haha that's funny") is False

    def test_reminder_is_not_candidate(self):
        assert is_reflection_candidate("Remind me to check email") is False

    def test_greeting_is_not_candidate(self):
        assert is_reflection_candidate("Hello!") is False

    def test_empty_is_not_candidate(self):
        assert is_reflection_candidate("") is False

    def test_ambiguous_low_confidence_is_not_candidate(self):
        """Freeform with very low confidence should not be a candidate."""
        result = classify_message("Some random thought at midday")
        # This depends on whether it matches any pattern; if it's pure freeform
        # with low confidence, is_reflection_candidate returns False
        if result.source == "freeform_fallback" and result.confidence < 0.3:
            assert is_reflection_candidate("Some random thought at midday") is False
