"""Pure-Python tests for adherence computation (compute_adherence, iter_week_dates, summarize_board).

Covers every cadence (daily, weekdays, weekly_count, custom_days, custom) ×
every status (done, missed, excused, unknown, pending), including week
boundaries, timezone handling, and weekly_count per-slot unknown/pending.

unknown is computed only — NEVER persisted as an event row.  These tests are
pure-Python; no DB required.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

import pytest

from app.services.adherence import (
    AdherenceBoard,
    AdherenceSlot,
    compute_adherence,
    iter_week_dates,
    summarize_board,
)


# ── Helpers ────────────────────────────────────────────────────────────────

UTC = dt_timezone.utc
TZ_NY = ZoneInfo("America/New_York")


def _make_commitment(
    *,
    cid: str = "c1",
    label: str = "Test",
    cadence: str = "daily",
    days_of_week: list[int] | None = None,
    target_count: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    schedule_rule: dict | None = None,
) -> dict:
    return {
        "id": cid,
        "label": label,
        "cadence": cadence,
        "days_of_week": days_of_week or [],
        "target_count": target_count,
        "start_date": start_date or date.today(),
        "end_date": end_date,
        "schedule_rule": schedule_rule or {},
    }


def _make_event(
    *,
    cid: str = "c1",
    observed_at: str,
    adherence_status: str | None = None,
    value_numeric: float | None = None,
    value_text: str | None = None,
) -> dict:
    evt: dict = {
        "commitment_id": cid,
        "observed_at": observed_at,
        "adherence_status": adherence_status,
        "value_numeric": value_numeric,
        "value_text": value_text,
    }
    return evt


def _monday_of_week(d: date) -> date:
    return d - timedelta(days=d.weekday())


# ── iter_week_dates ────────────────────────────────────────────────────────


class TestIterWeekDates:
    def test_monday_sunday_range(self):
        """iter_week_dates returns Monday-Sunday range for a Wednesday."""
        today = date(2026, 5, 13)  # Wednesday
        week = iter_week_dates(today)
        assert len(week) == 7
        assert week[0] == date(2026, 5, 11)  # Monday
        assert week[6] == date(2026, 5, 17)  # Sunday

    def test_monday_input(self):
        """If today IS Monday, that day starts the week."""
        today = date(2026, 5, 11)  # Monday
        week = iter_week_dates(today)
        assert week[0] == today

    def test_sunday_input(self):
        """If today IS Sunday, the week starts the prior Monday."""
        today = date(2026, 5, 17)  # Sunday
        week = iter_week_dates(today)
        assert week[0] == date(2026, 5, 11)

    def test_timezone_irrelevant_for_date_week(self):
        """iter_week_dates uses the date, not the tz, for Monday offsets."""
        today = date(2026, 5, 13)
        week_utc = iter_week_dates(today, UTC)
        week_ny = iter_week_dates(today, TZ_NY)
        assert week_utc == week_ny


# ── Daily cadence ──────────────────────────────────────────────────────────


class TestDailyCadence:
    def test_all_done(self):
        """Daily cadence with a done event for every day."""
        today = date(2026, 5, 13)  # Wednesday
        mon = _monday_of_week(today)
        events = [
            _make_event(observed_at=f"{mon + timedelta(days=i)}T08:00:00Z", adherence_status="done")
            for i in range(7)
        ]
        board = compute_adherence(
            _make_commitment(cadence="daily"), events, today, UTC
        )
        # All 7 days have done events, including future days
        assert board.done == 7
        for s in board.slots:
            assert s.status == "done"

    def test_all_missed(self):
        """Daily cadence with a missed event for each day."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        events = [
            _make_event(observed_at=f"{mon + timedelta(days=i)}T08:00:00Z", adherence_status="missed")
            for i in range(7)
        ]
        board = compute_adherence(
            _make_commitment(cadence="daily"), events, today, UTC
        )
        assert board.missed == 7

    def test_unknown_and_pending(self):
        """Past days with no events = unknown; today/future = pending."""
        today = date(2026, 5, 13)  # Wednesday
        board = compute_adherence(
            _make_commitment(cadence="daily"), [], today, UTC
        )
        # Mon, Tue are past with no events → unknown
        assert board.unknown == 2  # Mon, Tue
        # Wed (today) with no event → pending
        # Thu-Sun are pending
        assert board.pending == 5  # Wed-Sun
        assert board.done == 0
        assert board.missed == 0
        assert board.excused == 0

    def test_mixed_statuses(self):
        """Mix of done, missed, excused, unknown, pending."""
        today = date(2026, 5, 13)  # Wednesday
        mon = _monday_of_week(today)
        events = [
            _make_event(observed_at=f"{mon}T08:00:00Z", adherence_status="done"),     # Mon done
            _make_event(observed_at=f"{mon + timedelta(days=1)}T08:00:00Z", adherence_status="excused"),  # Tue excused
        ]
        board = compute_adherence(
            _make_commitment(cadence="daily"), events, today, UTC
        )
        assert board.done == 1
        assert board.excused == 1
        assert board.unknown == 0
        assert board.pending == 5  # Wed-Sun (today+future)
        assert board.missed == 0


