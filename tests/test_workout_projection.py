"""Tests for the pure workout→commitment projection matcher.

Covers all non-projecting reasons: no local_date, unknown workout type,
zero commitments, wrong bot/topic, no eligible slot, ambiguous multiple
commitments, and the happy path of exactly one match.
"""

from __future__ import annotations

import pytest
from datetime import date, datetime, timezone

from app.services.health_sync.models import NormalizedWorkout
from app.services.health_sync.workout_projection import (
    ProjectionDecision,
    ProjectionMatch,
    project_workout,
    reason_is_projecting,
    _REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS,
    _REASON_MATCHED,
    _REASON_NO_ELIGIBLE_SLOT,
    _REASON_NO_HECTOR_FITNESS_COMMITMENTS,
    _REASON_NO_LOCAL_DATE,
    _REASON_UNKNOWN_WORKOUT_TYPE,
    _REASON_ZERO_ACTIVE_COMMITMENTS,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _make_workout(
    *,
    local_date: date | None = date(2025, 6, 16),  # a Monday
    workout_type: str = "running",
) -> NormalizedWorkout:
    """Create a minimal NormalizedWorkout for testing."""
    started_at = datetime(2025, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    return NormalizedWorkout(
        started_at=started_at,
        local_date=local_date,
        workout_type=workout_type,
        attribution={"provider": "withings"},
    )


def _make_commitment(
    *,
    id: str = "c001",
    bot_id: str = "hector",
    topic_slug: str = "fitness",
    cadence: str = "daily",
    start_date: date | str | None = date(2025, 1, 1),
    end_date: date | str | None = None,
    days_of_week: list[int] | None = None,
    target_count: int | None = None,
) -> dict:
    """Create a minimal commitment dict."""
    return {
        "id": id,
        "bot_id": bot_id,
        "topic_slug": topic_slug,
        "label": "Test Commitment",
        "cadence": cadence,
        "start_date": start_date.isoformat() if isinstance(start_date, date) else start_date,
        "end_date": end_date.isoformat() if isinstance(end_date, date) else end_date,
        "days_of_week": days_of_week or [],
        "target_count": target_count,
        "schedule_rule": {},
        "user_id": "u001",
        "status": "active",
    }


# ── Test: no local_date ─────────────────────────────────────────────────────


class TestNoLocalDate:
    def test_workout_without_local_date_returns_no_local_date_reason(self):
        workout = _make_workout(local_date=None)
        commitments = [_make_commitment()]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_LOCAL_DATE
        assert decision.candidates_considered == 0

    def test_no_local_date_even_with_valid_commitments(self):
        workout = _make_workout(local_date=None)
        commitments = [_make_commitment(cadence="daily")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_LOCAL_DATE


# ── Test: unknown workout type ──────────────────────────────────────────────


class TestUnknownWorkoutType:
    def test_unknown_workout_type_rejected(self):
        workout = _make_workout(workout_type="unknown")
        commitments = [_make_commitment()]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_UNKNOWN_WORKOUT_TYPE
        assert decision.candidates_considered == 0

    def test_unmapped_workout_type_rejected(self):
        # "surfing" is in the Withings taxonomy but NOT in
        # HECTOR_FITNESS_TAXONOMY_LABELS (it's not a broadcast type).
        workout = _make_workout(workout_type="surfing")
        commitments = [_make_commitment()]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_UNKNOWN_WORKOUT_TYPE

    def test_unknown_type_checked_before_commitment_filtering(self):
        workout = _make_workout(workout_type="unknown")
        # Even with valid Hector fitness commitments, unknown type fails.
        commitments = [_make_commitment()]
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_UNKNOWN_WORKOUT_TYPE
        assert decision.candidates_considered == 0


# ── Test: zero active commitments ───────────────────────────────────────────


class TestZeroActiveCommitments:
    def test_empty_commitment_list(self):
        workout = _make_workout()
        decision = project_workout(workout, commitments=[])
        assert decision.matched is None
        assert decision.reason == _REASON_ZERO_ACTIVE_COMMITMENTS
        assert decision.candidates_considered == 0


# ── Test: wrong bot/topic ───────────────────────────────────────────────────


class TestWrongBotOrTopic:
    def test_non_hector_bot_rejected(self):
        workout = _make_workout()
        commitments = [_make_commitment(bot_id="superpom")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_HECTOR_FITNESS_COMMITMENTS
        assert decision.candidates_considered == 1

    def test_non_fitness_topic_rejected(self):
        workout = _make_workout()
        commitments = [_make_commitment(bot_id="hector", topic_slug="habits")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_HECTOR_FITNESS_COMMITMENTS
        assert decision.candidates_considered == 1

    def test_both_wrong_bot_and_topic(self):
        workout = _make_workout()
        commitments = [_make_commitment(bot_id="habits", topic_slug="habits")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_HECTOR_FITNESS_COMMITMENTS

    def test_mixed_valid_and_invalid_commitments_filters_correctly(self):
        """When some commitments have wrong bot/topic, only valid ones
        are considered for matching."""
        workout = _make_workout(workout_type="running", local_date=date(2025, 6, 16))
        commitments = [
            _make_commitment(id="c-bad", bot_id="superpom", topic_slug="fitness", cadence="daily"),
            _make_commitment(id="c-good", bot_id="hector", topic_slug="fitness", cadence="daily"),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-good"
        assert decision.reason == _REASON_MATCHED
        # candidates_considered is the number of Hector fitness commitments
        assert decision.candidates_considered == 1


# ── Test: no eligible slot ──────────────────────────────────────────────────


class TestNoEligibleSlot:
    def test_date_before_commitment_start(self):
        workout = _make_workout(local_date=date(2025, 1, 1))
        commitments = [_make_commitment(start_date=date(2025, 6, 1), cadence="daily")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT
        assert decision.candidates_considered == 1

    def test_date_after_commitment_end(self):
        workout = _make_workout(local_date=date(2025, 12, 31))
        commitments = [_make_commitment(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 6, 30),
            cadence="daily",
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT

    def test_weekday_cadence_on_weekend(self):
        # Sunday
        workout = _make_workout(local_date=date(2025, 6, 15))
        commitments = [_make_commitment(cadence="weekdays")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT

    def test_custom_days_wrong_day(self):
        # Monday, but commitment only expects Wed/Fri
        workout = _make_workout(local_date=date(2025, 6, 16))  # Monday
        commitments = [_make_commitment(
            cadence="custom_days",
            days_of_week=[2, 4],  # Wed, Fri
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT


# ── Test: ambiguous multiple commitments ────────────────────────────────────


class TestAmbiguousMultipleCommitments:
    def test_two_commitments_both_eligible(self):
        workout = _make_workout(local_date=date(2025, 6, 16), workout_type="running")
        commitments = [
            _make_commitment(id="c1", cadence="daily"),
            _make_commitment(id="c2", cadence="daily"),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS
        assert decision.candidates_considered == 2

    def test_three_commitments_all_eligible(self):
        workout = _make_workout(local_date=date(2025, 6, 16))
        commitments = [
            _make_commitment(id="c1", cadence="daily"),
            _make_commitment(id="c2", cadence="daily"),
            _make_commitment(id="c3", cadence="daily"),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS
        assert decision.candidates_considered == 3


# ── Test: happy path — exactly one match ────────────────────────────────────


class TestHappyPath:
    def test_single_daily_commitment_matches(self):
        workout = _make_workout(local_date=date(2025, 6, 16), workout_type="running")
        commitments = [_make_commitment(id="c-run", cadence="daily")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-run"
        assert decision.matched.matched_local_date == date(2025, 6, 16)
        assert decision.reason == _REASON_MATCHED
        assert decision.candidates_considered == 1

    def test_weekday_cadence_on_weekday(self):
        # Monday
        workout = _make_workout(local_date=date(2025, 6, 16), workout_type="cycling")
        commitments = [_make_commitment(id="c-bike", cadence="weekdays")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-bike"

    def test_custom_days_matching_day(self):
        # Wednesday
        workout = _make_workout(local_date=date(2025, 6, 18), workout_type="strength")
        commitments = [_make_commitment(
            id="c-gym",
            cadence="custom_days",
            days_of_week=[2, 4],  # Wed, Fri
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-gym"

    def test_weekly_count_any_day_in_week(self):
        # Any day within the week works for weekly_count
        workout = _make_workout(local_date=date(2025, 6, 17), workout_type="yoga")
        commitments = [_make_commitment(id="c-yoga", cadence="weekly_count")]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-yoga"

    def test_custom_cadence_within_range(self):
        workout = _make_workout(local_date=date(2025, 3, 15), workout_type="swimming")
        commitments = [_make_commitment(
            id="c-swim",
            cadence="custom",
            start_date=date(2025, 3, 1),
            end_date=date(2025, 3, 31),
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None

    def test_all_taxonomy_types_are_accepted(self):
        """Every type in HECTOR_FITNESS_TAXONOMY_LABELS should be matchable."""
        from app.services.health_sync.models import HECTOR_FITNESS_TAXONOMY_LABELS
        commitment = _make_commitment(id="c-all", cadence="daily")
        for wtype in sorted(HECTOR_FITNESS_TAXONOMY_LABELS):
            workout = _make_workout(local_date=date(2025, 6, 16), workout_type=wtype)
            decision = project_workout(workout, commitments=[commitment])
            assert decision.reason == _REASON_MATCHED, f"Type '{wtype}' should match"
            assert decision.matched is not None

    def test_end_date_none_means_unbounded(self):
        """Commitment with end_date=None should match any date >= start_date."""
        workout = _make_workout(local_date=date(2025, 12, 1))
        commitments = [_make_commitment(id="c-forever", cadence="daily", end_date=None)]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None


# ── Test: reason_is_projecting helper ───────────────────────────────────────


class TestReasonIsProjecting:
    def test_matched_is_projecting(self):
        assert reason_is_projecting(_REASON_MATCHED) is True

    def test_all_other_reasons_are_not_projecting(self):
        non_matches = [
            _REASON_NO_LOCAL_DATE,
            _REASON_UNKNOWN_WORKOUT_TYPE,
            _REASON_ZERO_ACTIVE_COMMITMENTS,
            _REASON_NO_HECTOR_FITNESS_COMMITMENTS,
            _REASON_NO_ELIGIBLE_SLOT,
            _REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS,
        ]
        for reason in non_matches:
            assert reason_is_projecting(reason) is False, f"'{reason}' should NOT be projecting"


# ── Test: edge cases ────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_start_date_none_daily_matches_any_date(self):
        """Commitment with start_date=None should match any date (unbounded start)."""
        workout = _make_workout(local_date=date(2020, 1, 1))
        commitments = [_make_commitment(id="c-any", cadence="daily", start_date=None)]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None

    def test_start_date_none_custom_matches_any_date(self):
        workout = _make_workout(local_date=date(2020, 1, 1))
        commitments = [_make_commitment(id="c-any", cadence="custom", start_date=None)]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None

    def test_weekly_count_with_future_start_date_rejected(self):
        """weekly_count with start_date after the workout week is not eligible."""
        workout = _make_workout(local_date=date(2025, 1, 6))  # Monday
        commitments = [_make_commitment(
            id="c-future",
            cadence="weekly_count",
            start_date=date(2025, 2, 1),  # way after the workout
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT

    def test_weekly_count_with_past_end_date_rejected(self):
        """weekly_count with end_date before workout week is not eligible."""
        workout = _make_workout(local_date=date(2025, 6, 16))
        commitments = [_make_commitment(
            id="c-past",
            cadence="weekly_count",
            start_date=date(2025, 1, 1),
            end_date=date(2025, 1, 31),  # well before the workout
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT

    def test_user_timezone_accepted_but_not_used(self):
        """user_timezone parameter is accepted for forward compatibility."""
        workout = _make_workout()
        commitments = [_make_commitment(id="c1", cadence="daily")]
        decision = project_workout(
            workout,
            commitments=commitments,
            user_timezone="America/New_York",
        )
        assert decision.matched is not None

    def test_projection_version_accepted_but_not_used(self):
        """projection_version parameter is accepted for forward compatibility."""
        workout = _make_workout()
        commitments = [_make_commitment(id="c1", cadence="daily")]
        decision = project_workout(
            workout,
            commitments=commitments,
            projection_version=3,
        )
        assert decision.matched is not None

    def test_workout_on_commitment_start_date_boundary(self):
        """Workout on the exact start_date should match."""
        workout = _make_workout(local_date=date(2025, 6, 1))
        commitments = [_make_commitment(
            id="c-start",
            cadence="daily",
            start_date=date(2025, 6, 1),
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None

    def test_workout_on_commitment_end_date_boundary(self):
        """Workout on the exact end_date should match."""
        workout = _make_workout(local_date=date(2025, 6, 30))
        commitments = [_make_commitment(
            id="c-end",
            cadence="daily",
            start_date=date(2025, 6, 1),
            end_date=date(2025, 6, 30),
        )]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None

    def test_missing_bot_id_field_treated_as_empty(self):
        """Commitment dict without bot_id key should not match."""
        workout = _make_workout()
        c = _make_commitment(id="c-no-bot")
        del c["bot_id"]
        decision = project_workout(workout, commitments=[c])
        assert decision.reason == _REASON_NO_HECTOR_FITNESS_COMMITMENTS

    def test_missing_topic_slug_field_treated_as_empty(self):
        """Commitment dict without topic_slug key should not match."""
        workout = _make_workout()
        c = _make_commitment(id="c-no-topic")
        del c["topic_slug"]
        decision = project_workout(workout, commitments=[c])
        assert decision.reason == _REASON_NO_HECTOR_FITNESS_COMMITMENTS


# ── Test: DST-local-date eligibility ────────────────────────────────────────


class TestDSTLocalDateEligibility:
    """The matcher works on ``date`` objects, so DST transitions in the
    user's timezone do not change slot-eligibility.  These tests verify
    that workouts whose ``local_date`` falls on DST transition days still
    project correctly, and that the matcher preserves the
    ``matched_local_date`` transparently regardless of DST."""

    def test_spring_forward_date_matches_daily_commitment(self):
        """2025-03-09 is US spring-forward (clocks 02:00→03:00)."""
        workout = _make_workout(
            local_date=date(2025, 3, 9),  # spring-forward Sunday
            workout_type="running",
        )
        commitments = [_make_commitment(id="c-dst1", cadence="daily")]
        decision = project_workout(
            workout, commitments=commitments,
            user_timezone="America/New_York",
        )
        assert decision.matched is not None
        assert decision.matched.matched_local_date == date(2025, 3, 9)

    def test_fall_back_date_matches_daily_commitment(self):
        """2025-11-02 is US fall-back (clocks 02:00→01:00)."""
        workout = _make_workout(
            local_date=date(2025, 11, 2),  # fall-back Sunday
            workout_type="cycling",
        )
        commitments = [_make_commitment(id="c-dst2", cadence="daily")]
        decision = project_workout(
            workout, commitments=commitments,
            user_timezone="America/New_York",
        )
        assert decision.matched is not None
        assert decision.matched.matched_local_date == date(2025, 11, 2)

    def test_spring_forward_date_with_weekday_cadence_on_sunday_rejected(self):
        """Sunday during spring-forward is not a weekday slot."""
        workout = _make_workout(
            local_date=date(2025, 3, 9),  # Sunday
            workout_type="running",
        )
        commitments = [_make_commitment(id="c-wd", cadence="weekdays")]
        decision = project_workout(
            workout, commitments=commitments,
            user_timezone="America/New_York",
        )
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT

    def test_spring_forward_monday_matches_weekday_cadence(self):
        """Monday after spring-forward is a normal weekday."""
        workout = _make_workout(
            local_date=date(2025, 3, 10),  # Monday after spring-forward
            workout_type="strength",
        )
        commitments = [_make_commitment(id="c-wd2", cadence="weekdays")]
        decision = project_workout(
            workout, commitments=commitments,
            user_timezone="America/New_York",
        )
        assert decision.matched is not None

    def test_dst_date_with_custom_days_matches(self):
        """Custom-days cadence on DST transition day still uses weekday index."""
        # 2025-03-09 is Sunday (weekday 6)
        workout = _make_workout(
            local_date=date(2025, 3, 9),
            workout_type="yoga",
        )
        commitments = [_make_commitment(
            id="c-custom-dst",
            cadence="custom_days",
            days_of_week=[6],  # Sunday
        )]
        decision = project_workout(
            workout, commitments=commitments,
            user_timezone="America/New_York",
        )
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-custom-dst"

    def test_user_timezone_during_dst_preserves_local_date(self):
        """The timezone parameter is accepted; matched_local_date is unchanged."""
        workout = _make_workout(
            local_date=date(2025, 10, 15),
            workout_type="hiking",
        )
        commitments = [_make_commitment(id="c-tz", cadence="daily")]
        decision = project_workout(
            workout, commitments=commitments,
            user_timezone="Europe/London",
        )
        assert decision.matched is not None
        assert decision.matched.matched_local_date == date(2025, 10, 15)

    def test_dst_date_ambiguous_multiple_still_rejected(self):
        """DST doesn't change ambiguous-multiple logic."""
        workout = _make_workout(
            local_date=date(2025, 3, 9),
            workout_type="running",
        )
        commitments = [
            _make_commitment(id="c-a", cadence="daily"),
            _make_commitment(id="c-b", cadence="daily"),
        ]
        decision = project_workout(
            workout, commitments=commitments,
            user_timezone="America/New_York",
        )
        assert decision.reason == _REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS
        assert decision.candidates_considered == 2


# ── Test: wrong user ────────────────────────────────────────────────────────


class TestWrongUser:
    """The matcher intentionally does **not** filter by ``user_id``.
    It trusts the caller to pass only the current user's commitments.
    These tests document that contract and protect against accidental
    user-scoped filtering that could break the pure-matcher design."""

    def test_commitment_with_different_user_id_still_matches(self):
        """A commitment with a mismatched user_id is still eligible.
        Caller is expected to pre-filter by user before invoking the matcher."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        commitments = [_make_commitment(
            id="c-other-user",
            cadence="daily",
        )]
        # Override user_id to a different value — matcher doesn't check it.
        commitments[0]["user_id"] = "u-other"
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-other-user"
        assert decision.reason == _REASON_MATCHED

    def test_all_commitments_wrong_user_no_user_filter_applied(self):
        """Even when every commitment has a different user_id, the matcher
        does not reject — it only filters by bot_id and topic_slug, then
        checks slot eligibility."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="cycling",
        )
        commitments = [
            _make_commitment(id="c-u1", cadence="daily"),
            _make_commitment(id="c-u2", cadence="daily"),
        ]
        for c in commitments:
            c["user_id"] = "u-stranger"
        # Both are Hector fitness with daily cadence → ambiguous (not
        # rejected for wrong user).
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS
        assert decision.candidates_considered == 2

    def test_wrong_user_commitment_with_no_slot_still_checks_slot(self):
        """A wrong-user commitment on the wrong day still fails on
        slot eligibility — user_id is never consulted."""
        workout = _make_workout(
            local_date=date(2025, 6, 15),  # Sunday
            workout_type="running",
        )
        commitments = [_make_commitment(
            id="c-wrong-user-weekday",
            cadence="weekdays",
        )]
        commitments[0]["user_id"] = "u-stranger"
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT
        assert decision.candidates_considered == 1

    def test_wrong_user_compared_to_caller_pre_filter_note(self):
        """Demonstrate that a commitment with the 'correct' user_id
        matches exactly the same as one with a 'wrong' user_id.
        This is the caller-pre-filter contract at work."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="swimming",
        )
        # Correct user
        c_correct = _make_commitment(id="c-correct", cadence="daily")
        c_correct["user_id"] = "u-caller"
        d_good = project_workout(workout, commitments=[c_correct])
        assert d_good.matched is not None

        # Wrong user — same result
        c_wrong = _make_commitment(id="c-wrong", cadence="daily")
        c_wrong["user_id"] = "u-stranger"
        d_wrong = project_workout(workout, commitments=[c_wrong])
        assert d_wrong.matched is not None

        # The matcher treats both identically.
        assert d_good.reason == d_wrong.reason == _REASON_MATCHED


# ── Test: wrong metric / source type ─────────────────────────────────────────


class TestWrongMetricOrSourceType:
    """The matcher only rejects workout types that are absent from
    ``HECTOR_FITNESS_TAXONOMY_LABELS``.  It does **not** inspect the
    provider attribution, device info, or metric data fields — those are
    irrelevant to slot-based commitment matching."""

    # -- Already covered by TestUnknownWorkoutType, but re-asserted here
    #    under the explicit "wrong metric/source" banner per the plan.

    def test_withings_only_type_not_in_fitness_taxonomy_rejected(self):
        """Types in the Withings→Hector map that are NOT broadcast fitness
        types (e.g. 'surfing') are rejected."""
        workout = _make_workout(workout_type="surfing")
        decision = project_workout(workout, commitments=[_make_commitment()])
        assert decision.reason == _REASON_UNKNOWN_WORKOUT_TYPE

    def test_completely_unknown_label_rejected(self):
        """An arbitrary string not in any taxonomy is rejected."""
        workout = _make_workout(workout_type="sleep_measurement")
        decision = project_workout(workout, commitments=[_make_commitment()])
        assert decision.reason == _REASON_UNKNOWN_WORKOUT_TYPE

    def test_different_attribution_source_still_matches(self):
        """Attribution from a non-Withings source does not block matching."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        # Simulate a workout from a different provider.
        object.__setattr__(workout, "attribution", {"provider": "garmin"})
        decision = project_workout(workout, commitments=[_make_commitment()])
        assert decision.matched is not None
        assert decision.reason == _REASON_MATCHED

    def test_attribution_field_is_ignored_for_matching(self):
        """The matcher never inspects attribution — only workout_type,
        local_date, bot_id, topic_slug, and cadence/slot info."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="cycling",
        )
        object.__setattr__(workout, "attribution", {})
        decision = project_workout(workout, commitments=[_make_commitment()])
        assert decision.matched is not None

    def test_device_fields_ignored_for_matching(self):
        """source_device_id and source_device_model do not affect matching."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        object.__setattr__(workout, "source_device_id", "abc-123")
        object.__setattr__(workout, "source_device_model", "ScanWatch 2")
        decision = project_workout(workout, commitments=[_make_commitment()])
        assert decision.matched is not None

    def test_metric_data_fields_ignored_for_matching(self):
        """Duration, distance, steps, energy etc. are irrelevant to matching."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        object.__setattr__(workout, "duration_seconds", 3600)
        object.__setattr__(workout, "distance_meters", 10000.0)
        object.__setattr__(workout, "steps", 12000)
        object.__setattr__(workout, "energy_kcal", 500.0)
        object.__setattr__(workout, "average_heart_rate_bpm", 145.0)
        decision = project_workout(workout, commitments=[_make_commitment()])
        assert decision.matched is not None

    def test_zero_metric_values_still_match(self):
        """Zero-valued optional metrics don't block projection."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="walking",
        )
        object.__setattr__(workout, "duration_seconds", 0)
        object.__setattr__(workout, "distance_meters", 0.0)
        object.__setattr__(workout, "energy_kcal", 0.0)
        decision = project_workout(workout, commitments=[_make_commitment()])
        assert decision.matched is not None


# ── Test: zero candidates (after filtering) ──────────────────────────────────


class TestZeroCandidatesAfterFiltering:
    """Cover the scenario where commitments exist but none survive the
    Hector-fitness bot/topic filter or slot-eligibility check."""

    def test_all_commitments_filtered_by_bot_id(self):
        """Every commitment has a non-hector bot_id → zero candidates."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        commitments = [
            _make_commitment(id="c1", bot_id="superpom", cadence="daily"),
            _make_commitment(id="c2", bot_id="habits", cadence="daily"),
            _make_commitment(id="c3", bot_id="sage", cadence="daily"),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is None
        assert decision.reason == _REASON_NO_HECTOR_FITNESS_COMMITMENTS
        assert decision.candidates_considered == 3

    def test_all_commitments_filtered_by_topic_slug(self):
        """Every commitment has a non-fitness topic → zero candidates."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        commitments = [
            _make_commitment(id="c1", bot_id="hector", topic_slug="habits", cadence="daily"),
            _make_commitment(id="c2", bot_id="hector", topic_slug="sleep", cadence="daily"),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_NO_HECTOR_FITNESS_COMMITMENTS
        assert decision.candidates_considered == 2

    def test_mix_of_filtered_and_ineligible_slots(self):
        """One Hector-fitness candidate exists but date is outside its range."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        commitments = [
            _make_commitment(id="c-bad-bot", bot_id="sage",
                             topic_slug="fitness", cadence="daily"),
            _make_commitment(id="c-hector", bot_id="hector",
                             topic_slug="fitness", cadence="daily",
                             start_date=date(2025, 12, 1)),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.reason == _REASON_NO_ELIGIBLE_SLOT
        # Only 1 Hector-fitness candidate was considered.
        assert decision.candidates_considered == 1


# ── Test: one-candidate projection (explicit) ────────────────────────────────


class TestOneCandidateProjection:
    """Explicitly verify the exactly-one-eligible-candidate happy path
    across a variety of cadence, date, and filter scenarios."""

    def test_single_daily_candidate_direct_match(self):
        """The simplest case: one commitment, daily cadence, in-range date."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="running",
        )
        decision = project_workout(
            workout, commitments=[_make_commitment(id="c-one", cadence="daily")],
        )
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-one"
        assert decision.candidates_considered == 1
        assert decision.reason == _REASON_MATCHED

    def test_one_eligible_among_filtered_competitors(self):
        """One Hector-fitness commitment is eligible; the others are
        filtered by wrong bot or wrong topic."""
        workout = _make_workout(
            local_date=date(2025, 6, 17),
            workout_type="hiking",
        )
        commitments = [
            _make_commitment(id="c-bad-bot", bot_id="sage",
                             topic_slug="fitness", cadence="daily"),
            _make_commitment(id="c-bad-topic", bot_id="hector",
                             topic_slug="habits", cadence="daily"),
            _make_commitment(id="c-good", bot_id="hector",
                             topic_slug="fitness", cadence="daily"),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-good"
        assert decision.candidates_considered == 1  # only c-good is HF
        assert decision.reason == _REASON_MATCHED

    def test_one_eligible_with_another_out_of_range(self):
        """Two Hector-fitness commitments, but only one has the date in
        its slot range."""
        workout = _make_workout(
            local_date=date(2025, 6, 16),
            workout_type="strength",
        )
        commitments = [
            _make_commitment(id="c-out", bot_id="hector",
                             topic_slug="fitness", cadence="daily",
                             start_date=date(2025, 9, 1)),
            _make_commitment(id="c-in", bot_id="hector",
                             topic_slug="fitness", cadence="daily",
                             start_date=date(2025, 1, 1)),
        ]
        decision = project_workout(workout, commitments=commitments)
        assert decision.matched is not None
        assert decision.matched.commitment_id == "c-in"
        assert decision.candidates_considered == 2
        assert decision.reason == _REASON_MATCHED
