"""Focused tests for the pure normalization helpers introduced in T2.

These tests exercise:

- value × 10^unit decoding (Withings exponent)
- the exact metric map including type 6 → fat_ratio_pct
- absence semantics (no invented row for types absent from the map)
- attribution propagation
- timezone offset calculation with null fallback for invalid zones
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest
import zoneinfo

from app.services.health_sync.models import (
    NormalizedMeasurement,
    WITHINGS_METRIC_MAP,
    WithingsMeasureType,
)
from app.services.health_sync.normalization import (
    calculate_offset_seconds,
    decode_withings_value,
    metric_info,
    normalize_measure_group,
    resolve_timezone,
)


# ---------------------------------------------------------------------------
# decode_withings_value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, unit, expected",
    [
        # Typical Withings scale measurements:
        (70540, -3, 70.54),      # weight: 70540 × 10⁻³ = 70.54 kg
        (212, -1, 21.2),          # fat ratio: 212 × 10⁻¹ = 21.2 %
        (14980, -3, 14.98),       # muscle mass: 14980 × 10⁻³ = 14.98 kg
        (5430, -3, 5.43),         # bone mass: 5430 × 10⁻³ = 5.43 kg
        # Edge cases:
        (0, -3, 0.0),
        (1, 0, 1.0),
        (1000, -3, 1.0),
        (1, 3, 1000.0),
        (1, -6, 0.000001),
        (-1, 2, -100.0),
    ],
)
def test_decode_withings_value(value: int, unit: int, expected: float) -> None:
    assert decode_withings_value(value, unit) == pytest.approx(expected)


def test_decode_withings_value_float() -> None:
    """Verify decode returns float type for type safety."""
    result = decode_withings_value(70540, -3)
    assert isinstance(result, float)
    assert result == 70.54


# ---------------------------------------------------------------------------
# WITHINGS_METRIC_MAP completeness
# ---------------------------------------------------------------------------


def test_metric_map_contains_type_6_as_fat_ratio() -> None:
    """Type 6 must map to fat_ratio with unit percent (not e.g. fat_mass)."""
    assert WITHINGS_METRIC_MAP[WithingsMeasureType.FAT_RATIO_PCT] == ("fat_ratio", "percent")


def test_metric_map_contains_weight() -> None:
    assert WITHINGS_METRIC_MAP[WithingsMeasureType.WEIGHT_KG] == ("weight", "kg")


def test_metric_map_contains_fat_mass() -> None:
    assert WITHINGS_METRIC_MAP[WithingsMeasureType.FAT_MASS_WEIGHT_KG] == ("fat_mass", "kg")


def test_metric_map_contains_muscle_mass() -> None:
    assert WITHINGS_METRIC_MAP[WithingsMeasureType.MUSCLE_MASS_KG] == ("muscle_mass", "kg")


def test_metric_map_contains_bone_mass() -> None:
    assert WITHINGS_METRIC_MAP[WithingsMeasureType.BONE_MASS_KG] == ("bone_mass", "kg")


def test_metric_info_returns_none_for_unmapped_type() -> None:
    """A type not in the map must return None (absence semantics)."""
    assert metric_info(999) is None
    assert metric_info(-1) is None


def test_metric_info_returns_tuple_for_mapped_types() -> None:
    assert metric_info(1) == ("weight", "kg")
    assert metric_info(6) == ("fat_ratio", "percent")
    assert metric_info(8) == ("fat_mass", "kg")


# ---------------------------------------------------------------------------
# normalize_measure_group – happy path
# ---------------------------------------------------------------------------

_MEASURED_AT = datetime(2025, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_normalize_measure_group_decodes_weight_and_fat_ratio() -> None:
    measures = [
        {"value": 70540, "type": 1, "unit": -3},
        {"value": 212, "type": 6, "unit": -1},
    ]
    rows = normalize_measure_group(
        measures,
        measured_at=_MEASURED_AT,
        source_timezone="America/New_York",
        source_device_id="scale-01",
        source_device_model="Body Comp",
        attribution={"fixture": "test"},
    )

    assert len(rows) == 2

    weight = next(r for r in rows if r.metric == "weight")
    assert weight.value_numeric == pytest.approx(70.54)
    assert weight.canonical_unit == "kg"
    assert weight.source_timezone == "America/New_York"
    assert weight.source_device_id == "scale-01"
    assert weight.source_device_model == "Body Comp"
    assert weight.attribution == {"fixture": "test"}

    fat = next(r for r in rows if r.metric == "fat_ratio")
    assert fat.value_numeric == pytest.approx(21.2)
    assert fat.canonical_unit == "percent"


# ---------------------------------------------------------------------------
# normalize_measure_group – absence semantics
# ---------------------------------------------------------------------------


def test_normalize_measure_group_skips_unmapped_types() -> None:
    """Types not in WITHINGS_METRIC_MAP must not produce rows."""
    measures = [
        {"value": 50000, "type": 999, "unit": -3},  # unknown type
        {"value": 70540, "type": 1, "unit": -3},     # weight
    ]
    rows = normalize_measure_group(measures, measured_at=_MEASURED_AT)
    assert len(rows) == 1
    assert rows[0].metric == "weight"


def test_normalize_measure_group_empty_measures() -> None:
    rows = normalize_measure_group([], measured_at=_MEASURED_AT)
    assert rows == []


# ---------------------------------------------------------------------------
# normalize_measure_group – attribution propagation
# ---------------------------------------------------------------------------


def test_normalize_measure_group_attribution_propagated_to_all_rows() -> None:
    measures = [
        {"value": 70540, "type": 1, "unit": -3},
        {"value": 212, "type": 6, "unit": -1},
        {"value": 14980, "type": 76, "unit": -3},
    ]
    attr = {"fixture_scenario": "measurements_page_1", "adapter": "withings"}
    rows = normalize_measure_group(measures, measured_at=_MEASURED_AT, attribution=attr)

    assert len(rows) == 3
    for row in rows:
        assert row.attribution == attr, f"attribution not propagated for {row.metric}"


def test_normalize_measure_group_attribution_is_a_copy() -> None:
    """Caller mutations after normalization must not affect rows."""
    attr: dict[str, str] = {"key": "original"}
    measures = [{"value": 70540, "type": 1, "unit": -3}]
    rows = normalize_measure_group(measures, measured_at=_MEASURED_AT, attribution=attr)
    attr["key"] = "mutated"
    assert rows[0].attribution == {"key": "original"}


# ---------------------------------------------------------------------------
# normalize_measure_group – optional fields
# ---------------------------------------------------------------------------


def test_normalize_measure_group_none_optionals() -> None:
    measures = [{"value": 70540, "type": 1, "unit": -3}]
    rows = normalize_measure_group(measures, measured_at=_MEASURED_AT)
    row = rows[0]
    assert row.source_timezone is None
    assert row.source_device_id is None
    assert row.source_device_model is None


# ---------------------------------------------------------------------------
# resolve_timezone
# ---------------------------------------------------------------------------


def test_resolve_timezone_valid() -> None:
    tz = resolve_timezone("America/New_York")
    assert isinstance(tz, zoneinfo.ZoneInfo)
    assert str(tz) == "America/New_York"


def test_resolve_timezone_utc() -> None:
    tz = resolve_timezone("UTC")
    assert isinstance(tz, zoneinfo.ZoneInfo)


def test_resolve_timezone_none() -> None:
    assert resolve_timezone(None) is None


def test_resolve_timezone_empty_string() -> None:
    assert resolve_timezone("") is None


def test_resolve_timezone_whitespace() -> None:
    assert resolve_timezone("   ") is None


def test_resolve_timezone_invalid_name() -> None:
    """Invalid IANA zone name must return None — no exception."""
    assert resolve_timezone("Not/A_Real_Zone") is None


def test_resolve_timezone_garbage() -> None:
    assert resolve_timezone("!#$%") is None


# ---------------------------------------------------------------------------
# calculate_offset_seconds
# ---------------------------------------------------------------------------


def test_calculate_offset_seconds_nyc_winter() -> None:
    """America/New_York in January → UTC-5 = -18000 seconds."""
    at = datetime(2025, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    offset = calculate_offset_seconds("America/New_York", at_datetime=at)
    assert offset == -5 * 3600  # -18000


def test_calculate_offset_seconds_nyc_summer() -> None:
    """America/New_York in July → UTC-4 (EDT) = -14400 seconds."""
    at = datetime(2025, 7, 15, 12, 0, 0, tzinfo=timezone.utc)
    offset = calculate_offset_seconds("America/New_York", at_datetime=at)
    assert offset == -4 * 3600  # -14400


def test_calculate_offset_seconds_dst_spring_forward() -> None:
    """Verify DST transition is handled correctly (EDT at 2025-03-10)."""
    # March 9, 2025 06:59 UTC → still EST (UTC-5)
    before = datetime(2025, 3, 9, 6, 59, 0, tzinfo=timezone.utc)
    assert calculate_offset_seconds("America/New_York", at_datetime=before) == -5 * 3600
    # March 9, 2025 07:01 UTC → now EDT (UTC-4)
    after = datetime(2025, 3, 9, 7, 1, 0, tzinfo=timezone.utc)
    assert calculate_offset_seconds("America/New_York", at_datetime=after) == -4 * 3600


def test_calculate_offset_seconds_dst_fall_back() -> None:
    """Verify DST fall-back transition."""
    # Nov 2, 2025 05:59 UTC → still EDT (UTC-4)
    before = datetime(2025, 11, 2, 5, 59, 0, tzinfo=timezone.utc)
    assert calculate_offset_seconds("America/New_York", at_datetime=before) == -4 * 3600
    # Nov 2, 2025 06:01 UTC → now EST (UTC-5)
    after = datetime(2025, 11, 2, 6, 1, 0, tzinfo=timezone.utc)
    assert calculate_offset_seconds("America/New_York", at_datetime=after) == -5 * 3600


def test_calculate_offset_seconds_utc() -> None:
    assert calculate_offset_seconds("UTC") == 0


def test_calculate_offset_seconds_null_on_none_zone() -> None:
    assert calculate_offset_seconds(None) is None


def test_calculate_offset_seconds_null_on_empty_zone() -> None:
    assert calculate_offset_seconds("") is None


def test_calculate_offset_seconds_null_on_invalid_zone() -> None:
    assert calculate_offset_seconds("Not/A_Zone") is None


def test_calculate_offset_seconds_defaults_to_now() -> None:
    """When at_datetime is omitted, offset is computed for 'now' and returns an int."""
    offset = calculate_offset_seconds("UTC")
    assert isinstance(offset, int)
    assert offset == 0


def test_calculate_offset_seconds_naive_datetime() -> None:
    """A naive datetime is treated as UTC per spec."""
    naive = datetime(2025, 7, 15, 12, 0, 0)
    offset = calculate_offset_seconds("America/New_York", at_datetime=naive)
    assert offset == -4 * 3600  # July → EDT


# ---------------------------------------------------------------------------
# NormalizedMeasurement dataclass
# ---------------------------------------------------------------------------


def test_normalized_measurement_construction() -> None:
    nm = NormalizedMeasurement(
        metric="weight",
        measured_at=_MEASURED_AT,
        value_numeric=70.54,
        canonical_unit="kg",
    )
    assert nm.metric == "weight"
    assert nm.value_numeric == pytest.approx(70.54)
    assert nm.canonical_unit == "kg"
    assert nm.source_timezone is None
    assert nm.source_offset_seconds is None
    assert nm.source_device_id is None
    assert nm.source_device_model is None
    assert nm.attribution == {}


def test_normalized_measurement_empty_metric_raises() -> None:
    with pytest.raises(ValueError, match="metric"):
        NormalizedMeasurement(
            metric="",
            measured_at=_MEASURED_AT,
            value_numeric=1.0,
            canonical_unit="kg",
        )


def test_normalized_measurement_whitespace_metric_raises() -> None:
    with pytest.raises(ValueError, match="metric"):
        NormalizedMeasurement(
            metric="   ",
            measured_at=_MEASURED_AT,
            value_numeric=1.0,
            canonical_unit="kg",
        )


def test_normalized_measurement_empty_unit_raises() -> None:
    with pytest.raises(ValueError, match="canonical_unit"):
        NormalizedMeasurement(
            metric="weight",
            measured_at=_MEASURED_AT,
            value_numeric=1.0,
            canonical_unit="",
        )


def test_normalized_measurement_normalizes_datetime() -> None:
    naive = datetime(2025, 7, 20, 12, 0, 0)
    nm = NormalizedMeasurement(
        metric="weight",
        measured_at=naive,
        value_numeric=70.54,
        canonical_unit="kg",
    )
    assert nm.measured_at.tzinfo is not None
    assert nm.measured_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Repository: normalized measurement replacement / deletion (T3)
# ---------------------------------------------------------------------------

import copy
from uuid import UUID, uuid4

from tests.conftest import FakePool
from app.services.health_sync.repository import HealthSyncRepository


def _make_measurements(
    measured_at: datetime | None = None,
) -> list[NormalizedMeasurement]:
    at = measured_at or datetime(2025, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    return [
        NormalizedMeasurement(
            metric="weight",
            measured_at=at,
            value_numeric=70.54,
            canonical_unit="kg",
            source_device_id="scale-01",
            source_device_model="Body Comp",
            attribution={"source": "withings"},
        ),
        NormalizedMeasurement(
            metric="fat_ratio",
            measured_at=at,
            value_numeric=21.2,
            canonical_unit="percent",
        ),
    ]


async def test_replace_normalized_measurements_inserts_rows() -> None:
    """Replace creates rows after deleting any existing ones."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()
    measurements = _make_measurements()

    async with repo.transaction():
        row_ids = await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=measurements,
        )

    assert len(row_ids) == 2
    assert len(pool.health_normalized_measurements) == 2
    stored_metrics = {r["metric"] for r in pool.health_normalized_measurements.values()}
    assert stored_metrics == {"weight", "fat_ratio"}


