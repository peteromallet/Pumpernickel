"""Tests for app/reflections/periods.py — period resolution in user timezone.

Covers:
  - Day/week/month/custom/instant/none period resolution.
  - User-timezone rollover boundaries (midnight crossing).
  - Content-override: user text that names a different scope.
  - Content-vs-clock conflicts resolved in favour of content.
  - Separation of period resolution from capture eligibility.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.reflections.periods import (
    PeriodResult,
    VALID_TEMPORAL_SCOPES,
    resolve_period,
    resolve_from_classification,
    _detect_content_scope_override,
)


# ── Helpers ─────────────────────────────────────────────────────────────────

UTC = timezone.utc
NYC = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
LONDON = ZoneInfo("Europe/London")


def _dt(year: int, month: int, day: int, hour: int = 12, minute: int = 0, tz=UTC):
    """Create a timezone-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=tz)


# ── PeriodResult invariants ─────────────────────────────────────────────────


class TestPeriodResult:
    """Test PeriodResult dataclass invariants."""

    def test_valid_construction(self):
        start = _dt(2026, 7, 20, 0, 0)
        end = _dt(2026, 7, 21, 0, 0)
        result = PeriodResult(
            temporal_scope="day",
            period_start=start,
            period_end=end,
            timezone="UTC",
            source="clock_time",
        )
        assert result.temporal_scope == "day"
        assert result.period_start == start
        assert result.period_end == end

    def test_invalid_scope_raises(self):
        with pytest.raises(ValueError, match="invalid temporal_scope"):
            PeriodResult(
                temporal_scope="bogus",
                period_start=_dt(2026, 7, 20),
                period_end=_dt(2026, 7, 21),
                timezone="UTC",
                source="test",
            )

    def test_start_must_be_before_end(self):
        with pytest.raises(ValueError, match="must be before"):
            PeriodResult(
                temporal_scope="day",
                period_start=_dt(2026, 7, 21),
                period_end=_dt(2026, 7, 20),
                timezone="UTC",
                source="test",
            )

    def test_start_must_be_tz_aware(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            PeriodResult(
                temporal_scope="day",
                period_start=datetime(2026, 7, 20, 0, 0),
                period_end=_dt(2026, 7, 21),
                timezone="UTC",
                source="test",
            )

    def test_end_must_be_tz_aware(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            PeriodResult(
                temporal_scope="day",
                period_start=_dt(2026, 7, 20),
                period_end=datetime(2026, 7, 21, 0, 0),
                timezone="UTC",
                source="test",
            )

    def test_start_equal_end_raises(self):
        dt_val = _dt(2026, 7, 20, 0, 0)
        with pytest.raises(ValueError, match="must be before"):
            PeriodResult(
                temporal_scope="day",
                period_start=dt_val,
                period_end=dt_val,
                timezone="UTC",
                source="test",
            )


# ── Day resolution ──────────────────────────────────────────────────────────


class TestDayResolution:
    """Day period: midnight-to-midnight in user timezone."""

    def test_day_utc(self):
        ref = _dt(2026, 7, 20, 14, 30, tz=UTC)
        result = resolve_period("day", user_tz="UTC", reference_dt=ref)
        assert result.temporal_scope == "day"
        assert result.period_start == _dt(2026, 7, 20, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 7, 21, 0, 0, tz=UTC)

    def test_day_nyc(self):
        ref = _dt(2026, 7, 20, 14, 30, tz=UTC)  # 10:30 AM NYC
        result = resolve_period("day", user_tz="America/New_York", reference_dt=ref)
        assert result.period_start.astimezone(UTC).hour == 4  # Midnight NYC = 04:00 UTC (EDT)
        assert result.period_end.astimezone(UTC).hour == 4

    def test_day_tokyo(self):
        ref = _dt(2026, 7, 20, 14, 30, tz=UTC)  # 23:30 Tokyo
        result = resolve_period("day", user_tz="Asia/Tokyo", reference_dt=ref)
        assert result.temporal_scope == "day"
        # Midnight Tokyo = 15:00 UTC previous day
        assert result.period_start.astimezone(UTC).hour == 15

    def test_day_near_midnight_utc(self):
        """At 23:59 UTC, day period should still be the current UTC day."""
        ref = _dt(2026, 7, 20, 23, 59, tz=UTC)
        result = resolve_period("day", user_tz="UTC", reference_dt=ref)
        assert result.period_start == _dt(2026, 7, 20, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 7, 21, 0, 0, tz=UTC)

    def test_day_at_midnight_utc(self):
        """At exactly midnight UTC, period should be the new day."""
        ref = _dt(2026, 7, 21, 0, 0, tz=UTC)
        result = resolve_period("day", user_tz="UTC", reference_dt=ref)
        assert result.period_start == _dt(2026, 7, 21, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 7, 22, 0, 0, tz=UTC)


# ── Week resolution ─────────────────────────────────────────────────────────


class TestWeekResolution:
    """Week period: Monday 00:00 to next Monday 00:00."""

    def test_week_monday(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)  # Monday
        result = resolve_period("week", user_tz="UTC", reference_dt=ref)
        assert result.temporal_scope == "week"
        assert result.period_start == _dt(2026, 7, 20, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 7, 27, 0, 0, tz=UTC)

    def test_week_wednesday(self):
        ref = _dt(2026, 7, 22, 14, 0, tz=UTC)  # Wednesday
        result = resolve_period("week", user_tz="UTC", reference_dt=ref)
        assert result.period_start == _dt(2026, 7, 20, 0, 0, tz=UTC)  # Monday
        assert result.period_end == _dt(2026, 7, 27, 0, 0, tz=UTC)

    def test_week_sunday(self):
        ref = _dt(2026, 7, 26, 14, 0, tz=UTC)  # Sunday
        result = resolve_period("week", user_tz="UTC", reference_dt=ref)
        assert result.period_start == _dt(2026, 7, 20, 0, 0, tz=UTC)  # Monday
        assert result.period_end == _dt(2026, 7, 27, 0, 0, tz=UTC)

    def test_week_across_month_boundary(self):
        """Week starting in one month and ending in the next."""
        ref = _dt(2026, 7, 31, 14, 0, tz=UTC)  # Friday
        result = resolve_period("week", user_tz="UTC", reference_dt=ref)
        # Monday of that week is July 27
        assert result.period_start == _dt(2026, 7, 27, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 8, 3, 0, 0, tz=UTC)

    def test_week_nyc_rollover(self):
        """Week boundaries should respect NYC timezone rollover."""
        ref = _dt(2026, 7, 21, 3, 0, tz=UTC)  # Mon 23:00 EDT → still Monday
        result = resolve_period("week", user_tz="America/New_York", reference_dt=ref)
        # In NYC, this is still Monday
        nyc_start = result.period_start.astimezone(NYC)
        assert nyc_start.weekday() == 0  # Monday


# ── Month resolution ────────────────────────────────────────────────────────


class TestMonthResolution:
    """Month period: 1st 00:00 to next month 1st 00:00."""

    def test_month_mid(self):
        ref = _dt(2026, 7, 15, 12, 0, tz=UTC)
        result = resolve_period("month", user_tz="UTC", reference_dt=ref)
        assert result.temporal_scope == "month"
        assert result.period_start == _dt(2026, 7, 1, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 8, 1, 0, 0, tz=UTC)

    def test_month_first_day(self):
        ref = _dt(2026, 7, 1, 0, 0, tz=UTC)
        result = resolve_period("month", user_tz="UTC", reference_dt=ref)
        assert result.period_start == _dt(2026, 7, 1, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 8, 1, 0, 0, tz=UTC)

    def test_month_last_day(self):
        ref = _dt(2026, 7, 31, 23, 59, tz=UTC)
        result = resolve_period("month", user_tz="UTC", reference_dt=ref)
        assert result.period_start == _dt(2026, 7, 1, 0, 0, tz=UTC)
        assert result.period_end == _dt(2026, 8, 1, 0, 0, tz=UTC)

    def test_month_december_rollover(self):
        ref = _dt(2026, 12, 15, 12, 0, tz=UTC)
        result = resolve_period("month", user_tz="UTC", reference_dt=ref)
        assert result.period_start == _dt(2026, 12, 1, 0, 0, tz=UTC)
        assert result.period_end == _dt(2027, 1, 1, 0, 0, tz=UTC)

    def test_month_nyc_boundary(self):
        """NYC late on July 31 should still be in July."""
        ref = _dt(2026, 8, 1, 3, 0, tz=UTC)  # July 31 23:00 EDT
        result = resolve_period("month", user_tz="America/New_York", reference_dt=ref)
        nyc_start = result.period_start.astimezone(NYC)
        assert nyc_start.month == 7
        assert nyc_start.day == 1


# ── Instant resolution ──────────────────────────────────────────────────────


class TestInstantResolution:
    """Instant period: a small window around the reference time."""

    def test_instant_utc(self):
        ref = _dt(2026, 7, 20, 14, 30, tz=UTC)
        result = resolve_period("instant", user_tz="UTC", reference_dt=ref)
        assert result.temporal_scope == "instant"
        # ~5-minute window centered on ref
        assert result.period_start == ref - timedelta(minutes=2, seconds=30)
        assert result.period_end == ref + timedelta(minutes=2, seconds=30)

    def test_instant_preserves_tz(self):
        ref = _dt(2026, 7, 20, 14, 30, tz=NYC)
        result = resolve_period("instant", user_tz="America/New_York", reference_dt=ref)
        assert result.temporal_scope == "instant"
        assert result.period_start.tzinfo == NYC
        assert result.period_end.tzinfo == NYC


# ── None resolution ─────────────────────────────────────────────────────────


class TestNoneResolution:
    """None scope: a placeholder 1-hour window."""

    def test_none_scope(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period("none", user_tz="UTC", reference_dt=ref)
        assert result.temporal_scope == "none"
        assert result.period_end - result.period_start == timedelta(hours=1)


# ── Content override ────────────────────────────────────────────────────────


class TestContentOverride:
    """User text hints that override the clock-derived period."""

    def test_content_override_day(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "instant",  # classified as instant
            user_tz="UTC",
            reference_dt=ref,
            user_text="Today has been amazing, I want to reflect on the whole day",
        )
        # Content says "today" → override to day
        assert result.temporal_scope == "day"
        assert result.source == "explicit_content"

    def test_content_override_week(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "instant",
            user_tz="UTC",
            reference_dt=ref,
            user_text="This week's reflection on my progress",
        )
        assert result.temporal_scope == "week"
        assert result.source == "explicit_content"

    def test_content_override_month(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "instant",
            user_tz="UTC",
            reference_dt=ref,
            user_text="Monthly review of my projects",
        )
        assert result.temporal_scope == "month"
        assert result.source == "explicit_content"

    def test_content_override_custom(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "instant",
            user_tz="UTC",
            reference_dt=ref,
            user_text="Reflecting from July 15 to July 20",
        )
        assert result.temporal_scope == "custom"
        assert result.source == "explicit_content"

    def test_content_override_does_not_override_explicit_scope(self):
        """When the classified scope is already 'week', 'today' in text
        shouldn't downgrade it to 'day'."""
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "week",
            user_tz="UTC",
            reference_dt=ref,
            user_text="My weekly review — today I focused on health",
        )
        # "weekly" wins over "today" — week scope stays
        assert result.temporal_scope == "week"

    def test_no_content_override_when_no_match(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "day",
            user_tz="UTC",
            reference_dt=ref,
            user_text="I had a great lunch",
        )
        assert result.temporal_scope == "day"
        assert result.source == "clock_time"

    def test_content_custom_with_iso_dates(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "custom",
            user_tz="UTC",
            reference_dt=ref,
            user_text="Reflection since 2026-07-15 until 2026-07-20",
        )
        assert result.temporal_scope == "custom"
        assert result.period_start.date().isoformat() == "2026-07-15"
        # end is exclusive, so 2026-07-21
        assert result.period_end.date().isoformat() == "2026-07-21"

    def test_content_custom_with_natural_dates(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "custom",
            user_tz="UTC",
            reference_dt=ref,
            user_text="Looking back from July 15, 2026 to July 19, 2026",
        )
        assert result.temporal_scope == "custom"
        assert result.period_start.date().isoformat() == "2026-07-15"
        assert result.period_end.date().isoformat() == "2026-07-20"

    def test_content_custom_only_start(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "custom",
            user_tz="UTC",
            reference_dt=ref,
            user_text="Reflection since July 15, 2026",
        )
        assert result.temporal_scope == "custom"
        assert result.period_start.date().isoformat() == "2026-07-15"


# ── Timezone rollover ───────────────────────────────────────────────────────


class TestTimezoneRollover:
    """Local midnight boundaries are correct across timezone offsets."""

    def test_nyc_eastern_midnight(self):
        """At 03:59 UTC (23:59 EDT), day should still be the local day."""
        ref = _dt(2026, 7, 20, 3, 59, tz=UTC)  # 23:59 EDT on July 19
        result = resolve_period("day", user_tz="America/New_York", reference_dt=ref)
        nyc_start = result.period_start.astimezone(NYC)
        # This should be July 19 midnight in NYC
        assert nyc_start.month == 7
        assert nyc_start.day == 19

    def test_nyc_midnight_passes(self):
        """At 04:00 UTC (00:00 EDT), day should roll over to new day."""
        ref = _dt(2026, 7, 20, 4, 0, tz=UTC)  # 00:00 EDT on July 20
        result = resolve_period("day", user_tz="America/New_York", reference_dt=ref)
        nyc_start = result.period_start.astimezone(NYC)
        assert nyc_start.day == 20

    def test_tokyo_midnight(self):
        """At 14:59 UTC (23:59 JST), day should be the local day."""
        ref = _dt(2026, 7, 20, 14, 59, tz=UTC)
        result = resolve_period("day", user_tz="Asia/Tokyo", reference_dt=ref)
        tokyo_start = result.period_start.astimezone(TOKYO)
        assert tokyo_start.day == 20

    def test_tokyo_midnight_passes(self):
        """At 15:00 UTC (00:00 JST next day), day rolls over."""
        ref = _dt(2026, 7, 20, 15, 0, tz=UTC)
        result = resolve_period("day", user_tz="Asia/Tokyo", reference_dt=ref)
        tokyo_start = result.period_start.astimezone(TOKYO)
        assert tokyo_start.day == 21

    def test_london_summer_midnight(self):
        """BST (UTC+1) midnight at 23:00 UTC."""
        ref = _dt(2026, 7, 20, 22, 59, tz=UTC)  # 23:59 BST
        result = resolve_period("day", user_tz="Europe/London", reference_dt=ref)
        london_start = result.period_start.astimezone(LONDON)
        assert london_start.day == 20

    def test_london_midnight_passes(self):
        ref = _dt(2026, 7, 20, 23, 0, tz=UTC)  # 00:00 BST next day
        result = resolve_period("day", user_tz="Europe/London", reference_dt=ref)
        london_start = result.period_start.astimezone(LONDON)
        assert london_start.day == 21


# ── Content vs clock conflict resolution ────────────────────────────────────


class TestContentVsClockConflict:
    """When content says one thing and clock says another, content wins."""

    def test_content_week_override_clock_day(self):
        """User says 'this week' — period should be week, not day."""
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "day",  # clock-derived scope
            user_tz="UTC",
            reference_dt=ref,
            user_text="My reflection for this week: I did well",
        )
        assert result.temporal_scope == "week"
        assert result.source == "explicit_content"

    def test_content_month_not_overridden_by_time(self):
        """Even at day boundary, 'this month' in text means month."""
        ref = _dt(2026, 7, 20, 4, 0, tz=UTC)  # midnight NYC
        result = resolve_period(
            "day",
            user_tz="America/New_York",
            reference_dt=ref,
            user_text="This month has been incredible",
        )
        assert result.temporal_scope == "month"
        assert result.source == "explicit_content"

    def test_no_content_hint_uses_clock_scope(self):
        """Without content override, the classified scope is used."""
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period(
            "week",
            user_tz="UTC",
            reference_dt=ref,
            user_text="I had some thoughts about things",
        )
        # "some thoughts" doesn't match any scope override pattern — stays "week"
        assert result.temporal_scope == "week"
        assert result.source == "clock_time"

    def test_period_not_blocking_capture(self):
        """Period resolution never blocks capture — that's the classifier's job.
        Even 'none' scope produces a valid period."""
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_period("none", user_tz="UTC", reference_dt=ref)
        assert result.period_start is not None
        assert result.period_end is not None
        assert result.temporal_scope == "none"


# ── resolve_from_classification ─────────────────────────────────────────────


class TestResolveFromClassification:
    """Convenience bridge from classifier output to period resolution."""

    def test_bridge_passes_through(self):
        ref = _dt(2026, 7, 20, 14, 0, tz=UTC)
        result = resolve_from_classification(
            temporal_scope="day",
            user_tz="UTC",
            reference_dt=ref,
            user_text="My daily reflection",
        )
        assert result.temporal_scope == "day"
        assert result.source == "explicit_content"

    def test_bridge_defaults(self):
        result = resolve_from_classification(
            temporal_scope="instant",
            user_tz="UTC",
        )
        assert result.temporal_scope == "instant"
        assert result.source == "clock_time"


# ── Content scope override detection ────────────────────────────────────────


class TestContentScopeOverrideDetection:
    """Test _detect_content_scope_override priority ordering."""

    def test_custom_detected_first(self):
        text = "Reflecting from July 1 to July 5 this week"
        result = _detect_content_scope_override(text)
        assert result == "custom"

    def test_month_detected(self):
        assert _detect_content_scope_override("This month was great") == "month"
        assert _detect_content_scope_override("Monthly review time") == "month"

    def test_week_detected(self):
        assert _detect_content_scope_override("This week flew by") == "week"
        assert _detect_content_scope_override("Weekly standup notes") == "week"

    def test_day_detected(self):
        assert _detect_content_scope_override("Today was productive") == "day"
        assert _detect_content_scope_override("Daily update") == "day"

    def test_no_match(self):
        assert _detect_content_scope_override("Random thought") is None
        assert _detect_content_scope_override("") is None


# ── Error handling ──────────────────────────────────────────────────────────


class TestErrorHandling:
    """Invalid inputs are caught early."""

    def test_invalid_scope(self):
        with pytest.raises(ValueError, match="invalid temporal_scope"):
            resolve_period("bogus", user_tz="UTC")

    def test_invalid_timezone(self):
        with pytest.raises(ValueError, match="Unrecognized timezone"):
            resolve_period("day", user_tz="Not/A_Real_Zone")

    def test_naive_reference_dt(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            resolve_period(
                "day",
                user_tz="UTC",
                reference_dt=datetime(2026, 7, 20, 12, 0),
            )
