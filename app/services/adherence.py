"""Pure-Python adherence computation for commitments.

Provides compute_adherence(commitment, events, today, tz) -> AdherenceBoard
that classifies every expected slot in the current Monday-start week as one of:
done, missed, excused, unknown, or pending.

unknown is computed only — it is NEVER persisted as an event row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone as dt_timezone
from typing import Any, Literal
from zoneinfo import ZoneInfo

SlotStatus = Literal["done", "missed", "excused", "unknown", "pending"]
Cadence = Literal["daily", "weekdays", "weekly_count", "custom", "custom_days"]


@dataclass
class AdherenceSlot:
    date: date
    day_label: str  # e.g. "Mon", "Tue"
    status: SlotStatus


@dataclass
class AdherenceBoard:
    commitment_id: str
    label: str
    cadence: str
    slots: list[AdherenceSlot] = field(default_factory=list)
    done: int = 0
    missed: int = 0
    excused: int = 0
    unknown: int = 0
    pending: int = 0


DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _resolve_tz(tz_name: str | None) -> ZoneInfo:
    """Return a ZoneInfo for the given timezone name, falling back to UTC."""
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:
            pass
    return dt_timezone.utc  # type: ignore[return-value]


def iter_week_dates(today: date, tz: ZoneInfo | None = None) -> list[date]:
    """Return the list of date objects for the Monday-Sunday week containing today.

    Week boundary is Monday-start in the provided timezone (or UTC).
    """
    tz = tz or dt_timezone.utc  # type: ignore[assignment]
    weekday = today.weekday()  # 0=Monday … 6=Sunday
    monday = today - timedelta(days=weekday)
    return [monday + timedelta(days=i) for i in range(7)]


def _event_map(events: list[dict[str, Any]], tz: ZoneInfo) -> dict[date, dict[str, Any]]:
    """Index events by their observed_at date in the commitment's timezone.

    Only the most recent event per date is kept (sorted by observed_at desc).
    When an event has adherence_status, it wins over a measurement-only event.
    """
    by_date: dict[date, dict[str, Any]] = {}
    for evt in events:
        obs = evt.get("observed_at")
        if isinstance(obs, str):
            obs = datetime.fromisoformat(obs.replace("Z", "+00:00"))
        elif isinstance(obs, datetime):
            pass
        else:
            obs = datetime.now(dt_timezone.utc)
        if obs.tzinfo is None:
            obs = obs.replace(tzinfo=dt_timezone.utc)
        local = obs.astimezone(tz)
        key = local.date()

        existing = by_date.get(key)
        if existing is None:
            by_date[key] = evt
        elif existing.get("adherence_status") is None and evt.get("adherence_status") is not None:
            # Adherence event replaces measurement-only event on same date
            by_date[key] = evt
    return by_date


def compute_adherence(
    commitment: dict[str, Any],
    events: list[dict[str, Any]],
    today: date,
    tz: ZoneInfo | None = None,
) -> AdherenceBoard:
    """Compute per-slot adherence status for a single commitment.

    Args:
        commitment: Row dict with keys: id, label, cadence, days_of_week,
                   target_count, start_date, end_date, schedule_rule.
        events: List of event row dicts with keys: commitment_id, observed_at,
                adherence_status, value_numeric, value_text.
        today: The "current date" for computing the week window.
        tz: Timezone for the commitment. Read from schedule_rule.timezone,
            falling back to UTC.

    Returns:
        AdherenceBoard with per-slot classification and summary counts.

    unknown is NEVER persisted as an event — it is a computed-only slot
    classification indicating a past slot with no matching event.
    """
    tz = tz or _resolve_tz(
        (commitment.get("schedule_rule") or {}).get("timezone") if isinstance(commitment.get("schedule_rule"), dict) else None  # type: ignore[arg-type]
    )

    week_dates = iter_week_dates(today, tz)
    week_start = week_dates[0]
    week_end = week_dates[-1]

    cadence: Cadence = commitment.get("cadence", "custom")  # type: ignore[assignment]
    commitment_id = str(commitment.get("id", ""))
    label = str(commitment.get("label", ""))
    days_of_week: list[int] = commitment.get("days_of_week") or []
    target_count: int | None = commitment.get("target_count")

    start_date: date | None = None
    end_date: date | None = None
    sd = commitment.get("start_date")
    ed = commitment.get("end_date")
    if isinstance(sd, str):
        start_date = date.fromisoformat(sd)
    elif isinstance(sd, date):
        start_date = sd
    if isinstance(ed, str):
        end_date = date.fromisoformat(ed)
    elif isinstance(ed, date):
        end_date = ed

    # Build expected slot dates
    expected_dates: list[date] = []

    if cadence == "daily":
        expected_dates = list(week_dates)
    elif cadence == "weekdays":
        expected_dates = [d for d in week_dates if d.weekday() < 5]
    elif cadence == "custom_days":
        expected_dates = [d for d in week_dates if d.weekday() in days_of_week]
    elif cadence == "custom":
        effective_start = max(start_date or week_start, week_start)
        effective_end = min(end_date or week_end, week_end)
        d = effective_start
        while d <= effective_end:
            expected_dates.append(d)
            d += timedelta(days=1)
    elif cadence == "weekly_count":
        # Greedy slot assignment:
        # 1. Map events to their observed_at date (one per day).
        # 2. Fill remaining slots in date order up to target_count.
        tc = target_count or 1
        event_map = _event_map(events, tz)
        used_dates: set[date] = set()

        # First pass: events that match this commitment's observed_at dates
        for d in week_dates:
            if d in event_map:
                used_dates.add(d)

        # If we need more slots, fill from Monday forward
        for d in week_dates:
            if len(used_dates) >= tc:
                break
            used_dates.add(d)

        expected_dates = sorted(used_dates)[:tc]

    # Match events to slots
    event_map = _event_map(events, tz)
    board = AdherenceBoard(
        commitment_id=commitment_id,
        label=label,
        cadence=cadence,
    )

    for d in expected_dates:
        day_label = DAY_NAMES[d.weekday()]
        evt = event_map.get(d)

        if d < today:
            # Past slot
            if evt is not None and evt.get("adherence_status") in ("done", "missed", "excused"):
                status: SlotStatus = evt["adherence_status"]  # type: ignore[assignment]
            elif evt is not None and evt.get("value_numeric") is not None:
                # Measurement-only event — treat as done if it exists
                status = "done"
            else:
                status = "unknown"  # Computed only — NEVER persisted
        elif d == today:
            # Today: can be done/missed/excused if event exists, else pending
            if evt is not None and evt.get("adherence_status") in ("done", "missed", "excused"):
                status = evt["adherence_status"]  # type: ignore[assignment]
            elif evt is not None and evt.get("value_numeric") is not None:
                status = "done"
            else:
                status = "pending"
        else:
            # Future slot
            if evt is not None and evt.get("adherence_status") in ("done", "missed", "excused"):
                status = evt["adherence_status"]  # type: ignore[assignment]
            elif evt is not None and evt.get("value_numeric") is not None:
                status = "done"
            else:
                status = "pending"

        slot = AdherenceSlot(date=d, day_label=day_label, status=status)
        board.slots.append(slot)
        setattr(board, status, getattr(board, status) + 1)

    return board


def summarize_board(board: AdherenceBoard) -> str:
    """Return a compact text summary of the adherence board for hot-context rendering."""
    total = board.done + board.missed + board.excused + board.unknown + board.pending
    parts = [
        f"{board.label}:",
    ]
    # Per-day status line
    day_parts = [f"{s.day_label} {s.status}" for s in board.slots]
    parts.append(" ".join(day_parts))
    # Summary counts
    counts = []
    if board.done:
        counts.append(f"{board.done} done")
    if board.missed:
        counts.append(f"{board.missed} missed")
    if board.excused:
        counts.append(f"{board.excused} excused")
    if board.unknown:
        counts.append(f"{board.unknown} unknown")
    if board.pending:
        counts.append(f"{board.pending} pending")
    if counts:
        parts.append(f"({', '.join(counts)} of {total})")
    return " ".join(parts)