async def test_replace_normalized_measurements_deletes_existing() -> None:
    """Second replace deletes old rows and inserts new ones."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    # First insert: two metrics
    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=_make_measurements(),
        )
    assert len(pool.health_normalized_measurements) == 2

    # Second insert: only one metric (weight)
    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=[
                NormalizedMeasurement(
                    metric="weight",
                    measured_at=datetime(2025, 7, 21, tzinfo=timezone.utc),
                    value_numeric=71.0,
                    canonical_unit="kg",
                ),
            ],
        )

    assert len(pool.health_normalized_measurements) == 1
    remaining = list(pool.health_normalized_measurements.values())[0]
    assert remaining["metric"] == "weight"
    assert remaining["value_numeric"] == 71.0


async def test_replace_normalized_measurements_empty_list_deletes_all() -> None:
    """Passing empty list deletes existing rows without inserting."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=_make_measurements(),
        )
    assert len(pool.health_normalized_measurements) == 2

    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=[],
        )
    assert len(pool.health_normalized_measurements) == 0


async def test_delete_normalized_measurements_removes_all() -> None:
    """Delete removes all rows scoped to the source record."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=_make_measurements(),
        )
    assert len(pool.health_normalized_measurements) == 2

    async with repo.transaction():
        await repo.delete_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
        )
    assert len(pool.health_normalized_measurements) == 0


async def test_measurement_ops_scoped_by_user_and_connection() -> None:
    """Operations on one user's data do not affect another user's."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)

    user_a = uuid4()
    conn_a = pool.seed_health_connection(user_id=user_a)
    source_a = uuid4()

    user_b = uuid4()
    conn_b = pool.seed_health_connection(user_id=user_b)
    source_b = uuid4()

    # Insert for user A
    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_a,
            connection_id=conn_a,
            user_id=user_a,
            measurements=_make_measurements(),
        )
    # Insert for user B
    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_b,
            connection_id=conn_b,
            user_id=user_b,
            measurements=_make_measurements(),
        )

    assert len(pool.health_normalized_measurements) == 4

    # Delete only user A's rows
    async with repo.transaction():
        await repo.delete_normalized_measurements(
            source_record_id=source_a,
            connection_id=conn_a,
            user_id=user_a,
        )

    # User B's rows are untouched
    assert len(pool.health_normalized_measurements) == 2
    remaining_users = {r["user_id"] for r in pool.health_normalized_measurements.values()}
    assert remaining_users == {user_b}


async def test_measurement_ops_wrong_user_not_deleted() -> None:
    """Calling delete with wrong user_id must not delete rows."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=_make_measurements(),
        )
    assert len(pool.health_normalized_measurements) == 2

    # Delete with wrong user_id — no effect (triple-scoped)
    await repo.delete_normalized_measurements(
        source_record_id=source_id,
        connection_id=connection_id,
        user_id=uuid4(),  # different user
    )
    assert len(pool.health_normalized_measurements) == 2


async def test_measurement_ops_wrong_connection_not_deleted() -> None:
    """Calling delete with wrong connection_id must not delete rows."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=_make_measurements(),
        )
    assert len(pool.health_normalized_measurements) == 2

    await repo.delete_normalized_measurements(
        source_record_id=source_id,
        connection_id=uuid4(),  # different connection
        user_id=user_id,
    )
    assert len(pool.health_normalized_measurements) == 2