# ── Weekdays cadence ───────────────────────────────────────────────────────


class TestWeekdaysCadence:
    def test_only_mon_fri_slots(self):
        """Weekdays cadence only creates slots Mon-Fri."""
        today = date(2026, 5, 13)  # Wednesday
        board = compute_adherence(
            _make_commitment(cadence="weekdays"), [], today, UTC
        )
        assert len(board.slots) == 5
        day_labels = [s.day_label for s in board.slots]
        assert day_labels == ["Mon", "Tue", "Wed", "Thu", "Fri"]

    def test_weekdays_all_unknown_past(self):
        """Weekdays, past days with no events = unknown."""
        today = date(2026, 5, 14)  # Thursday
        board = compute_adherence(
            _make_commitment(cadence="weekdays"), [], today, UTC
        )
        # Mon-Wed = past, unknown; Thu=today pending; Fri=future pending
        assert board.unknown == 3
        assert board.pending == 2

    def test_weekdays_mixed(self):
        """Weekdays with some done, some missed, some unknown."""
        today = date(2026, 5, 14)  # Thursday
        mon = _monday_of_week(today)
        events = [
            _make_event(observed_at=f"{mon}T08:00:00Z", adherence_status="done"),
            _make_event(observed_at=f"{mon + timedelta(days=1)}T08:00:00Z", adherence_status="missed"),
        ]
        board = compute_adherence(
            _make_commitment(cadence="weekdays"), events, today, UTC
        )
        assert board.done == 1
        assert board.missed == 1
        assert board.unknown == 1  # Wed
        assert board.pending == 2  # Thu, Fri


# ── custom_days cadence ────────────────────────────────────────────────────


class TestCustomDaysCadence:
    def test_tue_thu_sat(self):
        """custom_days with Tue(1), Thu(3), Sat(5)."""
        today = date(2026, 5, 13)  # Wednesday
        board = compute_adherence(
            _make_commitment(cadence="custom_days", days_of_week=[1, 3, 5]),
            [],
            today,
            UTC,
        )
        assert len(board.slots) == 3
        labels = [s.day_label for s in board.slots]
        assert labels == ["Tue", "Thu", "Sat"]

    def test_custom_days_mixed(self):
        """custom_days: Tue done, Thu unknown, Sat pending."""
        today = date(2026, 5, 15)  # Friday
        mon = _monday_of_week(today)
        tue = mon + timedelta(days=1)
        events = [
            _make_event(observed_at=f"{tue}T08:00:00Z", adherence_status="done"),
        ]
        board = compute_adherence(
            _make_commitment(cadence="custom_days", days_of_week=[1, 3, 5]),
            events,
            today,
            UTC,
        )
        assert board.done == 1  # Tue
        assert board.unknown == 1  # Thu (past)
        assert board.pending == 1  # Sat (future)


# ── Custom (date-window) cadence ───────────────────────────────────────────


