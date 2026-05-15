"""Pure unit tests for the adherence computation service.

Tests app/services/commitments.py directly — no DB, no pool, no async.
Covers all 5 cadence types, all 5 slot statuses, timezone handling,
and edge cases.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from app.services.commitments import (
    compute_slots,
    classify_slots,
    get_adherence,
)


def _utc_dt(*args: int) -> datetime:
    """Create a timezone-aware UTC datetime."""
    return datetime(*args, tzinfo=dt_timezone.utc)


UTC = dt_timezone.utc
EASTERN = ZoneInfo("America/New_York")


def _make_commitment(
    *,
    label: str = "test",
    cadence: str = "daily",
    days_of_week: list[int] | None = None,
    target_count: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    kind: str = "workout",
) -> dict:
    return {
        "id": "test-id",
        "label": label,
        "cadence": cadence,
        "days_of_week": days_of_week or [],
        "target_count": target_count,
        "start_date": start_date,
        "end_date": end_date,
        "kind": kind,
        "schedule_rule": {},
    }


def _make_event(
    *,
    commitment_id: str = "test-id",
    observed_at: datetime | None = None,
    adherence_status: str | None = None,
    value_numeric: float | None = None,
    value_text: str | None = None,
    metric_key: str = "workout",
) -> dict:
    return {
        "id": "evt-id",
        "commitment_id": commitment_id,
        "observed_at": observed_at or _utc_dt(2026, 5, 13, 8, 0),
        "adherence_status": adherence_status,
        "value_numeric": value_numeric,
        "value_text": value_text,
        "metric_key": metric_key,
    }


# ── Daily cadence ──────────────────────────────────────────────────────────


class TestDailyCadence:
    def test_daily_slots_this_week(self):
        """Daily cadence produces one slot per day from Mon to today."""
        c = _make_commitment(
            cadence="daily",
            start_date="2026-05-11",  # Monday
            end_date="2026-05-17",  # Sunday
        )
        # "Today" is Wednesday May 13
        now = _utc_dt(2026, 5, 13, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        assert len(slots) == 7  # Mon-Sun
        dates = [s["date"] for s in slots]
        assert dates[0] == "2026-05-11"  # Monday
        assert dates[-1] == "2026-05-17"  # Sunday

    def test_daily_with_done_and_missed(self):
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 13, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(observed_at=_utc_dt(2026, 5, 11, 8, 0), adherence_status="done"),
            _make_event(observed_at=_utc_dt(2026, 5, 12, 8, 0), adherence_status="missed"),
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        # Monday: done
        assert classified[0]["status"] == "done"
        # Tuesday: missed
        assert classified[1]["status"] == "missed"
        # Wednesday (today): pending
        assert classified[2]["status"] == "pending"

    def test_past_no_event_is_unknown(self):
        """Slots in the past with no event are 'unknown', not 'pending'."""
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)  # Thursday — Mon-Wed are past
        slots = compute_slots(c, EASTERN, now)
        classified = classify_slots(slots, [], EASTERN, now)
        # Mon-Wed are past → unknown
        assert classified[0]["status"] == "unknown"  # Mon
        assert classified[1]["status"] == "unknown"  # Tue
        assert classified[2]["status"] == "unknown"  # Wed
        # Thu (today) → pending
        assert classified[3]["status"] == "pending"

    def test_today_no_event_is_pending(self):
        """Today's slot with no event is 'pending', not 'unknown'."""
        c = _make_commitment(cadence="daily", start_date="2026-05-13")
        now = _utc_dt(2026, 5, 13, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        classified = classify_slots(slots, [], EASTERN, now)
        assert classified[0]["status"] == "pending"

    def test_excused_event(self):
        c = _make_commitment(cadence="daily", start_date="2026-05-13")
        now = _utc_dt(2026, 5, 13, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(observed_at=_utc_dt(2026, 5, 13, 8, 0), adherence_status="excused"),
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        assert classified[0]["status"] == "excused"


# ── Weekdays cadence ───────────────────────────────────────────────────────


class TestWeekdaysCadence:
    def test_weekdays_only_mon_to_fri(self):
        c = _make_commitment(cadence="weekdays", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 13, 12, 0)  # Wednesday
        slots = compute_slots(c, EASTERN, now)
        # Mon-Fri = 5 slots
        assert len(slots) == 5
        for s in slots:
            d = date.fromisoformat(s["date"])
            assert d.weekday() < 5, f"Got weekend day: {d}"

    def test_weekdays_weekend_excluded(self):
        """Weekend days are not in the slot grid."""
        c = _make_commitment(cadence="weekdays", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 17, 12, 0)  # Sunday
        slots = compute_slots(c, EASTERN, now)
        dates = {s["date"] for s in slots}
        # Sat 5/16 and Sun 5/17 should not be present
        assert "2026-05-16" not in dates
        assert "2026-05-17" not in dates


# ── Weekly count cadence ───────────────────────────────────────────────────


class TestWeeklyCount:
    def test_weekly_count_has_summary_slot(self):
        c = _make_commitment(cadence="weekly_count", target_count=3, start_date="2026-05-11")
        now = _utc_dt(2026, 5, 13, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        assert len(slots) == 1
        assert slots[0]["is_weekly_count"] is True
        assert slots[0]["target_count"] == 3

    def test_weekly_count_summary(self):
        c = _make_commitment(
            label="Workouts",
            cadence="weekly_count",
            target_count=3,
            start_date="2026-05-11",
        )
        now = _utc_dt(2026, 5, 13, 12, 0)
        events = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 11, 8, 0),
                adherence_status="done",
            ),
            _make_event(
                observed_at=_utc_dt(2026, 5, 12, 8, 0),
                adherence_status="done",
            ),
        ]
        result = get_adherence([c], events, EASTERN, now)
        assert len(result["commitments"]) == 1
        summary = result["commitments"][0]["summary"]
        assert "2/3 done" in summary
        assert "1 remaining" in summary