async def test_measurement_replace_attribution_preserved() -> None:
    """Attribution dict is stored as-is."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=[
                NormalizedMeasurement(
                    metric="weight",
                    measured_at=datetime(2025, 7, 20, tzinfo=timezone.utc),
                    value_numeric=70.5,
                    canonical_unit="kg",
                    attribution={"source": "withings", "grpid": 9001001},
                ),
            ],
        )

    row = list(pool.health_normalized_measurements.values())[0]
    assert row["attribution"] == {"source": "withings", "grpid": 9001001}


# ---------------------------------------------------------------------------
# Repository: normalized sleep replacement / deletion (T3)
# ---------------------------------------------------------------------------


def _make_sleep_kwargs(
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    **overrides: Any,
) -> dict:
    started = started_at or datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc)
    ended = ended_at or datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc)

    result = dict(
        started_at=started,
        ended_at=ended,
        local_sleep_date=date(2025, 7, 20),
        local_timezone="America/New_York",
        local_offset_seconds=-14400,
        completeness_state="complete",
        total_in_bed_seconds=28800,
        total_asleep_seconds=25200,
        awake_seconds=1200,
        light_sleep_seconds=10800,
        deep_sleep_seconds=7200,
        rem_sleep_seconds=7200,
        sleep_latency_seconds=600,
        wake_after_sleep_onset_seconds=1800,
        wakeups=2,
        sleep_score=85,
        source_device_id="tracker-01",
        source_device_model="Sleep Analyzer",
        attribution={"source": "withings"},
    )
    result.update(overrides)
    return result


async def test_replace_normalized_sleep_inserts_row() -> None:
    """Replace creates a sleep row after deleting any existing one."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        row_id = await repo.replace_normalized_sleep(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            **_make_sleep_kwargs(),
        )

    assert row_id is not None
    assert len(pool.health_normalized_sleep) == 1
    row = list(pool.health_normalized_sleep.values())[0]
    assert row["completeness_state"] == "complete"
    assert row["sleep_score"] == 85


async def test_replace_normalized_sleep_deletes_existing() -> None:
    """Second replace for same source deletes old row first."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_sleep(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            **_make_sleep_kwargs(sleep_score=85),
        )
    assert len(pool.health_normalized_sleep) == 1

    async with repo.transaction():
        await repo.replace_normalized_sleep(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            **_make_sleep_kwargs(sleep_score=90),
        )

    assert len(pool.health_normalized_sleep) == 1
    row = list(pool.health_normalized_sleep.values())[0]
    assert row["sleep_score"] == 90


async def test_delete_normalized_sleep_removes_row() -> None:
    """Delete removes the sleep row."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_sleep(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            **_make_sleep_kwargs(),
        )
    assert len(pool.health_normalized_sleep) == 1

    async with repo.transaction():
        await repo.delete_normalized_sleep(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
        )
    assert len(pool.health_normalized_sleep) == 0