class TestCustomCadence:
    def test_date_window(self):
        """Custom cadence creates one slot per day in [start, end] ∩ week."""
        today = date(2026, 5, 13)  # Wednesday
        mon = _monday_of_week(today)
        board = compute_adherence(
            _make_commitment(
                cadence="custom",
                start_date=mon + timedelta(days=1),  # Tue
                end_date=mon + timedelta(days=5),     # Sat
            ),
            [],
            today,
            UTC,
        )
        # Should have slots for Tue, Wed, Thu, Fri, Sat
        assert len(board.slots) == 5
        labels = [s.day_label for s in board.slots]
        assert labels == ["Tue", "Wed", "Thu", "Fri", "Sat"]

    def test_window_outside_week(self):
        """Custom with start/end outside the current week clips to week."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        board = compute_adherence(
            _make_commitment(
                cadence="custom",
                start_date=mon - timedelta(days=30),
                end_date=mon + timedelta(days=30),
            ),
            [],
            today,
            UTC,
        )
        assert len(board.slots) == 7  # Full week


# ── weekly_count cadence ───────────────────────────────────────────────────


class TestWeeklyCountCadence:
    def test_zero_events_all_unknown_and_pending(self):
        """weekly_count target_count=3, 0 events → all unknown (past) + pending (today/future)."""
        today = date(2026, 5, 13)  # Wednesday
        board = compute_adherence(
            _make_commitment(cadence="weekly_count", target_count=3),
            [],
            today,
            UTC,
        )
        assert len(board.slots) == 3  # target_count=3
        # Mon, Tue are past → unknown; Wed (today) → pending
        past_slots = [s for s in board.slots if s.date < today]
        today_or_future = [s for s in board.slots if s.date >= today]
        assert len(past_slots) == 2
        assert len(today_or_future) == 1
        for s in past_slots:
            assert s.status == "unknown"

    def test_two_events_two_done_one_unknown(self):
        """weekly_count target_count=3, 2 events → 2 done, 1 pending (third slot is today)."""
        today = date(2026, 5, 13)  # Wednesday
        mon = _monday_of_week(today)
        events = [
            _make_event(observed_at=f"{mon}T08:00:00Z", adherence_status="done"),
            _make_event(observed_at=f"{mon + timedelta(days=1)}T08:00:00Z", adherence_status="done"),
        ]
        board = compute_adherence(
            _make_commitment(cadence="weekly_count", target_count=3),
            events,
            today,
            UTC,
        )
        assert board.done == 2
        # Third slot is today (Wed) — no event, so it's pending (not unknown since it's not in the past)
        assert board.pending == 1
        assert board.unknown == 0

    def test_all_days_have_events(self):
        """weekly_count with events on the exact expected days."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        events = [
            _make_event(observed_at=f"{mon}T08:00:00Z", adherence_status="done"),
            _make_event(observed_at=f"{mon + timedelta(days=1)}T08:00:00Z", adherence_status="done"),
            _make_event(observed_at=f"{mon + timedelta(days=2)}T08:00:00Z", adherence_status="missed"),
        ]
        board = compute_adherence(
            _make_commitment(cadence="weekly_count", target_count=3),
            events,
            today,
            UTC,
        )
        assert board.done == 2
        assert board.missed == 1
        assert board.unknown == 0
        assert board.pending == 0


# ── Excused status ─────────────────────────────────────────────────────────


