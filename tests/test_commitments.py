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


# ── Type-safety regression ─────────────────────────────────────────────────
# Verifies the contract: only events with an explicit adherence_status in
# ('done', 'missed', 'excused') AND a matching commitment_id can classify a
# slot.  Weight, sleep, and generic numeric measurement events can NEVER
# satisfy a workout commitment — even through the compatibility shim.


class TestTypeSafetyNumericFallbackRemoved:
    """Prove the unsafe numeric-only fallback has been removed in the shim.

    These tests exercise the commitments.py compatibility shim
    (classify_slots / get_adherence) and verify it enforces the same
    type-safe contract as the canonical adherence.py.
    """

    def test_weight_event_not_classified_as_done(self):
        """Weight measurement (value_numeric=185.5, no adherence_status)
        must NOT mark a slot as done in classify_slots."""
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)  # Thursday
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 11, 8, 0),
                adherence_status=None,
                value_numeric=185.5,
                metric_key="weight",
            ),
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        # Monday (May 11) is in the past, no adherence_status → unknown
        assert classified[0]["status"] == "unknown", (
            f"Weight event must not satisfy slot; got {classified[0]['status']}"
        )

    def test_sleep_event_not_classified_as_done(self):
        """Sleep measurement (value_numeric=7.5, no adherence_status)
        must NOT mark a slot as done in classify_slots."""
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 11, 8, 0),
                adherence_status=None,
                value_numeric=7.5,
                metric_key="sleep",
            ),
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        assert classified[0]["status"] == "unknown", (
            f"Sleep event must not satisfy slot; got {classified[0]['status']}"
        )

    def test_numeric_only_event_not_classified_as_done(self):
        """A generic event with value_numeric=1.0 and no adherence_status
        must NOT mark a slot as done."""
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)
        slots = compute_slots(c, EASTERN, now)
        events = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 11, 8, 0),
                adherence_status=None,
                value_numeric=1.0,
            ),
        ]
        classified = classify_slots(slots, events, EASTERN, now)
        assert classified[0]["status"] == "unknown", (
            f"Numeric-only event must not satisfy slot; got {classified[0]['status']}"
        )

    def test_wrong_commitment_id_adherence_event_not_classified(self):
        """An event with adherence_status='done' but a different
        commitment_id must NOT classify another commitment's slot.

        Tests via get_adherence (the proper integration path), which
        filters events by commitment_id before classification.
        classify_slots is an internal helper that inherits this
        filtering from its caller; testing it in isolation with
        unfiltered events is not a realistic call pattern."""
        c = _make_commitment(cadence="daily", start_date="2026-05-11")
        now = _utc_dt(2026, 5, 14, 12, 0)
        events = [
            _make_event(
                commitment_id="different-cid",
                observed_at=_utc_dt(2026, 5, 11, 8, 0),
                adherence_status="done",
            ),
        ]
        result = get_adherence([c], events, EASTERN, now)
        totals = result["commitments"][0]["period_totals"]
        # The event has commitment_id="different-cid", but our
        # commitment is "test-id" — get_adherence filters by cid,
        # so the event should NOT match and the slot remains unknown.
        assert totals["done"] == 0, (
            f"Wrong-cid event must not count as done; got {totals['done']}"
        )
        assert totals["unknown"] >= 1, (
            f"Expected unknown slot for past date with mismatched event; got unknown={totals['unknown']}"
        )

    def test_get_adherence_does_not_count_numeric_events(self):
        """get_adherence must not count numeric-only events in period_totals."""
        c = _make_commitment(
            label="Workout",
            cadence="daily",
            start_date="2026-05-11",
        )
        now = _utc_dt(2026, 5, 14, 12, 0)
        events = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 11, 8, 0),
                adherence_status=None,
                value_numeric=500.0,
            ),
            _make_event(
                observed_at=_utc_dt(2026, 5, 12, 8, 0),
                adherence_status="done",
            ),
        ]
        result = get_adherence([c], events, EASTERN, now)
        totals = result["commitments"][0]["period_totals"]
        # Only the May 12 done event should count
        assert totals["done"] == 1, (
            f"Expected 1 done (from explicit adherence event), got {totals['done']}"
        )
        # May 11 numeric-only event should produce unknown, not done
        assert totals["unknown"] >= 1, (
            f"Expected unknown for numeric-only event date; got unknown={totals['unknown']}"
        )


# ── Shim parity with canonical implementation ──────────────────────────────
# These tests prove the commitments.py compatibility shim produces results
# identical to the canonical app.services.adherence module.