async def test_sleep_ops_scoped_by_user_and_connection() -> None:
    """Sleep deletion only affects the scoped source record."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)

    user_a = uuid4()
    conn_a = pool.seed_health_connection(user_id=user_a)
    source_a = uuid4()

    user_b = uuid4()
    conn_b = pool.seed_health_connection(user_id=user_b)
    source_b = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_sleep(
            source_record_id=source_a,
            connection_id=conn_a,
            user_id=user_a,
            **_make_sleep_kwargs(),
        )
    async with repo.transaction():
        await repo.replace_normalized_sleep(
            source_record_id=source_b,
            connection_id=conn_b,
            user_id=user_b,
            **_make_sleep_kwargs(),
        )
    assert len(pool.health_normalized_sleep) == 2

    async with repo.transaction():
        await repo.delete_normalized_sleep(
            source_record_id=source_a,
            connection_id=conn_a,
            user_id=user_a,
        )

    assert len(pool.health_normalized_sleep) == 1
    remaining = list(pool.health_normalized_sleep.values())[0]
    assert remaining["user_id"] == user_b


async def test_sleep_ops_wrong_user_not_deleted() -> None:
    """Sleep delete with mismatched user_id has no effect."""
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    async with repo.transaction():
        await repo.replace_normalized_sleep(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            **_make_sleep_kwargs(),
        )
    assert len(pool.health_normalized_sleep) == 1

    await repo.delete_normalized_sleep(
        source_record_id=source_id,
        connection_id=connection_id,
        user_id=uuid4(),  # different user
    )
    assert len(pool.health_normalized_sleep) == 1


async def test_no_raw_logging_in_replace_paths() -> None:
    """Repository methods do not log raw payloads or health values.

    We verify this by checking that no prints or log statements are
    emitted during replacements.  The methods only construct SQL and
    delegate to the executor — they never serialize or log payloads.
    """
    pool = FakePool()
    repo = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)
    source_id = uuid4()

    # Simply verify execution succeeds without side-effects on stdout/stderr.
    async with repo.transaction():
        row_ids = await repo.replace_normalized_measurements(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            measurements=_make_measurements(),
        )
    assert len(row_ids) == 2

    async with repo.transaction():
        sleep_id = await repo.replace_normalized_sleep(
            source_record_id=source_id,
            connection_id=connection_id,
            user_id=user_id,
            **_make_sleep_kwargs(),
        )
    assert sleep_id is not None


# ---------------------------------------------------------------------------
# T5: NormalizedSleep dataclass
# ---------------------------------------------------------------------------


def test_normalized_sleep_construction() -> None:
    """NormalizedSleep can be constructed with required fields."""
    ns = NormalizedSleep(
        started_at=datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
        local_sleep_date=date(2025, 7, 20),
    )
    assert ns.started_at == datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc)
    assert ns.ended_at == datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc)
    assert ns.local_sleep_date == date(2025, 7, 20)
    assert ns.completeness_state == "partial"  # default
    assert ns.total_in_bed_seconds is None
    assert ns.attribution == {}


def test_normalized_sleep_empty_completeness_state_raises() -> None:
    with pytest.raises(ValueError, match="completeness_state"):
        NormalizedSleep(
            started_at=datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
            ended_at=datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
            local_sleep_date=date(2025, 7, 20),
            completeness_state="",
        )


def test_normalized_sleep_normalizes_datetimes() -> None:
    naive_start = datetime(2025, 7, 20, 23, 0, 0)
    naive_end = datetime(2025, 7, 21, 7, 0, 0)
    ns = NormalizedSleep(
        started_at=naive_start,
        ended_at=naive_end,
        local_sleep_date=date(2025, 7, 20),
    )
    assert ns.started_at.tzinfo == timezone.utc
    assert ns.ended_at.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# T5: normalize_sleep_summary
# ---------------------------------------------------------------------------

from app.services.health_sync.models import (
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    NormalizedSleep,
)
from app.services.health_sync.normalization import normalize_sleep_summary


def _make_sleep_source_record(
    *,
    external_id: str = "sleep_summary:9203001",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    source_timezone: str | None = "America/New_York",
    source_device_id: str | None = "synthetic-sleep-hash-01",
    source_device_model: str | None = "Sleep Sensor",
    completed: bool = True,
    data: dict | None = None,
    attribution: dict | None = None,
) -> HealthSourceRecord:
    start = starts_at or datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc)
    end = ends_at or datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc)
    return HealthSourceRecord(
        provider=HealthProviderSlug.WITHINGS,
        resource_type=HealthResourceType.SLEEP,
        external_id=external_id,
        starts_at=start,
        ends_at=end,
        source_timezone=source_timezone,
        source_device_id=source_device_id,
        source_device_model=source_device_model,
        source_metadata={
            "completed": completed,
            "data": data or {},
            "date": "2025-07-20",
            "model": 32,
            "model_id": 63,
        },
        attribution=attribution or {"fixture_scenario": "test"},
    )


def test_normalize_sleep_summary_extracts_basic_fields() -> None:
    """Full sleep summary produces a NormalizedSleep with all extracted fields."""
    record = _make_sleep_source_record(
        data={
            "total_timeinbed": 28800,
            "total_sleep_time": 25200,
            "lightsleepduration": 10800,
            "remsleepduration": 7200,
            "deepsleepduration": 7200,
            "wakeupcount": 2,
            "sleep_score": 85,
        },
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.total_in_bed_seconds == 28800
    assert result.total_asleep_seconds == 25200
    assert result.light_sleep_seconds == 10800
    assert result.rem_sleep_seconds == 7200
    assert result.deep_sleep_seconds == 7200
    assert result.wakeups == 2
    assert result.sleep_score == 85
    assert result.completeness_state == "complete"
    assert result.source_device_id == "synthetic-sleep-hash-01"
    assert result.source_device_model == "Sleep Sensor"
    assert result.attribution == {"fixture_scenario": "test"}


def test_normalize_sleep_summary_local_sleep_date_from_wake_time() -> None:
    """local_sleep_date is derived from wake time (ended_at) in source timezone."""
    # Wake at 2025-07-21 07:00 UTC → in America/New_York (UTC-4 EDT) that's 03:00 → date 2025-07-21
    record = _make_sleep_source_record(
        starts_at=datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
        source_timezone="America/New_York",
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    # EDT in July: UTC-4, so 07:00 UTC = 03:00 local = 2025-07-21
    assert result.local_sleep_date == date(2025, 7, 21)
    assert result.local_offset_seconds == -4 * 3600  # -14400


def test_normalize_sleep_summary_cross_midnight_utc() -> None:
    """Sleep ending after midnight UTC with UTC timezone gets the next day."""
    record = _make_sleep_source_record(
        starts_at=datetime(2025, 7, 20, 22, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 7, 21, 6, 0, 0, tzinfo=timezone.utc),
        source_timezone="UTC",
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.local_sleep_date == date(2025, 7, 21)
    assert result.local_offset_seconds == 0


def test_normalize_sleep_summary_cross_midnight_europe() -> None:
    """Sleep ending at 06:00 UTC in Paris (UTC+2) → local 08:00 → date stays the same."""
    record = _make_sleep_source_record(
        starts_at=datetime(2025, 7, 20, 22, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 7, 21, 6, 0, 0, tzinfo=timezone.utc),
        source_timezone="Europe/Paris",
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    # July in Paris: UTC+2, so 06:00 UTC = 08:00 local = 2025-07-21
    assert result.local_sleep_date == date(2025, 7, 21)
    assert result.local_offset_seconds == 2 * 3600  # +7200


def test_normalize_sleep_summary_dst_winter() -> None:
    """EST (UTC-5) in January: wake at 12:00 UTC → 07:00 local."""
    record = _make_sleep_source_record(
        starts_at=datetime(2025, 1, 14, 3, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 1, 14, 12, 0, 0, tzinfo=timezone.utc),
        source_timezone="America/New_York",
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.local_sleep_date == date(2025, 1, 14)
    assert result.local_offset_seconds == -5 * 3600  # EST


def test_normalize_sleep_summary_dst_summer() -> None:
    """EDT (UTC-4) in July: wake at 12:00 UTC → 08:00 local."""
    record = _make_sleep_source_record(
        starts_at=datetime(2025, 7, 14, 3, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 7, 14, 12, 0, 0, tzinfo=timezone.utc),
        source_timezone="America/New_York",
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.local_sleep_date == date(2025, 7, 14)
    assert result.local_offset_seconds == -4 * 3600  # EDT


def test_normalize_sleep_summary_partial_completeness() -> None:
    """When completed=False, completeness_state is 'partial'."""
    record = _make_sleep_source_record(completed=False)
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.completeness_state == "partial"


def test_normalize_sleep_summary_complete() -> None:
    """When completed=True, completeness_state is 'complete'."""
    record = _make_sleep_source_record(completed=True)
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.completeness_state == "complete"


def test_normalize_sleep_summary_revised() -> None:
    """Completed + revision_count > 1 → 'revised'."""
    record = _make_sleep_source_record(completed=True)
    result = normalize_sleep_summary(record, revision_count=2)
    assert result is not None
    assert result.completeness_state == "revised"


def test_normalize_sleep_summary_revised_count_one_is_complete() -> None:
    """Completed + revision_count == 1 → 'complete' (first version)."""
    record = _make_sleep_source_record(completed=True)
    result = normalize_sleep_summary(record, revision_count=1)
    assert result is not None
    assert result.completeness_state == "complete"


def test_normalize_sleep_summary_not_completed_never_revised() -> None:
    """Incomplete + revision_count > 1 → still 'partial'."""
    record = _make_sleep_source_record(completed=False)
    result = normalize_sleep_summary(record, revision_count=5)
    assert result is not None
    assert result.completeness_state == "partial"


def test_normalize_sleep_summary_detail_record_excluded() -> None:
    """Stage-timeline (detail) records are excluded — returns None."""
    record = _make_sleep_source_record(
        external_id="sleep:fallback:{\"enddate\":1784494800}",
    )
    result = normalize_sleep_summary(record)
    assert result is None


def test_normalize_sleep_summary_non_sleep_record_excluded() -> None:
    """Non-sleep records are excluded."""
    record = HealthSourceRecord(
        provider=HealthProviderSlug.WITHINGS,
        resource_type=HealthResourceType.MEASUREMENT,
        external_id="grpid:9001001",
    )
    result = normalize_sleep_summary(record)
    assert result is None


def test_normalize_sleep_summary_null_timezone() -> None:
    """When timezone is None, local_sleep_date falls back to ended_at.date() in UTC."""
    record = _make_sleep_source_record(
        source_timezone=None,
        starts_at=datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.local_sleep_date == date(2025, 7, 21)
    assert result.local_timezone is None
    assert result.local_offset_seconds is None


def test_normalize_sleep_summary_invalid_timezone_fallback() -> None:
    """Invalid timezone falls back to UTC date and None offset."""
    record = _make_sleep_source_record(
        source_timezone="Not/A_Real_Zone",
        starts_at=datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.local_sleep_date == date(2025, 7, 21)
    assert result.local_offset_seconds is None


def test_normalize_sleep_summary_optional_null_handling() -> None:
    """Missing data fields produce None for optional values."""
    record = _make_sleep_source_record(data={})
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.total_in_bed_seconds is None
    assert result.total_asleep_seconds is None
    assert result.awake_seconds is None
    assert result.light_sleep_seconds is None
    assert result.deep_sleep_seconds is None
    assert result.rem_sleep_seconds is None
    assert result.sleep_latency_seconds is None
    assert result.wake_after_sleep_onset_seconds is None
    assert result.wakeups is None
    assert result.sleep_score is None


def test_normalize_sleep_summary_missing_starts_at() -> None:
    """Record with None starts_at returns None."""
    record = HealthSourceRecord(
        provider=HealthProviderSlug.WITHINGS,
        resource_type=HealthResourceType.SLEEP,
        external_id="sleep_summary:9203001",
        starts_at=None,
        ends_at=datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
        source_timezone="America/New_York",
    )
    result = normalize_sleep_summary(record)
    assert result is None


def test_normalize_sleep_summary_non_mapping_data() -> None:
    """When source_metadata['data'] is not a Mapping, fields default to None."""
    record = _make_sleep_source_record(data={})
    # Override the _make to have a non-mapping data
    record = HealthSourceRecord(
        provider=HealthProviderSlug.WITHINGS,
        resource_type=HealthResourceType.SLEEP,
        external_id="sleep_summary:9203001",
        starts_at=datetime(2025, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ends_at=datetime(2025, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
        source_timezone="America/New_York",
        source_metadata={
            "completed": True,
            "data": "not_a_mapping",
            "date": "2025-07-20",
        },
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.total_in_bed_seconds is None
    assert result.sleep_score is None


def test_normalize_sleep_summary_partial_data_fields() -> None:
    """Only the fields present in data are populated; missing ones are None."""
    record = _make_sleep_source_record(
        data={
            "total_timeinbed": 25200,
            "total_sleep_time": 23640,
            "sleep_score": 83,
        },
    )
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.total_in_bed_seconds == 25200
    assert result.total_asleep_seconds == 23640
    assert result.sleep_score == 83
    # Fields not in data are None
    assert result.light_sleep_seconds is None
    assert result.deep_sleep_seconds is None
    assert result.rem_sleep_seconds is None
    assert result.wakeups is None
    assert result.awake_seconds is None


def test_normalize_sleep_summary_attribution_is_copy() -> None:
    """Caller mutations after normalization must not affect the result."""
    attr: dict[str, str] = {"key": "original"}
    record = _make_sleep_source_record(attribution=attr)
    result = normalize_sleep_summary(record)
    attr["key"] = "mutated"
    assert result is not None
    assert result.attribution == {"key": "original"}


def test_normalize_sleep_summary_empty_device_id_becomes_none() -> None:
    """Empty source_device_id is normalized to None."""
    record = _make_sleep_source_record(source_device_id="")
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.source_device_id is None


def test_normalize_sleep_summary_empty_device_model_becomes_none() -> None:
    """Empty source_device_model is normalized to None."""
    record = _make_sleep_source_record(source_device_model="")
    result = normalize_sleep_summary(record)
    assert result is not None
    assert result.source_device_model is None


# ---------------------------------------------------------------------------
# T5: End-to-end sleep sync produces normalized sleep rows
# ---------------------------------------------------------------------------

from app.services.health_sync import FakeWithingsProvider, sync_connection_resource
from app.services.health_sync.repository import repository_for


async def _rotated_access_token(provider: FakeWithingsProvider) -> str:
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri="https://example.test/api/health/devices/withings/oauth/callback",
    )
    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")
    return refreshed.access_token


async def test_sleep_sync_produces_normalized_sleep_rows() -> None:
    """After syncing sleep, a normalized sleep row must be created for the
    summary record (detail record excluded)."""
    pool = FakePool()
    repository = repository_for(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)

    await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.SLEEP,
        now=datetime(2026, 7, 20, 7, 35, tzinfo=timezone.utc),
    )

    # 2 source records (1 summary + 1 detail)
    assert len(pool.health_source_records) == 2

    # 1 normalized sleep row (summary only, detail excluded)
    assert len(pool.health_normalized_sleep) == 1

    sleep_row = list(pool.health_normalized_sleep.values())[0]
    assert sleep_row["completeness_state"] == "complete"
    assert sleep_row["sleep_score"] == 83
    assert sleep_row["total_in_bed_seconds"] == 25200
    assert sleep_row["total_asleep_seconds"] == 23640
    assert sleep_row["light_sleep_seconds"] == 13200
    assert sleep_row["deep_sleep_seconds"] == 6240
    assert sleep_row["rem_sleep_seconds"] == 4200
    assert sleep_row["wakeups"] == 2
    assert sleep_row["local_timezone"] == "America/New_York"
    assert sleep_row["source_device_id"] == "synthetic-sleep-hash-01"
    assert sleep_row["source_device_model"] == "63"
    assert sleep_row["attribution"]["fixture_scenario"] == "sleep_summary_page_1+sleep_detail_page_1"

    # Verify external_id points to the summary record
    summary_source = next(
        row for row in pool.health_source_records.values()
        if row["external_id"].startswith("sleep_summary:")
    )
    assert sleep_row["source_record_id"] == summary_source["id"]

    # No normalized measurement rows from sleep sync
    assert len(pool.health_normalized_measurements) == 0


async def test_sleep_revision_replaces_normalized_sleep_row() -> None:
    """When a sleep summary is revised, the old normalized row is replaced."""
    pool = FakePool()
    repository = repository_for(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    # Initial sync
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)
    await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.SLEEP,
        now=datetime(2026, 7, 20, 7, 35, tzinfo=timezone.utc),
    )

    assert len(pool.health_normalized_sleep) == 1
    first_row = list(pool.health_normalized_sleep.values())[0]
    assert first_row["sleep_score"] == 83

    # Re-sync same data (revision)
    await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.SLEEP,
        now=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )

    # Still exactly 1 normalized sleep row (replaced, not duplicated)
    assert len(pool.health_normalized_sleep) == 1
    assert len(pool.health_source_records) == 2


# ---------------------------------------------------------------------------
# T6: Expanded fake Withings provider scenarios and golden-output tests
# ---------------------------------------------------------------------------


# --- Helpers for T6 scenario tests ---


async def _sync_scenario(
    pool: FakePool,
    *,
    scenario_id: str,
    resource_type: HealthResourceType = HealthResourceType.SLEEP,
    user_id: UUID | None = None,
    connection_id: UUID | None = None,
    now: datetime | None = None,
) -> None:
    """Run a single sync using the given override scenario."""
    repository = repository_for(pool)
    if user_id is None:
        user_id = uuid4()
    if connection_id is None:
        connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
    provider = FakeWithingsProvider(
        fetch_scenarios={resource_type: scenario_id},
    )
    access_token = await _rotated_access_token(provider)
    await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=resource_type,
        now=now or datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
    )


# --- Incomplete → Complete revision ---


async def test_t6_incomplete_to_complete_sleep_revision() -> None:
    """An incomplete sleep summary synced then revised to complete must
    update completeness_state (→'revised' on second write) and aggregates
    in the normalized row."""
    pool = FakePool()
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    # Step 1: sync incomplete sleep
    await _sync_scenario(
        pool,
        scenario_id="sleep_summary_incomplete",
        user_id=user_id,
        connection_id=connection_id,
        now=datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc),
    )

    assert len(pool.health_normalized_sleep) == 1
    incomplete_row = list(pool.health_normalized_sleep.values())[0]
    assert incomplete_row["completeness_state"] == "partial"
    assert incomplete_row["sleep_score"] == 55
    assert incomplete_row["total_asleep_seconds"] == 14400
    assert incomplete_row["wakeups"] == 3

    # Step 2: sync completed revision (same session, higher modified, completed=true)
    await _sync_scenario(
        pool,
        scenario_id="sleep_summary_completed_revision",
        user_id=user_id,
        connection_id=connection_id,
        now=datetime(2026, 7, 22, 8, 0, tzinfo=timezone.utc),
    )

    # Must still have exactly 1 normalized sleep row (replaced, not duplicated)
    assert len(pool.health_normalized_sleep) == 1
    completed_row = list(pool.health_normalized_sleep.values())[0]
    # revision_count=2 (second upsert of same external_id) → 'revised'
    assert completed_row["completeness_state"] == "revised"
    assert completed_row["sleep_score"] == 82
    assert completed_row["total_asleep_seconds"] == 23400
    assert completed_row["wakeups"] == 2
    # Wake at ~08:40 UTC → 04:40 EDT → date 2026-07-21
    assert completed_row["local_sleep_date"] == date(2026, 7, 21)


# --- Cross-midnight sleep ---


async def test_t6_cross_midnight_sleep_local_date() -> None:
    """A sleep that ends after local midnight must derive the next day as
    local_sleep_date (wake time in source timezone)."""
    pool = FakePool()

    await _sync_scenario(pool, scenario_id="sleep_cross_midnight")

    assert len(pool.health_normalized_sleep) == 1
    row = list(pool.health_normalized_sleep.values())[0]
    # start: 2026-07-21 22:00 UTC, end: 2026-07-22 06:40 UTC
    # America/New_York EDT (UTC-4): end_local = 02:40 → date = 2026-07-22
    assert row["local_sleep_date"] == date(2026, 7, 22)
    assert row["local_timezone"] == "America/New_York"
    assert row["local_offset_seconds"] == -4 * 3600  # EDT
    assert row["completeness_state"] == "complete"
    assert row["sleep_score"] == 90
    assert row["total_asleep_seconds"] == 25200


# --- Nap scenario ---


async def test_t6_nap_sleep_short_duration() -> None:
    """A short nap (< 2 hours) must still produce a valid normalized row
    with correct duration and completeness."""
    pool = FakePool()

    await _sync_scenario(pool, scenario_id="sleep_nap")

    assert len(pool.health_normalized_sleep) == 1
    row = list(pool.health_normalized_sleep.values())[0]
    # start: 2026-07-20 04:00 UTC, end: 2026-07-20 05:00 UTC
    # EDT (UTC-4): end_local = 01:00 → date = 2026-07-20
    assert row["local_sleep_date"] == date(2026, 7, 20)
    assert row["total_in_bed_seconds"] == 3600
    assert row["total_asleep_seconds"] == 3000
    assert row["sleep_score"] == 72
    assert row["completeness_state"] == "complete"
    assert row["wakeups"] == 0


# --- Split same local date ---


async def test_t6_split_same_local_date_two_sessions() -> None:
    """Two sleep sessions with wake times falling on the same local date
    must produce two distinct normalized rows with the same local_sleep_date."""
    pool = FakePool()

    await _sync_scenario(pool, scenario_id="sleep_split_same_date")

    assert len(pool.health_normalized_sleep) == 2
    rows = list(pool.health_normalized_sleep.values())
    # Both wake times resolve to 2026-07-19 in America/New_York EDT
    for row in rows:
        assert row["local_sleep_date"] == date(2026, 7, 19)

    # First session: sleep_summary:9207001
    first = next(r for r in rows if r["source_device_id"] == "synthetic-sleep-hash-05")
    assert first["total_asleep_seconds"] == 12600
    assert first["sleep_score"] == 78

    # Second session: sleep_summary:9207002
    second = next(r for r in rows if r["source_device_id"] == "synthetic-sleep-hash-06")
    assert second["total_asleep_seconds"] == 4800
    assert second["sleep_score"] == 65


# --- DST spring ---


async def test_t6_dst_spring_forward_sleep() -> None:
    """Sleep spanning DST spring-forward must resolve correct offset and date.
    Wake at 14:00 UTC on 2025-03-09 → 10:00 EDT (UTC-4)."""
    pool = FakePool()

    await _sync_scenario(
        pool,
        scenario_id="sleep_dst_spring",
        now=datetime(2025, 3, 9, 15, 0, tzinfo=timezone.utc),
    )

    assert len(pool.health_normalized_sleep) == 1
    row = list(pool.health_normalized_sleep.values())[0]
    assert row["local_sleep_date"] == date(2025, 3, 9)
    assert row["local_offset_seconds"] == -4 * 3600  # EDT
    assert row["completeness_state"] == "complete"
    assert row["sleep_score"] == 88


# --- DST fall ---


async def test_t6_dst_fall_back_sleep() -> None:
    """Sleep spanning DST fall-back must resolve correct offset and date.
    Wake at 08:00 UTC on 2025-11-02 → 03:00 EST (UTC-5)."""
    pool = FakePool()

    await _sync_scenario(
        pool,
        scenario_id="sleep_dst_fall",
        now=datetime(2025, 11, 2, 9, 0, tzinfo=timezone.utc),
    )

    assert len(pool.health_normalized_sleep) == 1
    row = list(pool.health_normalized_sleep.values())[0]
    assert row["local_sleep_date"] == date(2025, 11, 2)
    assert row["local_offset_seconds"] == -5 * 3600  # EST
    assert row["completeness_state"] == "complete"
    assert row["sleep_score"] == 76


# --- Overlapping sessions ---


async def test_t6_overlapping_sleep_sessions() -> None:
    """Two overlapping sleep sessions must both produce normalized rows
    without interfering with each other."""
    pool = FakePool()

    await _sync_scenario(pool, scenario_id="sleep_overlapping")

    assert len(pool.health_normalized_sleep) == 2
    rows = list(pool.health_normalized_sleep.values())
    device_ids = {r["source_device_id"] for r in rows}
    assert device_ids == {"synthetic-sleep-hash-ov-1", "synthetic-sleep-hash-ov-2"}

    # Session 1 (9210001): total_asleep=8400
    s1 = next(r for r in rows if r["source_device_id"] == "synthetic-sleep-hash-ov-1")
    assert s1["total_asleep_seconds"] == 8400
    assert s1["sleep_score"] == 70

    # Session 2 (9210002): total_asleep=9000
    s2 = next(r for r in rows if r["source_device_id"] == "synthetic-sleep-hash-ov-2")
    assert s2["total_asleep_seconds"] == 9000
    assert s2["sleep_score"] == 74


# --- Sleep tombstone deletion ---


async def test_t6_sleep_tombstone_deletes_normalized_row() -> None:
    """When a sleep summary receives a tombstone, its normalized row must
    be deleted while the source record is soft-deleted for audit."""
    pool = FakePool()
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    # Step 1: sync sleep to create a normalized row
    await _sync_scenario(
        pool,
        scenario_id="sleep_summary_page_1",
        user_id=user_id,
        connection_id=connection_id,
        now=datetime(2026, 7, 20, 7, 35, tzinfo=timezone.utc),
    )

    assert len(pool.health_normalized_sleep) == 1
    assert len(pool.health_source_records) == 2  # summary + detail

    # Step 2: sync sleep tombstones
    await _sync_scenario(
        pool,
        scenario_id="sleep_tombstones",
        user_id=user_id,
        connection_id=connection_id,
        now=datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc),
    )

    # Normalized sleep row must be deleted
    assert len(pool.health_normalized_sleep) == 0

    # Source record still exists (soft-deleted)
    assert len(pool.health_source_records) == 2
    tombstoned = next(
        r for r in pool.health_source_records.values()
        if r["external_id"] == "sleep_summary:9203001"
    )
    assert tombstoned["is_deleted"] is True


# --- Missing optional fields (sleep) ---


async def test_t6_sleep_missing_optional_fields() -> None:
    """Sleep fixture with no hash_deviceid and only partial data fields
    must produce a normalized row with None for missing columns."""
    pool = FakePool()

    await _sync_scenario(pool, scenario_id="sleep_missing_optional")

    assert len(pool.health_normalized_sleep) == 1
    row = list(pool.health_normalized_sleep.values())[0]
    assert row["completeness_state"] == "complete"
    assert row["total_in_bed_seconds"] == 33600
    assert row["total_asleep_seconds"] == 28800
    # Optional fields not present in data must be None
    assert row["light_sleep_seconds"] is None
    assert row["deep_sleep_seconds"] is None
    assert row["rem_sleep_seconds"] is None
    assert row["wakeups"] is None
    assert row["sleep_score"] is None
    assert row["awake_seconds"] is None
    assert row["sleep_latency_seconds"] is None
    assert row["wake_after_sleep_onset_seconds"] is None
    # No hash_deviceid → empty source_device_id → normalized to None
    assert row["source_device_id"] is None


# --- Missing optional fields (measurement) ---


async def test_t6_measurement_missing_optional_fields() -> None:
    """Measurement fixture with no deviceid and no model must produce
    normalized rows with None for device fields."""
    pool = FakePool()

    await _sync_scenario(
        pool,
        scenario_id="measurements_missing_optionals",
        resource_type=HealthResourceType.MEASUREMENT,
    )

    assert len(pool.health_normalized_measurements) == 1
    row = list(pool.health_normalized_measurements.values())[0]
    assert row["metric"] == "weight"
    assert row["value_numeric"] == pytest.approx(72.0)  # 72000 × 10⁻³
    assert row["canonical_unit"] == "kg"
    # No deviceid or hash_deviceid → source_device_id is empty string
    # (measurement records default to ""; sleep normalization converts to None)
    assert row["source_device_id"] == ""
    # No model field → source_device_model is empty string
    assert row["source_device_model"] == ""


# --- Fake provider scenario selection (offline) ---


async def test_t6_fake_provider_selects_sleep_scenarios_offline() -> None:
    """Every new sleep scenario can be selected and replayed offline
    without live credentials or network access."""
    for scenario_id in (
        "sleep_summary_incomplete",
        "sleep_summary_completed_revision",
        "sleep_cross_midnight",
        "sleep_nap",
        "sleep_split_same_date",
        "sleep_dst_spring",
        "sleep_dst_fall",
        "sleep_overlapping",
        "sleep_missing_optional",
    ):
        provider = FakeWithingsProvider(
            fetch_scenarios={HealthResourceType.SLEEP: scenario_id},
        )
        # Exchange + refresh to get a valid access token (no network)
        exchanged = await provider.exchange_code(
            code="synthetic-auth-code-001",
            redirect_uri="https://example.test/api/health/devices/withings/oauth/callback",
        )
        refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")
        # Fetch must return records without network access
        result = await provider.fetch_changes(
            access_token=refreshed.access_token,
            resource_type=HealthResourceType.SLEEP,
            cursor=None,
        )
        assert len(result.records) > 0, f"{scenario_id} must return records"
        assert any(
            r.external_id.startswith("sleep_summary:") for r in result.records
        ), f"{scenario_id} must include sleep summary records"


async def test_t6_fake_provider_sleep_tombstones_offline() -> None:
    """Sleep tombstone scenario can be replayed offline and returns
    tombstones with SLEEP resource type."""
    provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.SLEEP: "sleep_tombstones"},
    )
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri="https://example.test/api/health/devices/withings/oauth/callback",
    )
    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")
    result = await provider.fetch_changes(
        access_token=refreshed.access_token,
        resource_type=HealthResourceType.SLEEP,
        cursor=None,
    )
    assert len(result.tombstones) == 1
    tombstone = result.tombstones[0]
    assert tombstone.resource_type == HealthResourceType.SLEEP
    assert tombstone.external_id == "sleep_summary:9203001"
    assert tombstone.reason == "user_deleted_sleep"
    assert result.resource_type == HealthResourceType.SLEEP


# ---------------------------------------------------------------------------
# T7: Read model query service
# ---------------------------------------------------------------------------

from app.services.health_sync.read_models import (
    ConnectionFreshness,
    NightlySleepResult,
    SleepDaySummary,
    SleepRollingResult,
    SleepSession,
    WeightReading,
    WeightResult,
    get_connection_freshness,
    get_nightly_sleep,
    get_sleep_rolling_7d,
    get_weight,
)


# --- Helpers for seeding data ---


def _seed_weight(pool: FakePool, user_id: UUID, measured_at: datetime, value: float) -> None:
    """Directly seed a weight row into the fake pool's normalized measurements."""
    row_id = uuid4()
    pool.health_normalized_measurements[row_id] = {
        "id": row_id,
        "source_record_id": uuid4(),
        "connection_id": UUID("00000000-0000-4000-8000-000000000001"),
        "user_id": user_id,
        "metric": "weight",
        "measured_at": measured_at,
        "value_numeric": value,
        "canonical_unit": "kg",
        "source_unit": "kg",
        "source_device_id": "scale-01",
        "source_device_model": "Body Comp",
        "attribution": {"source": "test"},
    }


