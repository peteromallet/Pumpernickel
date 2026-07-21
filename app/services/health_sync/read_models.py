"""Query service for health read models.

Exposes a single source of truth over the normalized health tables
(``health_normalized_measurements`` and ``health_normalized_sleep``).
Every query is scoped by *user_id* so no caller can accidentally
cross privacy boundaries.  Tombstones and revisions are naturally
safe because reads only touch the normalized tables — source records
and their ``is_deleted`` flag are never consulted directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID


# ---------------------------------------------------------------------------
# Query result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ConnectionFreshness:
    """How recently a health connection completed a sync."""

    connection_id: UUID
    last_success_at: datetime | None
    is_fresh: bool


@dataclass(frozen=True, slots=True)
class WeightReading:
    """A single weight measurement from the normalized table."""

    metric: str
    measured_at: datetime
    value_numeric: float
    canonical_unit: str
    source_device_id: str | None = None
    source_device_model: str | None = None


@dataclass(frozen=True, slots=True)
class WeightResult:
    """Latest weight plus 7-day and 30-day trend summaries.

    Every field is ``None`` / empty when the user has no weight data
    at all.  Trend windows are computed at query time from
    ``reference_time`` (defaults to now UTC).
    """

    latest: WeightReading | None = None
    readings_7d: list[WeightReading] = field(default_factory=list)
    readings_30d: list[WeightReading] = field(default_factory=list)
    avg_7d: float | None = None
    avg_30d: float | None = None
    min_7d: float | None = None
    max_7d: float | None = None


@dataclass(frozen=True, slots=True)
class SleepSession:
    """A single nightly sleep session from the normalized table."""

    started_at: datetime
    ended_at: datetime
    local_sleep_date: date
    local_timezone: str | None = None
    local_offset_seconds: int | None = None
    completeness_state: str = "partial"
    total_in_bed_seconds: int | None = None
    total_asleep_seconds: int | None = None
    awake_seconds: int | None = None
    light_sleep_seconds: int | None = None
    deep_sleep_seconds: int | None = None
    rem_sleep_seconds: int | None = None
    sleep_latency_seconds: int | None = None
    wake_after_sleep_onset_seconds: int | None = None
    wakeups: int | None = None
    sleep_score: int | None = None
    source_device_id: str | None = None
    source_device_model: str | None = None


@dataclass(frozen=True, slots=True)
class NightlySleepResult:
    """All sleep sessions for a single ``local_sleep_date``."""

    local_sleep_date: date
    sessions: list[SleepSession] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SleepDaySummary:
    """Per-date aggregation within a rolling window."""

    local_sleep_date: date
    session_count: int
    total_asleep_seconds: int | None = None
    total_in_bed_seconds: int | None = None
    avg_sleep_score: float | None = None
    sessions: list[SleepSession] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SleepRollingResult:
    """7-day rolling sleep summaries."""

    summaries: list[SleepDaySummary] = field(default_factory=list)
    nights_with_data: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_SELECT_CONNECTION_FRESHNESS = """\
    SELECT last_success_at
    FROM mediator.health_connections
    WHERE id = $1
      AND user_id = $2
      AND deleted_at IS NULL
    LIMIT 1
"""

_SELECT_LATEST_WEIGHT = """\
    SELECT metric, measured_at, value_numeric, canonical_unit,
           source_device_id, source_device_model
    FROM mediator.health_normalized_measurements
    WHERE user_id = $1
      AND metric = 'weight'
    ORDER BY measured_at DESC
    LIMIT 1
"""

_SELECT_WEIGHT_TREND = """\
    SELECT metric, measured_at, value_numeric, canonical_unit,
           source_device_id, source_device_model
    FROM mediator.health_normalized_measurements
    WHERE user_id = $1
      AND metric = 'weight'
      AND measured_at >= $2
    ORDER BY measured_at DESC
"""

_SELECT_SLEEP_NIGHTLY = """\
    SELECT started_at, ended_at, local_sleep_date, local_timezone,
           local_offset_seconds, completeness_state,
           total_in_bed_seconds, total_asleep_seconds, awake_seconds,
           light_sleep_seconds, deep_sleep_seconds, rem_sleep_seconds,
           sleep_latency_seconds, wake_after_sleep_onset_seconds, wakeups,
           sleep_score, source_device_id, source_device_model
    FROM mediator.health_normalized_sleep
    WHERE user_id = $1
      AND local_sleep_date = $2
    ORDER BY started_at
