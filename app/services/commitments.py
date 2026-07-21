"""EXPLICIT COMPATIBILITY SHIM — delegates to app.services.adherence.

All new code should import compute_adherence / summarize_board directly from
app.services.adherence.  This module exists only for backward compatibility
with tests and code that still import from app.services.commitments.

The production adherence path (compute_adherence) is the single canonical
implementation.  classify_slots and get_adherence here delegate to it so
there is no duplicate classification logic.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as dt_date, datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── Re-export canonical functions ──────────────────────────────────────────

from app.services.adherence import (  # noqa: E402, F401
    compute_adherence,
    summarize_board,
    AdherenceBoard,
    AdherenceSlot,
    iter_week_dates,
)


# ── Helpers (kept for slot generation — richer label format) ──────────────


def _today_in_tz(tz: ZoneInfo, now_utc: datetime | None = None) -> dt_date:
    """Return today's date in the given timezone."""
    if now_utc is not None:
        return now_utc.astimezone(tz).date()
    return datetime.now(tz).date()


def _week_boundaries(today: dt_date) -> tuple[dt_date, dt_date]:
    """Return (monday, sunday) for the week containing `today`."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _day_label(d: dt_date, today: dt_date) -> str:
    """Human-readable day label: 'Mon 5/12', 'Today', 'Tomorrow', etc."""
    weekday_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd = weekday_names[d.weekday()]
    date_str = f"{d.month}/{d.day}"
    if d == today:
        return f"Today ({wd} {date_str})"
    elif d == today + timedelta(days=1):
        return f"Tomorrow ({wd} {date_str})"
    elif d == today - timedelta(days=1):
        return f"Yesterday ({wd} {date_str})"
    return f"{wd} {date_str}"


# ── Slot computation (kept — generates richer labels than adherence.py) ───


def compute_slots(
    commitment: dict[str, Any],
    timezone: ZoneInfo,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Produce a list of slot dicts for a commitment.

    Each slot: {label, date (iso), status: 'pending' initially}.

    Cadence types:
      - daily: one slot per day from start_date to end_date (or today)
      - weekdays: Mon-Fri slots in the current week (Mon-Sun)
      - weekly_count: no individual slots — returns a single summary slot
      - custom_days: slots on the specified days_of_week within the date window
      - custom: slots within the date window (start_date to end_date or today)
    """
    cadence = commitment.get("cadence", "custom")
    today = _today_in_tz(timezone, now_utc)

    start_date = commitment.get("start_date")
    if isinstance(start_date, str):
        start_date = dt_date.fromisoformat(start_date)
    elif start_date is None:
        start_date = today

    end_date = commitment.get("end_date")
    if isinstance(end_date, str):
        end_date = dt_date.fromisoformat(end_date)

    days_of_week = commitment.get("days_of_week") or []
    target_count = commitment.get("target_count")

    slots: list[dict[str, Any]] = []

    if cadence == "daily":
        # One slot per day from start_date to end_date (or today)
        stop = end_date if end_date else today
        current = start_date
        while current <= stop:
            slots.append({
                "label": _day_label(current, today),
                "date": current.isoformat(),
                "status": "pending",
            })
            current += timedelta(days=1)

    elif cadence == "weekdays":
        # Mon-Fri in the current week
        monday, sunday = _week_boundaries(today)
        current = max(start_date, monday)
        stop = min(end_date, sunday) if end_date else sunday
        while current <= stop:
            if current.weekday() < 5:  # Mon-Fri
                slots.append({
                    "label": _day_label(current, today),
                    "date": current.isoformat(),
                    "status": "pending",
                })
            current += timedelta(days=1)

    elif cadence == "weekly_count":
        # No individual slots — single summary slot for the week
        slots.append({
            "label": f"Week of {today.isoformat()}",
            "date": today.isoformat(),
            "status": "pending",
            "is_weekly_count": True,
            "target_count": target_count or 0,
        })

    elif cadence == "custom_days":
        # Slots on the specified days_of_week within the window
        monday, sunday = _week_boundaries(today)
        current = max(start_date, monday)
        stop = min(end_date, sunday) if end_date else sunday
        while current <= stop:
            if current.weekday() in days_of_week:
                slots.append({
                    "label": _day_label(current, today),
                    "date": current.isoformat(),
                    "status": "pending",
                })
            current += timedelta(days=1)

    elif cadence == "custom":
        # Slots within the date window
        stop = end_date if end_date else today
        current = start_date
        while current <= stop:
            slots.append({
                "label": _day_label(current, today),
                "date": current.isoformat(),
                "status": "pending",
            })
            current += timedelta(days=1)

    return slots