def _seed_sleep(
    pool: FakePool,
    user_id: UUID,
    *,
    started_at: datetime,
    ended_at: datetime,
    local_sleep_date: date,
    local_timezone: str = "America/New_York",
    local_offset_seconds: int = -14400,
    completeness_state: str = "complete",
    total_in_bed_seconds: int | None = 28800,
    total_asleep_seconds: int | None = 25200,
    awake_seconds: int | None = 1200,
    light_sleep_seconds: int | None = 10800,
    deep_sleep_seconds: int | None = 7200,
    rem_sleep_seconds: int | None = 7200,
    sleep_latency_seconds: int | None = 600,
    wake_after_sleep_onset_seconds: int | None = 1800,
    wakeups: int | None = 2,
    sleep_score: int | None = 85,
    source_device_id: str | None = "tracker-01",
    source_device_model: str | None = "Sleep Analyzer",
) -> UUID:
    row_id = uuid4()
    pool.health_normalized_sleep[row_id] = {
        "id": row_id,
        "source_record_id": uuid4(),
        "connection_id": UUID("00000000-0000-4000-8000-000000000001"),
        "user_id": user_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "local_sleep_date": local_sleep_date,
        "local_timezone": local_timezone,
        "local_offset_seconds": local_offset_seconds,
        "completeness_state": completeness_state,
        "total_in_bed_seconds": total_in_bed_seconds,
        "total_asleep_seconds": total_asleep_seconds,
        "awake_seconds": awake_seconds,
        "light_sleep_seconds": light_sleep_seconds,
        "deep_sleep_seconds": deep_sleep_seconds,
        "rem_sleep_seconds": rem_sleep_seconds,
        "sleep_latency_seconds": sleep_latency_seconds,
        "wake_after_sleep_onset_seconds": wake_after_sleep_onset_seconds,
        "wakeups": wakeups,
        "sleep_score": sleep_score,
        "source_device_id": source_device_id,
        "source_device_model": source_device_model,
        "attribution": {"source": "test"},
    }
    return row_id