# ── Custom days cadence ────────────────────────────────────────────────────


class TestCustomDays:
    def test_custom_days_specific_days(self):
        """custom_days with [1, 3, 5] = Tue, Thu, Sat."""
        c = _make_commitment(
            cadence="custom_days",
            days_of_week=[1, 3, 5],  # Tue, Thu, Sat
            start_date="2026-05-11",
        )
        now = _utc_dt(2026, 5, 13, 12, 0)  # Wednesday
        slots = compute_slots(c, EASTERN, now)
        dates = {date.fromisoformat(s["date"]).weekday() for s in slots}
        assert dates == {1, 3, 5}  # Only Tue, Thu, Sat

    def test_custom_days_with_events(self):
        c = _make_commitment(
            cadence="custom_days",
            days_of_week=[1, 3, 5],
            start_date="2026-05-11",
        )
        now = _utc_dt(2026, 5, 16, 12, 0)  # Saturday
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(observed_at=_utc_dt(2026, 5, 12, 8, 0), adherence_status="done"),  # Tue
            _make_event(observed_at=_utc_dt(2026, 5, 14, 8, 0), adherence_status="missed"),  # Thu
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        # Tue (past): done
        assert classified[0]["status"] == "done"
        # Thu (past): missed
        assert classified[1]["status"] == "missed"
        # Sat (today, no event): pending
        assert classified[2]["status"] == "pending"


# ── Custom date window cadence ─────────────────────────────────────────────


