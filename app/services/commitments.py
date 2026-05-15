"""Pure (no-DB) adherence computation for the Hector fitness bot.

Computes slot grids from commitments, joins events against them by date,
and classifies every slot as done / missed / excused / unknown / pending.

Key design decisions (per plan):
- 'unknown' is purely derived; it is NEVER written as an event.
- 'weekly_count' produces count-based summaries, not exact weekday slots.
- Weekly boundaries are Mon-Sun in the user's local timezone.
- All date/time math uses stdlib (datetime, zoneinfo) — no DB imports.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date as dt_date, datetime, timedelta, timezone as dt_timezone
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# ── Cadence helpers ────────────────────────────────────────────────────────


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


# ── Slot computation ───────────────────────────────────────────────────────


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


# ── Slot classification ────────────────────────────────────────────────────


def classify_slots(
    slots: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timezone: ZoneInfo,
    now_utc: datetime,
) -> list[dict[str, Any]]:
    """Join events against slots by date and classify each slot.

    Classification rules:
      - Events with adherence_status='done'/'missed'/'excused' set the slot status.
      - Slots in the past with no matching event → 'unknown'
      - Slots today or in the future with no event → 'pending'

    For weekly_count slots, count done/missed/excused events against target_count.
    """
    today = _today_in_tz(timezone, now_utc)
    events_by_date: dict[dt_date, list[dict[str, Any]]] = defaultdict(list)
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

        # Convert to user-local date
        if observed_dt.tzinfo is None:
            observed_dt = observed_dt.replace(tzinfo=dt_timezone.utc)
        local_dt = observed_dt.astimezone(timezone)
        evt_date = local_dt.date()
        events_by_date[evt_date].append(evt)

    classified: list[dict[str, Any]] = []
    for slot in slots:
        slot_date = dt_date.fromisoformat(slot["date"])
        matching = events_by_date.get(slot_date, [])

        # Check if any event has adherence_status
        adherence_event = None
        for evt in matching:
            if evt.get("adherence_status") in ("done", "missed", "excused"):
                adherence_event = evt
                break

        if adherence_event:
            slot["status"] = adherence_event["adherence_status"]
            slot["event_id"] = str(adherence_event.get("id", "")) if adherence_event.get("id") else None
            slot["event_note"] = adherence_event.get("note")
        elif slot.get("is_weekly_count"):
            # For weekly_count, count adherence events occurring this week
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
        elif slot_date < today:
            slot["status"] = "unknown"
        else:
            slot["status"] = "pending"  # today or future

        classified.append(slot)

    return classified


# ── Adherence summary ──────────────────────────────────────────────────────


def get_adherence(
    commitments: list[dict[str, Any]],
    events: list[dict[str, Any]],
    timezone: ZoneInfo,
    now_utc: datetime,
) -> dict[str, Any]:
    """Compute adherence read-model for a set of commitments and events.

    Returns:
        {
            'commitments': [
                {
                    'commitment_id': str,
                    'label': str,
                    'cadence': str,
                    'slots': [...],
                    'period_totals': {'done': N, 'missed': N, 'excused': N, 'unknown': N, 'pending': N},
                    'summary': str,  # count-based for weekly_count
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

        slots = compute_slots(c, timezone, now_utc)

        # Filter events to those matching this commitment
        c_events = [
            e for e in events
            if str(e.get("commitment_id", "")) == cid
        ]

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

        # Summary string
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