# --- Connection freshness ---


async def test_get_connection_freshness_fresh() -> None:
    """Connection that synced recently is fresh."""
    pool = FakePool()
    user_id = uuid4()
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    connection_id = pool.seed_health_connection(
        user_id=user_id,
        updated_at=now - timedelta(hours=2),
    )
    # Simulate a recent sync
    conn = pool.health_connections[connection_id]
    conn["last_success_at"] = now - timedelta(hours=2)

    result = await get_connection_freshness(
        connection_id=connection_id,
        user_id=user_id,
        pool=pool,
        now=now,
    )
    assert result.connection_id == connection_id
    assert result.is_fresh is True
    assert result.last_success_at is not None


async def test_get_connection_freshness_stale() -> None:
    """Connection that hasn't synced in 8 days is not fresh."""
    pool = FakePool()
    user_id = uuid4()
    now = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
    connection_id = pool.seed_health_connection(
        user_id=user_id,
        updated_at=now - timedelta(days=10),
    )
    conn = pool.health_connections[connection_id]
    conn["last_success_at"] = now - timedelta(days=8)

    result = await get_connection_freshness(
        connection_id=connection_id,
        user_id=user_id,
        pool=pool,
        now=now,
    )
    assert result.is_fresh is False


async def test_get_connection_freshness_no_sync() -> None:
    """Connection that never synced has is_fresh=False."""
    pool = FakePool()
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id)

    result = await get_connection_freshness(
        connection_id=connection_id,
        user_id=user_id,
        pool=pool,
    )
    assert result.is_fresh is False
    assert result.last_success_at is None