class TestCustomCadence:
    def test_custom_date_window(self):
        c = _make_commitment(
            cadence="custom",
            start_date="2026-05-13",
            end_date="2026-05-15",
        )
        now = _utc_dt(2026, 5, 14, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        assert len(slots) == 3  # 5/13, 5/14, 5/15
        assert slots[0]["date"] == "2026-05-13"
        assert slots[-1]["date"] == "2026-05-15"


# ── Full get_adherence() integration ───────────────────────────────────────


class TestGetAdherence:
    def test_empty_commitments(self):
        result = get_adherence([], [], EASTERN, _utc_dt(2026, 5, 13, 12, 0))
        assert result["commitments"] == []
        assert result["period_label"] == "this week"

    def test_period_totals(self):
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)  # Thursday — Mon-Wed are past
        events = [
            _make_event(observed_at=_utc_dt(2026, 5, 11, 8, 0), adherence_status="done"),
            _make_event(observed_at=_utc_dt(2026, 5, 12, 8, 0), adherence_status="missed"),
            _make_event(observed_at=_utc_dt(2026, 5, 13, 8, 0), adherence_status="excused"),
        ]
        result = get_adherence([c], events, EASTERN, now)
        totals = result["commitments"][0]["period_totals"]
        assert totals["done"] == 1
        assert totals["missed"] == 1
        assert totals["excused"] == 1
        # Thu (today) = pending (1 slot). No end_date means slots only to today.
        assert totals["pending"] == 1
        assert totals["unknown"] == 0  # all past slots have events

    def test_unknown_is_never_stored_in_events(self):
        """Unknown is purely derived from the absence of events on past dates."""
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)
        # No events at all
        result = get_adherence([c], [], EASTERN, now)
        totals = result["commitments"][0]["period_totals"]
        # Past slots (Mon-Wed) are unknown (3). Today (Thu) is pending (1).
        assert totals["unknown"] == 3
        assert totals["pending"] == 1

    def test_week_start_and_end(self):
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)  # Thursday
        result = get_adherence([c], [], EASTERN, now)
        assert result["week_start"] == "2026-05-11"  # Monday
        assert result["week_end"] == "2026-05-17"  # Sunday


# ── Nutrition patterns ─────────────────────────────────────────────────────


class TestNutritionPatterns:
    def test_positive_nutrition_done(self):
        """A positive nutrition commitment (ate_on_plan) satisfied by done event."""
        c = _make_commitment(
            label="Cook dinner",
            cadence="daily",
            start_date="2026-05-13",
            kind="nutrition",
        )
        now = _utc_dt(2026, 5, 13, 20, 0)
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 13, 19, 0),
                adherence_status="done",
                metric_key="ate_on_plan",
            ),
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        assert classified[0]["status"] == "done"

    def test_nutrition_missed_takeout(self):
        """A takeout_night event marks the nutrition slot as missed."""
        c = _make_commitment(
            label="Cook dinner",
            cadence="daily",
            start_date="2026-05-13",
            kind="nutrition",
        )
        now = _utc_dt(2026, 5, 13, 20, 0)
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 13, 19, 0),
                adherence_status="missed",
                metric_key="takeout_night",
            ),
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        assert classified[0]["status"] == "missed"


# ── Timezone handling ──────────────────────────────────────────────────────


class TestTimezoneHandling:
    def test_slots_in_local_timezone(self):
        """Slots are computed in user's local timezone, not UTC."""
        # Use a UTC time that is already Wednesday in Eastern time
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        # 2026-05-13 04:00 UTC = 2026-05-13 00:00 Eastern (same day)
        now = _utc_dt(2026, 5, 13, 4, 0)
        slots = compute_slots(c, EASTERN, now)
        # "Today" should be May 13 in Eastern
        today_slots = [s for s in slots if "Today" in s["label"]]
        assert len(today_slots) == 1
        assert today_slots[0]["date"] == "2026-05-13"

    def test_utc_midnight_boundary(self):
        """Day boundary is in user local time, not UTC."""
        c = _make_commitment(cadence="daily", start_date="2026-05-12")
        # 2026-05-13 03:00 UTC = 2026-05-12 23:00 Eastern (still Tuesday!)
        now = _utc_dt(2026, 5, 13, 3, 0)
        slots = compute_slots(c, EASTERN, now)
        today_slots = [s for s in slots if "Today" in s["label"]]
        # "Today" is May 12 in Eastern
        assert today_slots[0]["date"] == "2026-05-12"

    def test_pacific_timezone(self):
        """Different timezone produces correct slots."""
        pacific = ZoneInfo("America/Los_Angeles")
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        # 2026-05-13 08:00 UTC = 2026-05-13 01:00 Pacific
        now = _utc_dt(2026, 5, 13, 8, 0)
        slots = compute_slots(c, pacific, now)
        today_slots = [s for s in slots if "Today" in s["label"]]
        assert today_slots[0]["date"] == "2026-05-13"