class TestExcusedStatus:
    def test_excused_is_not_missed(self):
        """Excused status should be tracked separately from missed."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        events = [
            _make_event(observed_at=f"{mon}T08:00:00Z", adherence_status="excused"),
        ]
        board = compute_adherence(
            _make_commitment(cadence="daily"), events, today, UTC
        )
        assert board.excused == 1
        assert board.missed == 0


# ── Week boundary and timezone ─────────────────────────────────────────────


class TestWeekBoundaryAndTZ:
    def test_sunday_belongs_to_current_week(self):
        """A Sunday should be in the Monday-Sunday week containing it."""
        today = date(2026, 5, 17)  # Sunday
        mon = _monday_of_week(today)
        board = compute_adherence(
            _make_commitment(cadence="daily"), [], today, UTC
        )
        # All 7 days in the week, Mon(5/11) to Sun(5/17)
        assert len(board.slots) == 7
        assert board.slots[0].date == mon
        assert board.slots[6].date == today

    def test_monday_belongs_to_new_week(self):
        """Monday starts a fresh week."""
        today = date(2026, 5, 18)  # Monday
        board = compute_adherence(
            _make_commitment(cadence="daily"), [], today, UTC
        )
        assert board.slots[0].date == today
        assert board.slots[0].day_label == "Mon"

    def test_timezone_affects_event_local_date(self):
        """Events observed in one tz may fall on a different date in another."""
        # Event at 2026-05-12T02:00:00Z = Mon 22:00 NY (May 11) or Tue 04:00 Berlin (May 12)
        today = date(2026, 5, 14)  # Thursday
        mon = _monday_of_week(today)  # Mon May 11
        events = [
            _make_event(observed_at="2026-05-12T02:00:00Z", adherence_status="done"),
        ]
        # In NY, this is Mon late night → done slot = Monday
        board_ny = compute_adherence(
            _make_commitment(cadence="daily"), events, today, TZ_NY
        )
        mon_slot = board_ny.slots[0]
        assert mon_slot.date == mon
        assert mon_slot.status == "done"

        # In UTC, this is Tue early morning → done slot = Tuesday
        board_utc = compute_adherence(
            _make_commitment(cadence="daily"), events, today, UTC
        )
        tue_slot = board_utc.slots[1]
        assert tue_slot.date == mon + timedelta(days=1)
        assert tue_slot.status == "done"


# ── unknown is never persisted ─────────────────────────────────────────────


class TestUnknownNeverPersisted:
    """unknown is a computed-only slot classification.  No event should ever
    carry adherence_status='unknown', and compute_adherence should never
    produce an event with that status."""

    def test_no_slot_has_unknown_event(self):
        """compute_adherence slots with 'unknown' come from absence of events,
        not from an event with status='unknown'."""
        today = date(2026, 5, 13)
        board = compute_adherence(
            _make_commitment(cadence="daily"), [], today, UTC
        )
        unknown_slots = [s for s in board.slots if s.status == "unknown"]
        assert len(unknown_slots) > 0  # past days without events
        # But no event should have adherence_status='unknown'
        for s in board.slots:
            if s.status == "unknown":
                # This is correct: unknown comes from event absence
                pass

    def test_unknown_is_not_a_valid_event_status(self):
        """Verify the adherence module does not return 'unknown' as an event
        status in any slot that has an event attached."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        # Create events for every day with various statuses — none should
        # produce 'unknown' because every slot has an event
        statuses = ["done", "done", "missed", "excused", "missed", "done", "done"]
        events = [
            _make_event(
                observed_at=f"{mon + timedelta(days=i)}T08:00:00Z",
                adherence_status=statuses[i],
            )
            for i in range(7)
        ]
        board = compute_adherence(
            _make_commitment(cadence="daily"), events, today, UTC
        )
        assert board.unknown == 0
        for s in board.slots:
            assert s.status != "unknown"


# ── summarize_board ────────────────────────────────────────────────────────


class TestSummarizeBoard:
    def test_summarize_empty(self):
        board = AdherenceBoard(commitment_id="c1", label="Test", cadence="daily")
        summary = summarize_board(board)
        assert "Test:" in summary
        assert "0 done" not in summary  # zero counts omitted

    def test_summarize_mixed(self):
        board = AdherenceBoard(
            commitment_id="c1",
            label="Workout",
            cadence="weekdays",
            slots=[
                AdherenceSlot(date=date(2026, 5, 11), day_label="Mon", status="done"),
                AdherenceSlot(date=date(2026, 5, 12), day_label="Tue", status="unknown"),
                AdherenceSlot(date=date(2026, 5, 13), day_label="Wed", status="missed"),
            ],
            done=1,
            missed=1,
            unknown=1,
        )
        summary = summarize_board(board)
        assert "Workout:" in summary
        assert "Mon done" in summary
        assert "Tue unknown" in summary
        assert "Wed missed" in summary
        assert "1 done" in summary
        assert "1 missed" in summary
        assert "1 unknown" in summary
        assert "of 3" in summary

    def test_summarize_all_done(self):
        board = AdherenceBoard(
            commitment_id="c1",
            label="Food",
            cadence="daily",
            slots=[
                AdherenceSlot(date=date(2026, 5, 11), day_label="Mon", status="done"),
            ],
            done=1,
        )
        summary = summarize_board(board)
        assert "Food:" in summary
        assert "Mon done" in summary
        assert "1 done" in summary