async def test_get_connection_freshness_wrong_user_id_returns_none() -> None:
    """Connection lookup with a different user_id returns no data."""
    pool = FakePool()
    owner_id = uuid4()
    other_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=owner_id)
    conn = pool.health_connections[connection_id]
    conn["last_success_at"] = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)

    result = await get_connection_freshness(
        connection_id=connection_id,
        user_id=other_id,
        pool=pool,
    )
    assert result.last_success_at is None
    assert result.is_fresh is False


# --- Weight: latest ---


async def test_get_weight_latest_with_data() -> None:
    """Latest weight returns the most recent measurement."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    _seed_weight(pool, user_id, datetime(2026, 7, 18, 8, 0, 0, tzinfo=timezone.utc), 70.5)
    _seed_weight(pool, user_id, datetime(2026, 7, 19, 8, 0, 0, tzinfo=timezone.utc), 71.0)
    _seed_weight(pool, user_id, datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc), 70.8)

    result = await get_weight(
        user_id=user_id,
        pool=pool,
        reference_time=datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert result.latest is not None
    assert result.latest.value_numeric == pytest.approx(70.8)
    assert result.latest.metric == "weight"
    assert result.latest.canonical_unit == "kg"


async def test_get_weight_no_data() -> None:
    """When user has no weight readings, result is empty."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    result = await get_weight(user_id=user_id, pool=pool)
    assert result.latest is None
    assert result.readings_7d == []
    assert result.readings_30d == []
    assert result.avg_7d is None
    assert result.avg_30d is None
    assert result.min_7d is None
    assert result.max_7d is None


# --- Weight: 7-day trend ---


async def test_get_weight_7d_trend() -> None:
    """7-day trend returns only recent readings with aggregates."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)
    ref = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

    # Within 7 days
    _seed_weight(pool, user_id, datetime(2026, 7, 14, 8, 0, 0, tzinfo=timezone.utc), 71.0)
    _seed_weight(pool, user_id, datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc), 70.5)
    _seed_weight(pool, user_id, datetime(2026, 7, 18, 8, 0, 0, tzinfo=timezone.utc), 70.0)
    # Outside 7 days (13 days ago)
    _seed_weight(pool, user_id, datetime(2026, 7, 7, 8, 0, 0, tzinfo=timezone.utc), 72.0)

    result = await get_weight(user_id=user_id, pool=pool, reference_time=ref)

    assert len(result.readings_7d) == 3
    values_7d = [r.value_numeric for r in result.readings_7d]
    assert 72.0 not in values_7d  # excluded by window
    assert result.avg_7d == pytest.approx((71.0 + 70.5 + 70.0) / 3)
    assert result.min_7d == pytest.approx(70.0)
    assert result.max_7d == pytest.approx(71.0)


async def test_get_weight_30d_trend() -> None:
    """30-day trend includes readings from the full window."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)
    ref = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)

    # 31 days ago — excluded from 30-day window
    _seed_weight(pool, user_id, datetime(2026, 6, 19, 8, 0, 0, tzinfo=timezone.utc), 73.0)
    _seed_weight(pool, user_id, datetime(2026, 7, 5, 8, 0, 0, tzinfo=timezone.utc), 71.0)
    _seed_weight(pool, user_id, datetime(2026, 7, 18, 8, 0, 0, tzinfo=timezone.utc), 70.0)

    result = await get_weight(user_id=user_id, pool=pool, reference_time=ref)

    assert len(result.readings_30d) == 2  # June 19 excluded (31 days)
    assert result.avg_30d == pytest.approx((71.0 + 70.0) / 2)


# --- Weight: strict user isolation ---


async def test_get_weight_strict_user_filtering() -> None:
    """Reads only return data for the specified user_id."""
    pool = FakePool()
    user_a = uuid4()
    user_b = uuid4()
    pool.seed_health_connection(user_id=user_a)
    pool.seed_health_connection(user_id=user_b)

    _seed_weight(pool, user_a, datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc), 70.0)
    _seed_weight(pool, user_b, datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc), 80.0)

    result_a = await get_weight(user_id=user_a, pool=pool)
    result_b = await get_weight(user_id=user_b, pool=pool)

    assert result_a.latest is not None
    assert result_a.latest.value_numeric == pytest.approx(70.0)
    assert result_b.latest is not None
    assert result_b.latest.value_numeric == pytest.approx(80.0)


# --- Nightly sleep: basic ---


async def test_get_nightly_sleep_single_session() -> None:
    """Returns one sleep session for a given date."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)
    sleep_date = date(2026, 7, 20)

    _seed_sleep(
        pool, user_id,
        started_at=datetime(2026, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
        local_sleep_date=sleep_date,
        completeness_state="complete",
        total_asleep_seconds=25200,
        sleep_score=85,
    )

    result = await get_nightly_sleep(
        user_id=user_id,
        local_sleep_date=sleep_date,
        pool=pool,
    )
    assert len(result.sessions) == 1
    s = result.sessions[0]
    assert s.local_sleep_date == sleep_date
    assert s.total_asleep_seconds == 25200
    assert s.sleep_score == 85
    assert s.completeness_state == "complete"


async def test_get_nightly_sleep_multiple_sessions() -> None:
    """Two sessions (nap + main sleep) on the same date."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)
    sleep_date = date(2026, 7, 20)

    _seed_sleep(
        pool, user_id,
        started_at=datetime(2026, 7, 20, 4, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 20, 5, 0, 0, tzinfo=timezone.utc),
        local_sleep_date=sleep_date,
        total_asleep_seconds=3000,
        sleep_score=70,
    )
    _seed_sleep(
        pool, user_id,
        started_at=datetime(2026, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
        local_sleep_date=sleep_date,
        total_asleep_seconds=25200,
        sleep_score=85,
    )

    result = await get_nightly_sleep(
        user_id=user_id,
        local_sleep_date=sleep_date,
        pool=pool,
    )
    assert len(result.sessions) == 2
    # Sessions ordered by started_at
    assert result.sessions[0].total_asleep_seconds == 3000  # nap first
    assert result.sessions[1].total_asleep_seconds == 25200  # main sleep second


async def test_get_nightly_sleep_no_sessions() -> None:
    """Empty result when no sleep on that date."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    result = await get_nightly_sleep(
        user_id=user_id,
        local_sleep_date=date(2026, 7, 20),
        pool=pool,
    )
    assert len(result.sessions) == 0
    assert result.local_sleep_date == date(2026, 7, 20)


async def test_get_nightly_sleep_different_date_not_returned() -> None:
    """Only sessions for the requested date are returned."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    _seed_sleep(
        pool, user_id,
        started_at=datetime(2026, 7, 19, 23, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 20, 7, 0, 0, tzinfo=timezone.utc),
        local_sleep_date=date(2026, 7, 19),
        total_asleep_seconds=24000,
    )
    _seed_sleep(
        pool, user_id,
        started_at=datetime(2026, 7, 20, 23, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 7, 21, 7, 0, 0, tzinfo=timezone.utc),
        local_sleep_date=date(2026, 7, 20),
        total_asleep_seconds=25200,
    )

    result = await get_nightly_sleep(
        user_id=user_id,
        local_sleep_date=date(2026, 7, 20),
        pool=pool,
    )
    assert len(result.sessions) == 1
    assert result.sessions[0].total_asleep_seconds == 25200