"""

_SELECT_SLEEP_ROLLING = """\
    SELECT started_at, ended_at, local_sleep_date, local_timezone,
           local_offset_seconds, completeness_state,
           total_in_bed_seconds, total_asleep_seconds, awake_seconds,
           light_sleep_seconds, deep_sleep_seconds, rem_sleep_seconds,
           sleep_latency_seconds, wake_after_sleep_onset_seconds, wakeups,
           sleep_score, source_device_id, source_device_model
    FROM mediator.health_normalized_sleep
    WHERE user_id = $1
      AND local_sleep_date >= $2
      AND local_sleep_date <= $3
    ORDER BY local_sleep_date, started_at
"""

_FRESHNESS_WINDOW = timedelta(days=7)


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _row_to_weight_reading(row: Mapping[str, Any]) -> WeightReading:
    return WeightReading(
        metric=str(row["metric"]),
        measured_at=_ensure_utc(row["measured_at"]),
        value_numeric=float(row["value_numeric"]),
        canonical_unit=str(row["canonical_unit"]),
        source_device_id=row.get("source_device_id") or None,
        source_device_model=row.get("source_device_model") or None,
    )


def _row_to_sleep_session(row: Mapping[str, Any]) -> SleepSession:
    return SleepSession(
        started_at=_ensure_utc(row["started_at"]),
        ended_at=_ensure_utc(row["ended_at"]),
        local_sleep_date=_ensure_date(row["local_sleep_date"]),
        local_timezone=row.get("local_timezone") or None,
        local_offset_seconds=_nullable_int(row.get("local_offset_seconds")),
        completeness_state=str(row.get("completeness_state", "partial")),
        total_in_bed_seconds=_nullable_int(row.get("total_in_bed_seconds")),
        total_asleep_seconds=_nullable_int(row.get("total_asleep_seconds")),
        awake_seconds=_nullable_int(row.get("awake_seconds")),
        light_sleep_seconds=_nullable_int(row.get("light_sleep_seconds")),
        deep_sleep_seconds=_nullable_int(row.get("deep_sleep_seconds")),
        rem_sleep_seconds=_nullable_int(row.get("rem_sleep_seconds")),
        sleep_latency_seconds=_nullable_int(row.get("sleep_latency_seconds")),
        wake_after_sleep_onset_seconds=_nullable_int(row.get("wake_after_sleep_onset_seconds")),
        wakeups=_nullable_int(row.get("wakeups")),
        sleep_score=_nullable_int(row.get("sleep_score")),
        source_device_id=row.get("source_device_id") or None,
        source_device_model=row.get("source_device_model") or None,
    )


def _ensure_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return value


def _ensure_date(value: Any) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return date.fromisoformat(str(value))


def _nullable_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _avg_optional(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


# ---------------------------------------------------------------------------
# Public query API
# ---------------------------------------------------------------------------


async def get_connection_freshness(
    *,
    connection_id: UUID,
    user_id: UUID,
    pool: Any,
    now: datetime | None = None,
) -> ConnectionFreshness:
    """Check how recently *connection_id* completed a successful sync.

    Scoped to *user_id* so callers cannot cross privacy boundaries.

    Returns a ``ConnectionFreshness`` with ``is_fresh=True`` when
    ``last_success_at`` is within the last 7 days.
    """
    ref = now or _utc_now()
    row = await _executor(pool).fetchrow(
        _SELECT_CONNECTION_FRESHNESS,
        connection_id,
        user_id,
    )
    last_success_at: datetime | None = None
    if row is not None:
        raw = row.get("last_success_at")
        if raw is not None:
            last_success_at = _ensure_utc(raw)

    is_fresh = (
        last_success_at is not None
        and (ref - last_success_at) <= _FRESHNESS_WINDOW
    )
    return ConnectionFreshness(
        connection_id=connection_id,
        last_success_at=last_success_at,
        is_fresh=is_fresh,
    )


async def get_weight(
    *,
    user_id: UUID,
    pool: Any,
    reference_time: datetime | None = None,
) -> WeightResult:
    """Return latest weight, 7-day trend, and 30-day trend.

    All reads are scoped to *user_id*.  Returns an empty ``WeightResult``
    (all fields ``None`` / empty lists) when the user has no weight data.
    """
    ref = reference_time or _utc_now()

    # Latest weight
    latest_row = await _executor(pool).fetchrow(
        _SELECT_LATEST_WEIGHT,
        user_id,
    )
    latest: WeightReading | None = None
    if latest_row is not None:
        latest = _row_to_weight_reading(latest_row)

    # 7-day trend
    cutoff_7d = ref - timedelta(days=7)
    rows_7d = await _executor(pool).fetch(
        _SELECT_WEIGHT_TREND,
        user_id,
        cutoff_7d,
    )
    readings_7d = [_row_to_weight_reading(r) for r in (rows_7d or [])]

    # 30-day trend
    cutoff_30d = ref - timedelta(days=30)
    rows_30d = await _executor(pool).fetch(
        _SELECT_WEIGHT_TREND,
        user_id,
        cutoff_30d,
    )
    readings_30d = [_row_to_weight_reading(r) for r in (rows_30d or [])]

    # 7-day aggregates
    values_7d = [r.value_numeric for r in readings_7d]
    avg_7d = _avg_optional(values_7d)
    min_7d = min(values_7d) if values_7d else None
    max_7d = max(values_7d) if values_7d else None

    # 30-day average
    values_30d = [r.value_numeric for r in readings_30d]
    avg_30d = _avg_optional(values_30d)

    return WeightResult(
        latest=latest,
        readings_7d=readings_7d,
        readings_30d=readings_30d,
        avg_7d=avg_7d,
        avg_30d=avg_30d,
        min_7d=min_7d,
        max_7d=max_7d,
    )


async def get_nightly_sleep(
    *,
    user_id: UUID,
    local_sleep_date: date,
    pool: Any,
) -> NightlySleepResult:
    """Return every sleep session whose ``local_sleep_date`` matches.

    Scoped to *user_id*.  Returns an empty ``NightlySleepResult`` (empty
    sessions list) when no sessions exist for the date.
    """
    rows = await _executor(pool).fetch(
        _SELECT_SLEEP_NIGHTLY,
        user_id,
        local_sleep_date,
    )
    sessions = [_row_to_sleep_session(r) for r in (rows or [])]
    return NightlySleepResult(
        local_sleep_date=local_sleep_date,
        sessions=sessions,
    )


async def get_sleep_rolling_7d(
    *,
    user_id: UUID,
    pool: Any,
    reference_date: date | None = None,
) -> SleepRollingResult:
    """Return 7-day rolling sleep summaries ending on *reference_date*.

    Each summary aggregates sessions per ``local_sleep_date`` and
    includes session count, total asleep, total in bed, and average
    sleep score.

    Scoped to *user_id*.  Returns an empty ``SleepRollingResult`` when
    no sleep data exists in the window.
    """
    ref_date = reference_date or _utc_now().date()
    window_start = ref_date - timedelta(days=6)  # inclusive 7-day window

    rows = await _executor(pool).fetch(
        _SELECT_SLEEP_ROLLING,
        user_id,
        window_start,
        ref_date,
    )
    sessions = [_row_to_sleep_session(r) for r in (rows or [])]

    # Group by local_sleep_date
    by_date: dict[date, list[SleepSession]] = {}
    for s in sessions:
        by_date.setdefault(s.local_sleep_date, []).append(s)

    summaries: list[SleepDaySummary] = []
    for d in sorted(by_date):
        day_sessions = by_date[d]
        asleep_values = [s.total_asleep_seconds for s in day_sessions if s.total_asleep_seconds is not None]
        in_bed_values = [s.total_in_bed_seconds for s in day_sessions if s.total_in_bed_seconds is not None]
        score_values = [s.sleep_score for s in day_sessions if s.sleep_score is not None]

        summaries.append(
            SleepDaySummary(
                local_sleep_date=d,
                session_count=len(day_sessions),
                total_asleep_seconds=sum(asleep_values) if asleep_values else None,
                total_in_bed_seconds=sum(in_bed_values) if in_bed_values else None,
                avg_sleep_score=_avg_optional([float(v) for v in score_values]) if score_values else None,
                sessions=day_sessions,
            )
        )

    return SleepRollingResult(
        summaries=summaries,
        nights_with_data=len(summaries),
    )


def _executor(pool: Any) -> Any:
    """Return *pool* itself as the executor.

    The pool acts as its own asyncpg-style connection for simple reads.
    Callers that need a transaction-scoped executor can pass one in.
    """
    return pool


__all__ = [
    "ConnectionFreshness",
    "NightlySleepResult",
    "SleepDaySummary",
    "SleepRollingResult",
    "SleepSession",
    "WeightReading",
    "WeightResult",
    "get_connection_freshness",
    "get_nightly_sleep",
    "get_sleep_rolling_7d",
    "get_weight",
]