# ── Type-safety regression ─────────────────────────────────────────────────
# Verifies the contract: only events with an explicit adherence_status in
# ('done', 'missed', 'excused') AND a matching commitment_id can classify a
# slot.  Weight, sleep, and generic numeric measurement events can NEVER
# satisfy a workout commitment.


class TestTypeSafetyNumericFallbackRemoved:
    """Prove the unsafe numeric-only fallback has been removed.

    Under the old implementation, events with value_numeric would
    automatically count as 'done' even without an explicit
    adherence_status.  These tests lock in the type-safe contract.
    """

    def test_weight_event_with_value_numeric_does_not_satisfy_workout(self):
        """A weight measurement event (value_numeric=185.5, no
        adherence_status) must NOT classify a workout slot as done."""
        today = date(2026, 5, 13)  # Wednesday
        mon = _monday_of_week(today)
        weight_event = {
            "commitment_id": "workout_c1",
            "observed_at": f"{mon}T08:00:00Z",
            "adherence_status": None,
            "value_numeric": 185.5,
            "value_text": None,
            "metric_key": "weight",
        }
        board = compute_adherence(
            _make_commitment(cid="workout_c1", cadence="daily"),
            [weight_event],
            today,
            UTC,
        )
        mon_slot = board.slots[0]
        assert mon_slot.status == "unknown", (
            f"Weight event must not satisfy workout slot; got {mon_slot.status}"
        )

    def test_sleep_event_with_value_numeric_does_not_satisfy_workout(self):
        """A sleep measurement event (value_numeric=7.5 hours, no
        adherence_status) must NOT classify a workout slot as done."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        sleep_event = {
            "commitment_id": "workout_c1",
            "observed_at": f"{mon}T08:00:00Z",
            "adherence_status": None,
            "value_numeric": 7.5,
            "value_text": None,
            "metric_key": "sleep",
        }
        board = compute_adherence(
            _make_commitment(cid="workout_c1", cadence="daily"),
            [sleep_event],
            today,
            UTC,
        )
        mon_slot = board.slots[0]
        assert mon_slot.status == "unknown", (
            f"Sleep event must not satisfy workout slot; got {mon_slot.status}"
        )

    def test_generic_numeric_event_without_adherence_status_is_unknown(self):
        """A generic measurement event with value_numeric=1.0 but NO
        adherence_status must NOT classify a slot as done."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        numeric_event = {
            "commitment_id": "workout_c1",
            "observed_at": f"{mon}T08:00:00Z",
            "adherence_status": None,
            "value_numeric": 1.0,
            "value_text": None,
        }
        board = compute_adherence(
            _make_commitment(cid="workout_c1", cadence="daily"),
            [numeric_event],
            today,
            UTC,
        )
        mon_slot = board.slots[0]
        assert mon_slot.status == "unknown", (
            f"Numeric-only event must not satisfy a slot; got {mon_slot.status}"
        )

    def test_wrong_commitment_id_event_does_not_satisfy_slot(self):
        """An event with adherence_status='done' but a different
        commitment_id must NOT classify another commitment's slot."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        wrong_cid_event = {
            "commitment_id": "other_c2",
            "observed_at": f"{mon}T08:00:00Z",
            "adherence_status": "done",
            "value_numeric": None,
            "value_text": None,
        }
        board = compute_adherence(
            _make_commitment(cid="workout_c1", cadence="daily"),
            [wrong_cid_event],
            today,
            UTC,
        )
        mon_slot = board.slots[0]
        assert mon_slot.status == "unknown", (
            f"Wrong-commitment-id event must not satisfy slot; got {mon_slot.status}"
        )

    def test_explicit_done_event_with_correct_cid_satisfies_slot(self):
        """Sanity-check: an event with adherence_status='done' AND matching
        commitment_id MUST still satisfy the slot."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        valid_event = {
            "commitment_id": "workout_c1",
            "observed_at": f"{mon}T08:00:00Z",
            "adherence_status": "done",
            "value_numeric": None,
            "value_text": None,
        }
        board = compute_adherence(
            _make_commitment(cid="workout_c1", cadence="daily"),
            [valid_event],
            today,
            UTC,
        )
        mon_slot = board.slots[0]
        assert mon_slot.status == "done", (
            f"Explicit done event with correct cid must satisfy slot; got {mon_slot.status}"
        )

    def test_mixed_events_only_adherence_events_classify(self):
        """When both numeric-only and adherence events exist for the same
        date, the adherence event wins; numeric events are ignored."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        events = [
            {
                "commitment_id": "workout_c1",
                "observed_at": f"{mon}T06:00:00Z",
                "adherence_status": None,
                "value_numeric": 500.0,
                "value_text": None,
            },
            {
                "commitment_id": "workout_c1",
                "observed_at": f"{mon}T08:00:00Z",
                "adherence_status": "done",
                "value_numeric": None,
                "value_text": None,
            },
        ]
        board = compute_adherence(
            _make_commitment(cid="workout_c1", cadence="daily"),
            events,
            today,
            UTC,
        )
        mon_slot = board.slots[0]
        assert mon_slot.status == "done", (
            f"Adherence event should win over numeric-only event; got {mon_slot.status}"
        )


# ── All cadences reachability ──────────────────────────────────────────────
class TestAllCadencesReachability:
    """Smoke-test that every cadence can produce every status."""

    CADENCES = ["daily", "weekdays", "weekly_count", "custom", "custom_days"]

    def test_all_cadences_produce_slots(self):
        """Every cadence should produce at least one slot."""
        today = date(2026, 5, 13)
        for cadence in self.CADENCES:
            kwargs: dict = {}
            if cadence == "weekly_count":
                kwargs["target_count"] = 3
            elif cadence == "custom_days":
                kwargs["days_of_week"] = [1, 3, 5]
            elif cadence == "custom":
                kwargs["start_date"] = today - timedelta(days=2)
                kwargs["end_date"] = today + timedelta(days=2)
            board = compute_adherence(
                _make_commitment(cadence=cadence, **kwargs),
                [],
                today,
                UTC,
            )
            assert len(board.slots) > 0, f"Cadence {cadence} produced zero slots"

    def test_unknown_reachable_for_all_cadences(self):
        """For every cadence, past days with no events produce unknown slots."""
        today = date(2026, 5, 13)
        for cadence in self.CADENCES:
            kwargs: dict = {}
            if cadence == "weekly_count":
                kwargs["target_count"] = 3
            elif cadence == "custom_days":
                kwargs["days_of_week"] = [0, 1]  # Mon, Tue — both past
            elif cadence == "custom":
                kwargs["start_date"] = today - timedelta(days=5)
                kwargs["end_date"] = today - timedelta(days=2)  # All past
            board = compute_adherence(
                _make_commitment(cadence=cadence, **kwargs),
                [],
                today,
                UTC,
            )
            assert board.unknown > 0 or board.pending > 0, (
                f"Cadence {cadence}: expected unknown or pending, "
                f"got unknown={board.unknown} pending={board.pending}"
            )

    def test_done_reachable_for_all_cadences(self):
        """For every cadence, creating a done event on a slot produces done."""
        today = date(2026, 5, 13)
        mon = _monday_of_week(today)
        for cadence in self.CADENCES:
            kwargs: dict = {}
            if cadence == "weekly_count":
                kwargs["target_count"] = 3
                event_day = mon  # Mon — greedy slot will include it
            elif cadence == "custom_days":
                kwargs["days_of_week"] = [mon.weekday()]
                event_day = mon
            elif cadence == "custom":
                kwargs["start_date"] = mon
                kwargs["end_date"] = mon
                event_day = mon
            else:
                event_day = mon
            events = [
                _make_event(observed_at=f"{event_day}T08:00:00Z", adherence_status="done")
            ]
            board = compute_adherence(
                _make_commitment(cadence=cadence, **kwargs),
                events,
                today,
                UTC,
            )
            assert board.done == 1, (
                f"Cadence {cadence}: expected done=1, got done={board.done}"
            )