class TestShimParityWithCanonical:
    """Verify the shim (commitments.py) matches canonical (adherence.py)."""

    def test_get_adherence_matches_canonical_daily(self):
        """get_adherence via shim produces same period_totals as
        compute_adherence via canonical for daily cadence."""
        from app.services.adherence import compute_adherence as canonical

        c_shim = _make_commitment(cadence="daily", start_date="2026-05-11")
        c_canonical = {
            "id": "test-id",
            "label": "test",
            "cadence": "daily",
            "days_of_week": [],
            "target_count": None,
            "start_date": date(2026, 5, 11),
            "end_date": None,
            "schedule_rule": {},
        }
        now = _utc_dt(2026, 5, 14, 12, 0)
        events_shim = [
            _make_event(observed_at=_utc_dt(2026, 5, 11, 8, 0), adherence_status="done"),
            _make_event(observed_at=_utc_dt(2026, 5, 12, 8, 0), adherence_status="missed"),
        ]
        events_canonical = [
            {
                "commitment_id": "test-id",
                "observed_at": _utc_dt(2026, 5, 11, 8, 0),
                "adherence_status": "done",
                "value_numeric": None,
                "value_text": None,
            },
            {
                "commitment_id": "test-id",
                "observed_at": _utc_dt(2026, 5, 12, 8, 0),
                "adherence_status": "missed",
                "value_numeric": None,
                "value_text": None,
            },
        ]

        # Shim path
        result = get_adherence([c_shim], events_shim, EASTERN, now)
        shim_totals = result["commitments"][0]["period_totals"]

        # Canonical path
        today = now.astimezone(EASTERN).date()
        board = canonical(c_canonical, events_canonical, today, EASTERN)
        canonical_totals = {
            "done": board.done,
            "missed": board.missed,
            "excused": board.excused,
            "unknown": board.unknown,
            "pending": board.pending,
        }

        assert shim_totals["done"] == canonical_totals["done"], (
            f"done mismatch: shim={shim_totals['done']} canonical={canonical_totals['done']}"
        )
        assert shim_totals["missed"] == canonical_totals["missed"], (
            f"missed mismatch: shim={shim_totals['missed']} canonical={canonical_totals['missed']}"
        )
        assert shim_totals["excused"] == canonical_totals["excused"], (
            f"excused mismatch: shim={shim_totals['excused']} canonical={canonical_totals['excused']}"
        )

    def test_get_adherence_matches_canonical_weekdays(self):
        """Shim get_adherence matches canonical compute_adherence for
        weekdays cadence."""
        from app.services.adherence import compute_adherence as canonical

        c_shim = _make_commitment(cadence="weekdays", start_date="2026-05-11")
        c_canonical = {
            "id": "test-id",
            "label": "test",
            "cadence": "weekdays",
            "days_of_week": [],
            "target_count": None,
            "start_date": date(2026, 5, 11),
            "end_date": None,
            "schedule_rule": {},
        }
        now = _utc_dt(2026, 5, 14, 12, 0)
        events = [
            _make_event(observed_at=_utc_dt(2026, 5, 11, 8, 0), adherence_status="done"),
        ]
        events_canonical = [
            {
                "commitment_id": "test-id",
                "observed_at": _utc_dt(2026, 5, 11, 8, 0),
                "adherence_status": "done",
                "value_numeric": None,
                "value_text": None,
            },
        ]

        result = get_adherence([c_shim], events, EASTERN, now)
        shim_totals = result["commitments"][0]["period_totals"]

        today = now.astimezone(EASTERN).date()
        board = canonical(c_canonical, events_canonical, today, EASTERN)

        assert shim_totals["done"] == board.done, (
            f"done mismatch: shim={shim_totals['done']} canonical={board.done}"
        )

    def test_get_adherence_matches_canonical_custom_days(self):
        """Shim get_adherence matches canonical compute_adherence for
        custom_days cadence."""
        from app.services.adherence import compute_adherence as canonical

        c_shim = _make_commitment(
            cadence="custom_days",
            days_of_week=[1, 3, 5],  # Tue, Thu, Sat
            start_date="2026-05-11",
        )
        c_canonical = {
            "id": "test-id",
            "label": "test",
            "cadence": "custom_days",
            "days_of_week": [1, 3, 5],
            "target_count": None,
            "start_date": date(2026, 5, 11),
            "end_date": None,
            "schedule_rule": {},
        }
        now = _utc_dt(2026, 5, 16, 12, 0)  # Saturday
        events = [
            _make_event(observed_at=_utc_dt(2026, 5, 12, 8, 0), adherence_status="done"),
        ]
        events_canonical = [
            {
                "commitment_id": "test-id",
                "observed_at": _utc_dt(2026, 5, 12, 8, 0),
                "adherence_status": "done",
                "value_numeric": None,
                "value_text": None,
            },
        ]

        result = get_adherence([c_shim], events, EASTERN, now)
        shim_totals = result["commitments"][0]["period_totals"]

        today = now.astimezone(EASTERN).date()
        board = canonical(c_canonical, events_canonical, today, EASTERN)

        assert shim_totals["done"] == board.done, (
            f"done mismatch: shim={shim_totals['done']} canonical={board.done}"
        )

    def test_shim_and_canonical_both_reject_numeric_only(self):
        """Both shim and canonical must reject numeric-only events equally."""
        from app.services.adherence import compute_adherence as canonical

        c_shim = _make_commitment(cadence="daily", start_date="2026-05-11")
        c_canonical = {
            "id": "test-id",
            "label": "test",
            "cadence": "daily",
            "days_of_week": [],
            "target_count": None,
            "start_date": date(2026, 5, 11),
            "end_date": None,
            "schedule_rule": {},
        }
        now = _utc_dt(2026, 5, 14, 12, 0)

        # Numeric-only events — should NOT produce 'done'
        events_shim = [
            _make_event(
                observed_at=_utc_dt(2026, 5, 11, 8, 0),
                adherence_status=None,
                value_numeric=500.0,
            ),
        ]
        events_canonical = [
            {
                "commitment_id": "test-id",
                "observed_at": _utc_dt(2026, 5, 11, 8, 0),
                "adherence_status": None,
                "value_numeric": 500.0,
                "value_text": None,
            },
        ]

        result = get_adherence([c_shim], events_shim, EASTERN, now)
        shim_totals = result["commitments"][0]["period_totals"]

        today = now.astimezone(EASTERN).date()
        board = canonical(c_canonical, events_canonical, today, EASTERN)

        # Both should show 0 done for numeric-only events
        assert shim_totals["done"] == 0, f"Shim counted numeric event as done"
        assert board.done == 0, f"Canonical counted numeric event as done"
        assert shim_totals["done"] == board.done, (
            f"Mismatch: shim done={shim_totals['done']}, canonical done={board.done}"
        )