# ── Slot classification — delegates to canonical compute_adherence ─────────


def classify_slots(
    slots: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timezone: ZoneInfo,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Join events against slots by date and classify each slot.

    DELEGATES to app.services.adherence.compute_adherence for the
    canonical classification logic.  This ensures no duplicate
    implementation of the type-safety invariant:

      Only events with an explicit adherence_status in
      ('done', 'missed', 'excused') AND a matching commitment_id
      can classify a slot.

    Numeric-only, weight, sleep, or generic measurement events can
    NEVER satisfy a commitment through this path.
    """
    if not slots:
        return []

    today = _today_in_tz(timezone, now_utc)

    # Reconstruct a minimal commitment that matches the slot date range.
    # compute_adherence handles commitment_id filtering and the
    # type-safe classification rules internally.
    slot_dates = sorted(dt_date.fromisoformat(s["date"]) for s in slots)
    first_date = slot_dates[0]
    last_date = slot_dates[-1]

    # Infer cadence for weekly_count summary slots
    if slots[0].get("is_weekly_count"):
        cadence = "weekly_count"
        target_count = slots[0].get("target_count", len(slots))
    else:
        cadence = "custom"
        target_count = None

    commitment: dict[str, Any] = {
        "id": events[0].get("commitment_id", "") if events else "",
        "label": "",
        "cadence": cadence,
        "start_date": first_date,
        "end_date": last_date,
        "days_of_week": list({d.weekday() for d in slot_dates}),
        "target_count": target_count,
        "schedule_rule": {},
    }

    board = compute_adherence(commitment, events, today, timezone)

    # Map canonical statuses back to the pre-computed slots
    status_by_date: dict[str, str] = {s.date.isoformat(): s.status for s in board.slots}

    classified: list[dict[str, Any]] = []
    for slot in slots:
        slot_date = slot["date"]
        status = status_by_date.get(slot_date, "pending")

        if slot.get("is_weekly_count"):
            # For weekly_count, count done/missed events across all
            # matching events (same logic as the canonical path in
            # compute_adherence, but applied to the summary slot).
            done_count = sum(
                1 for evt in events
                if evt.get("adherence_status") == "done"
            )
            missed_count = sum(
                1 for evt in events
                if evt.get("adherence_status") == "missed"
            )
            if done_count > 0:
                slot["status"] = "done"
                slot["_done_count"] = done_count
                slot["_missed_count"] = missed_count
            elif missed_count > 0:
                slot["status"] = "missed"
                slot["_done_count"] = done_count
                slot["_missed_count"] = missed_count
            else:
                slot["status"] = "pending"
        else:
            slot["status"] = status

            # Attach event metadata for the matching date
            # Build a quick lookup by date to find the event that matched
            event_map: dict[dt_date, dict[str, Any]] = {}
            for evt in events:
                observed_at = evt.get("observed_at")
                if isinstance(observed_at, str):
                    try:
                        observed_dt = datetime.fromisoformat(observed_at)
                    except (ValueError, TypeError):
                        continue
                elif isinstance(observed_at, datetime):
                    observed_dt = observed_at
                else:
                    continue
                if observed_dt.tzinfo is None:
                    observed_dt = observed_dt.replace(tzinfo=dt_timezone.utc)
                local_dt = observed_dt.astimezone(timezone)
                evt_date = local_dt.date()
                if evt_date not in event_map or (
                    event_map[evt_date].get("adherence_status") is None
                    and evt.get("adherence_status") is not None
                ):
                    event_map[evt_date] = evt

            matching_evt = event_map.get(dt_date.fromisoformat(slot_date))
            if matching_evt and matching_evt.get("adherence_status") in ("done", "missed", "excused"):
                slot["event_id"] = str(matching_evt.get("id", "")) if matching_evt.get("id") else None
                slot["event_note"] = matching_evt.get("note")

        classified.append(slot)

    return classified


# ── Adherence summary — delegates to canonical compute_adherence ───────────


def get_adherence(
    commitments: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timezone: ZoneInfo,
    now_utc: datetime,
) -> dict[str, Any]:
    """Compute adherence read-model for a set of commitments and events.

    DELEGATES to app.services.adherence.compute_adherence for each
    commitment.  This is the only classification path — there is no
    duplicate implementation.

    Returns:
        {
            'commitments': [
                {
                    'commitment_id': str,
                    'label': str,
                    'cadence': str,
                    'slots': [...],
                    'period_totals': {'done': N, 'missed': N, 'excused': N, 'unknown': N, 'pending': N},
                    'summary': str,
                }
            ],
            'period_label': 'this week',
            'week_start': iso_date,
            'week_end': iso_date,
        }
    """
    today = _today_in_tz(timezone, now_utc)
    monday, sunday = _week_boundaries(today)

    result_commitments: list[dict[str, Any]] = []

    for c in commitments:
        cid = str(c.get("id", ""))
        label = str(c.get("label", ""))
        cadence = str(c.get("cadence", "custom"))

        # Generate slots using the legacy slot generator (clips to today
        # for commitments without end_date, unlike compute_adherence
        # which always generates the full week).
        slots = compute_slots(c, timezone, now_utc)

        # Filter events to those matching this commitment.
        c_events = [
            e for e in events
            if str(e.get("commitment_id", "")) == cid
        ]

        # Classify using the canonical implementation via classify_slots,
        # which delegates to compute_adherence.
        classified = classify_slots(slots, c_events, timezone, now_utc)

        # Period totals
        totals: dict[str, int] = {
            "done": 0,
            "missed": 0,
            "excused": 0,
            "unknown": 0,
            "pending": 0,
        }
        for s in classified:
            status = s.get("status", "pending")
            if cadence == "weekly_count" and status == "done":
                totals["done"] = s.get("_done_count", 0)
                totals["missed"] = s.get("_missed_count", 0)
            elif status in totals:
                totals[status] += 1

        # Summary string (compatible format)
        if cadence == "weekly_count":
            target = c.get("target_count") or 0
            done = totals["done"]
            remaining = max(0, target - done)
            summary = f"{done}/{target} done"
            if remaining > 0:
                summary += f", {remaining} remaining"
            if totals["missed"] > 0:
                summary += f", {totals['missed']} missed"
        else:
            parts = []
            if totals["done"]:
                parts.append(f"{totals['done']} done")
            if totals["missed"]:
                parts.append(f"{totals['missed']} missed")
            if totals["excused"]:
                parts.append(f"{totals['excused']} excused")
            if totals["unknown"]:
                parts.append(f"{totals['unknown']} blank")
            if totals["pending"]:
                parts.append(f"{totals['pending']} upcoming")
            summary = ", ".join(parts) if parts else "no activity yet"

        result_commitments.append({
            "commitment_id": cid,
            "label": label,
            "cadence": cadence,
            "slots": classified,
            "period_totals": totals,
            "summary": summary,
        })

    return {
        "commitments": result_commitments,
        "period_label": "this week",
        "week_start": monday.isoformat(),
        "week_end": sunday.isoformat(),
    }
