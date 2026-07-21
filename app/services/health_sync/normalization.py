"""Pure normalization helpers for health measurement and sleep data.

These functions are intentionally free of database I/O and async so they
can be tested and composed without a connection pool.  Repository and sync
code import them to decode Withings payloads into NormalizedMeasurement
and NormalizedSleep rows.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from typing import Any, Mapping, Sequence

import zoneinfo

from app.services.health_sync.models import (
    HealthSourceRecord,
    NormalizedMeasurement,
    NormalizedSleep,
    WITHINGS_METRIC_MAP,
)


# ---------------------------------------------------------------------------
# Value decoding
# ---------------------------------------------------------------------------


def decode_withings_value(value: int, unit: int) -> float:
    """Decode a Withings integer measure value using the exponent-unit.

    Withings sends an integer *value* and a signed integer *unit* where the
    real value is ``value × 10^unit``.  For example:

        - value=70540, unit=-3   → 70.54  (weight in kg)
        - value=212,   unit=-1   → 21.2   (fat ratio in percent)
        - value=0,     unit=0    → 0.0

    Returns a ``float``.  No rounding is applied — callers that need decimal
    precision should round at the persistence or presentation layer.
    """
    return float(value) * (10.0 ** int(unit))


# ---------------------------------------------------------------------------
# Metric map and fan-out
# ---------------------------------------------------------------------------


def metric_info(measure_type: int) -> tuple[str, str] | None:
    """Return the canonical (metric_name, unit) pair for a Withings type.

    Returns ``None`` when *measure_type* is absent from ``WITHINGS_METRIC_MAP``,
    which signals "do not produce a normalized row."  This is the core absence
    semantic: types not in the map (e.g. 8 when the provider omitted fat mass)
    never invent a spurious row.
    """
    return WITHINGS_METRIC_MAP.get(measure_type)


def normalize_measure_group(
    raw_measures: Sequence[Mapping[str, Any]],
    *,
    measured_at: datetime,
    source_timezone: str | None = None,
    source_device_id: str | None = None,
    source_device_model: str | None = None,
    attribution: Mapping[str, Any] | None = None,
) -> list[NormalizedMeasurement]:
    """Fan out a single Withings measure-group into zero or more
    ``NormalizedMeasurement`` rows.

    Each measure entry is decoded via ``decode_withings_value`` and mapped
    through ``WITHINGS_METRIC_MAP``.  Types absent from the map are silently
    skipped — no row is produced for them.

    *attribution* is shallow-merged into every produced row so downstream
    consumers can trace the original source record.
    """
    base_attribution = dict(attribution or {})
    results: list[NormalizedMeasurement] = []

    for entry in raw_measures:
        measure_type = int(entry["type"])
        info = metric_info(measure_type)
        if info is None:
            continue
        metric_name, canonical_unit = info
        value = decode_withings_value(int(entry["value"]), int(entry.get("unit", 0)))
        results.append(
            NormalizedMeasurement(
                metric=metric_name,
                measured_at=measured_at,
                value_numeric=value,
                canonical_unit=canonical_unit,
                source_timezone=source_timezone,
                source_device_id=source_device_id,
                source_device_model=source_device_model,
                attribution=dict(base_attribution),
            )
        )

    return results


# ---------------------------------------------------------------------------
# Timezone helpers
# ---------------------------------------------------------------------------


def resolve_timezone(name: str | None) -> tzinfo | None:
    """Resolve an IANA timezone name to a ``zoneinfo.ZoneInfo`` instance.

    Returns ``None`` for empty, whitespace-only, or invalid names instead of
    raising so callers can propagate null to optional schema columns.
    """
    if name is None:
        return None
    stripped = name.strip()
    if not stripped:
        return None
    try:
        return zoneinfo.ZoneInfo(stripped)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError):
        return None


def calculate_offset_seconds(
    timezone_name: str | None,
    *,
    at_datetime: datetime | None = None,
) -> int | None:
    """Return the UTC offset in seconds for *timezone_name* at *at_datetime*.

    * *timezone_name*: an IANA zone string (e.g. ``"America/New_York"``).
    * *at_datetime*: a timezone-aware datetime used to resolve DST.
      Defaults to ``now(UTC)`` when omitted.

    Returns ``None`` when *timezone_name* is falsy, whitespace-only, or
    cannot be resolved by ``zoneinfo``.  This is the null-fallback contract
    for invalid/unavailable provider timezones.
    """
    zone = resolve_timezone(timezone_name)
    if zone is None:
        return None

    ref = at_datetime
    if ref is None:
        ref = datetime.now(tz=timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)

    offset = ref.astimezone(zone).utcoffset()
    if offset is None:
        return None
    return int(offset.total_seconds())


# ---------------------------------------------------------------------------
# Sleep normalization
# ---------------------------------------------------------------------------


def normalize_sleep_summary(
    record: HealthSourceRecord,
    *,
    revision_count: int = 1,
) -> NormalizedSleep | None:
    """Decode a Withings sleep summary source record into a NormalizedSleep row.

    Only summary records (whose ``external_id`` starts with ``"sleep_summary:"``)
    are processed.  Detail / stage-timeline records are intentionally skipped —
    ``health_normalized_sleep`` does not store per-stage data.

    The ``local_sleep_date`` is derived from the *wake* time (``ended_at``)
    converted to the source timezone.  This means a sleep that ends at
    06:00 UTC on Tuesday in ``America/New_York`` (UTC-4 → 02:00 local)
    is assigned to Monday, while one that ends at 06:00 UTC in
    ``Europe/Paris`` (UTC+2 → 08:00 local) is assigned to Tuesday.

    *revision_count* comes from the persisted source record and drives
    ``completeness_state``: completed summaries with ``revision_count > 1``
    are marked ``"revised"``, completed first versions are ``"complete"``,
    and incomplete summaries are ``"partial"``.

    Returns ``None`` when *record* is not a sleep summary (detail records
    and non-sleep records are silently ignored).
    """
    if not record.external_id.startswith("sleep_summary:"):
        return None

    started_at = record.starts_at
    ended_at = record.ends_at
    if started_at is None or ended_at is None:
        return None

    timezone_name = record.source_timezone
    offset_seconds: int | None = None

    # Derive local_sleep_date from wake time (ended_at) in source timezone.
    local_sleep_date: date | None = None
    tz = resolve_timezone(timezone_name)
    if tz is not None and ended_at is not None:
        local_wake = ended_at.astimezone(tz)
        local_sleep_date = local_wake.date()
        offset = local_wake.utcoffset()
        if offset is not None:
            offset_seconds = int(offset.total_seconds())

    if local_sleep_date is None:
        local_sleep_date = ended_at.date()

    # If offset wasn't computed through the local_wake path, try the helper.
    if offset_seconds is None:
        offset_seconds = calculate_offset_seconds(timezone_name, at_datetime=ended_at)

    data = record.source_metadata.get("data") if isinstance(record.source_metadata.get("data"), Mapping) else None

    def _int_field(key: str) -> int | None:
        if data is None:
            return None
        raw = data.get(key)
        if raw is None:
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    completed = bool(record.source_metadata.get("completed", False))
    if not completed:
        completeness_state = "partial"
    elif revision_count > 1:
        completeness_state = "revised"
    else:
        completeness_state = "complete"

    return NormalizedSleep(
        started_at=started_at,
        ended_at=ended_at,
        local_sleep_date=local_sleep_date,
        local_timezone=timezone_name,
        local_offset_seconds=offset_seconds,
        completeness_state=completeness_state,
        total_in_bed_seconds=_int_field("total_timeinbed"),
        total_asleep_seconds=_int_field("total_sleep_time"),
        awake_seconds=_int_field("awake_seconds"),
        light_sleep_seconds=_int_field("lightsleepduration"),
        deep_sleep_seconds=_int_field("deepsleepduration"),
        rem_sleep_seconds=_int_field("remsleepduration"),
        sleep_latency_seconds=_int_field("sleep_latency"),
        wake_after_sleep_onset_seconds=_int_field("waso"),
        wakeups=_int_field("wakeupcount"),
        sleep_score=_int_field("sleep_score"),
        source_device_id=record.source_device_id or None,
        source_device_model=record.source_device_model or None,
        attribution=dict(record.attribution),
    )


__all__ = [
    "calculate_offset_seconds",
    "decode_withings_value",
    "metric_info",
    "normalize_measure_group",
    "normalize_sleep_summary",
    "resolve_timezone",
]