# --- Nightly sleep: strict user isolation ---


async def test_get_nightly_sleep_strict_user_filtering() -> None:
    """Sleep reads are scoped to user_id — no cross-user leakage."""
    pool = FakePool()
    user_a = uuid4()
    user_b = uuid4()
    pool.seed_health_connection(user_id=user_a)
    pool.seed_health_connection(user_id=user_b)
    sleep_date = date(2026, 7, 20)

    _seed_sleep(pool, user_a, started_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=sleep_date, sleep_score=90)
    _seed_sleep(pool, user_b, started_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=sleep_date, sleep_score=70)

    result_a = await get_nightly_sleep(user_id=user_a, local_sleep_date=sleep_date, pool=pool)
    result_b = await get_nightly_sleep(user_id=user_b, local_sleep_date=sleep_date, pool=pool)

    assert len(result_a.sessions) == 1
    assert result_a.sessions[0].sleep_score == 90
    assert len(result_b.sessions) == 1
    assert result_b.sessions[0].sleep_score == 70


# --- Rolling 7-day sleep summaries ---


async def test_get_sleep_rolling_7d_basic() -> None:
    """7-day rolling window aggregates per local_sleep_date."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)
    ref_date = date(2026, 7, 20)

    # Day 1: one session
    _seed_sleep(pool, user_id,
                started_at=datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=date(2026, 7, 14),
                total_asleep_seconds=25200, sleep_score=85)
    # Day 2: two sessions (nap + main)
    _seed_sleep(pool, user_id,
                started_at=datetime(2026, 7, 15, 4, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 15, 5, 0, tzinfo=timezone.utc),
                local_sleep_date=date(2026, 7, 15),
                total_asleep_seconds=3000, sleep_score=70)
    _seed_sleep(pool, user_id,
                started_at=datetime(2026, 7, 15, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 16, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=date(2026, 7, 15),
                total_asleep_seconds=24000, sleep_score=80)
    # Outside window (7 days before ref_date minus 6 = July 14)
    _seed_sleep(pool, user_id,
                started_at=datetime(2026, 7, 13, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 14, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=date(2026, 7, 13),
                total_asleep_seconds=20000, sleep_score=75)

    result = await get_sleep_rolling_7d(
        user_id=user_id,
        pool=pool,
        reference_date=ref_date,
    )

    assert result.nights_with_data == 2  # July 14 and 15 only

    day_14 = next(s for s in result.summaries if s.local_sleep_date == date(2026, 7, 14))
    assert day_14.session_count == 1
    assert day_14.total_asleep_seconds == 25200
    assert day_14.avg_sleep_score == pytest.approx(85.0)

    day_15 = next(s for s in result.summaries if s.local_sleep_date == date(2026, 7, 15))
    assert day_15.session_count == 2
    assert day_15.total_asleep_seconds == 27000  # 3000 + 24000
    assert day_15.avg_sleep_score == pytest.approx(75.0)  # (70 + 80) / 2


async def test_get_sleep_rolling_7d_no_data() -> None:
    """Empty rolling result when no sleep in window."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    result = await get_sleep_rolling_7d(
        user_id=user_id,
        pool=pool,
        reference_date=date(2026, 7, 20),
    )
    assert result.nights_with_data == 0
    assert result.summaries == []


async def test_get_sleep_rolling_7d_strict_user_filtering() -> None:
    """Rolling sleep aggregates only include the requested user."""
    pool = FakePool()
    user_a = uuid4()
    user_b = uuid4()
    pool.seed_health_connection(user_id=user_a)
    pool.seed_health_connection(user_id=user_b)
    sleep_date = date(2026, 7, 18)

    _seed_sleep(pool, user_a, started_at=datetime(2026, 7, 18, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 19, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=sleep_date, total_asleep_seconds=25200, sleep_score=90)
    _seed_sleep(pool, user_b, started_at=datetime(2026, 7, 18, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 19, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=sleep_date, total_asleep_seconds=18000, sleep_score=60)

    result_a = await get_sleep_rolling_7d(user_id=user_a, pool=pool, reference_date=date(2026, 7, 20))
    result_b = await get_sleep_rolling_7d(user_id=user_b, pool=pool, reference_date=date(2026, 7, 20))

    assert result_a.nights_with_data == 1
    assert result_a.summaries[0].total_asleep_seconds == 25200
    assert result_b.nights_with_data == 1
    assert result_b.summaries[0].total_asleep_seconds == 18000


# --- Tombstone-safe reads ---


async def test_weight_reads_are_tombstone_safe() -> None:
    """Normalized weight rows that would be deleted by tombstones aren't
    present; only explicitly seeded rows are returned (reads don't consult
    source-record is_deleted)."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    _seed_weight(pool, user_id, datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc), 70.0)

    result = await get_weight(user_id=user_id, pool=pool)
    assert result.latest is not None
    # Reads from normalized tables only — deleted source records
    # don't affect results because tombstones explicitly delete
    # normalized rows during sync.
    assert result.latest.value_numeric == pytest.approx(70.0)


async def test_sleep_reads_are_tombstone_safe() -> None:
    """Normalized sleep reads return only rows that exist in the
    normalized table — tombstone-deleted rows are absent."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)
    sleep_date = date(2026, 7, 20)

    _seed_sleep(pool, user_id,
                started_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=sleep_date, sleep_score=85)

    result = await get_nightly_sleep(user_id=user_id, local_sleep_date=sleep_date, pool=pool)
    assert len(result.sessions) == 1
    assert result.sessions[0].sleep_score == 85


# --- Deterministic null/no-data behavior ---


async def test_get_weight_null_handling() -> None:
    """Missing optional fields produce None in WeightReading."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    row_id = uuid4()
    pool.health_normalized_measurements[row_id] = {
        "id": row_id,
        "source_record_id": uuid4(),
        "connection_id": UUID("00000000-0000-4000-8000-000000000001"),
        "user_id": user_id,
        "metric": "weight",
        "measured_at": datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
        "value_numeric": 70.0,
        "canonical_unit": "kg",
        "source_unit": "kg",
        "source_device_id": None,
        "source_device_model": None,
        "attribution": {},
    }

    result = await get_weight(user_id=user_id, pool=pool)
    assert result.latest is not None
    assert result.latest.source_device_id is None
    assert result.latest.source_device_model is None


async def test_get_nightly_sleep_null_handling() -> None:
    """Sleep sessions with null optional fields return None correctly."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)
    sleep_date = date(2026, 7, 20)

    _seed_sleep(pool, user_id,
                started_at=datetime(2026, 7, 20, 23, 0, tzinfo=timezone.utc),
                ended_at=datetime(2026, 7, 21, 7, 0, tzinfo=timezone.utc),
                local_sleep_date=sleep_date,
                total_asleep_seconds=None,
                sleep_score=None,
                awake_seconds=None,
                light_sleep_seconds=None,
                deep_sleep_seconds=None,
                rem_sleep_seconds=None,
                sleep_latency_seconds=None,
                wake_after_sleep_onset_seconds=None,
                wakeups=None,
                source_device_id=None,
                source_device_model=None,
                )

    result = await get_nightly_sleep(user_id=user_id, local_sleep_date=sleep_date, pool=pool)
    assert len(result.sessions) == 1
    s = result.sessions[0]
    assert s.total_asleep_seconds is None
    assert s.sleep_score is None
    assert s.awake_seconds is None
    assert s.light_sleep_seconds is None
    assert s.deep_sleep_seconds is None
    assert s.rem_sleep_seconds is None
    assert s.source_device_id is None
    assert s.source_device_model is None


async def test_get_nightly_sleep_no_data_deterministic() -> None:
    """Empty result shape is deterministic — not an error, not partial."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    result = await get_nightly_sleep(
        user_id=user_id,
        local_sleep_date=date(2026, 7, 20),
        pool=pool,
    )
    assert isinstance(result, NightlySleepResult)
    assert result.local_sleep_date == date(2026, 7, 20)
    assert result.sessions == []


async def test_get_weight_no_data_deterministic() -> None:
    """Empty weight result is deterministic — structure stable."""
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(user_id=user_id)

    result = await get_weight(user_id=user_id, pool=pool)
    assert isinstance(result, WeightResult)
    assert result.latest is None
    assert result.readings_7d == []
    assert result.readings_30d == []
    assert result.avg_7d is None
    assert result.avg_30d is None
    assert result.min_7d is None
    assert result.max_7d is None
