"""Direct contract tests for Hector's private health hot-context blocks."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.hot_context_solo import (
    _build_health_summary_block,
    _build_workout_summary_block,
)


NOW = datetime(2026, 7, 21, 10, 0, tzinfo=UTC)  # 12:00 Europe/Berlin


def _measurement(pool, user_id, *, metric, measured_at, value, unit):
    row_id = uuid4()
    pool.health_normalized_measurements[row_id] = {
        "id": row_id,
        "source_record_id": uuid4(),
        "connection_id": next(iter(pool.health_connections)),
        "user_id": user_id,
        "metric": metric,
        "measured_at": measured_at,
        "value_numeric": value,
        "canonical_unit": unit,
        "source_device_id": None,
        "source_device_model": None,
    }


def _sleep(
    pool,
    user_id,
    *,
    sleep_date,
    started_at,
    ended_at,
    asleep,
    completeness_state="complete",
):
    row_id = uuid4()
    pool.health_normalized_sleep[row_id] = {
        "id": row_id,
        "source_record_id": uuid4(),
        "connection_id": next(iter(pool.health_connections)),
        "user_id": user_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "local_sleep_date": sleep_date,
        "local_timezone": "Europe/Berlin",
        "local_offset_seconds": 7200,
        "completeness_state": completeness_state,
        "total_in_bed_seconds": asleep + 1800,
        "total_asleep_seconds": asleep,
        "awake_seconds": 1800,
        "light_sleep_seconds": None,
        "deep_sleep_seconds": None,
        "rem_sleep_seconds": None,
        "sleep_latency_seconds": None,
        "wake_after_sleep_onset_seconds": None,
        "wakeups": None,
        "sleep_score": 84,
        "source_device_id": None,
        "source_device_model": None,
    }


def _workout(pool, user_id, *, started_at, workout_type, duration):
    row_id = uuid4()
    pool.health_normalized_workouts[row_id] = {
        "id": row_id,
        "source_record_id": uuid4(),
        "connection_id": next(iter(pool.health_connections)),
        "user_id": user_id,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=duration),
        "local_timezone": "Europe/Berlin",
        "local_offset_seconds": 7200,
        "workout_type": workout_type,
        "duration_seconds": duration,
        "pause_duration_seconds": None,
        "distance_meters": None,
        "steps": None,
        "energy_kcal": None,
        "elevation_gain_meters": None,
        "average_heart_rate_bpm": None,
        "max_heart_rate_bpm": None,
        "source_device_id": None,
        "source_device_model": None,
    }


@pytest.mark.asyncio
async def test_health_block_latest_first_and_local_time(fake_pool):
    user_id = uuid4()
    connection_id = fake_pool.seed_health_connection(user_id=user_id)
    fake_pool.health_connections[connection_id]["last_success_at"] = NOW - timedelta(hours=1)

    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 21),
        started_at=datetime(2026, 7, 20, 21, 30, tzinfo=UTC),
        ended_at=datetime(2026, 7, 21, 5, 15, tzinfo=UTC),
        asleep=7 * 3600 + 15 * 60,
    )
    # A later daytime nap must not replace the overnight headline or extend
    # the nightly wake time in the trend row.
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 21),
        started_at=datetime(2026, 7, 21, 11, 0, tzinfo=UTC),
        ended_at=datetime(2026, 7, 21, 12, 0, tzinfo=UTC),
        asleep=45 * 60,
    )
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 20),
        started_at=datetime(2026, 7, 19, 22, 0, tzinfo=UTC),
        ended_at=datetime(2026, 7, 20, 5, 0, tzinfo=UTC),
        asleep=6 * 3600 + 30 * 60,
    )
    _measurement(
        fake_pool,
        user_id,
        metric="weight",
        measured_at=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        value=72.4,
        unit="kg",
    )
    _measurement(
        fake_pool,
        user_id,
        metric="fat_ratio",
        measured_at=datetime(2026, 7, 21, 6, 0, tzinfo=UTC),
        value=18.2,
        unit="percent",
    )
    _measurement(
        fake_pool,
        user_id,
        metric="weight",
        measured_at=datetime(2026, 7, 19, 6, 0, tzinfo=UTC),
        value=73.0,
        unit="kg",
    )

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    assert "latest_completed_sleep_past_24h (wake_date=2026-07-21)" in block
    assert "bed_local=2026-07-20T23:30+02:00" in block
    assert "wake_local=2026-07-21T07:15+02:00" in block
    assert "weight=72.4 kg @ 2026-07-21T08:00+02:00 (age=4h00m)" in block
    assert "fat_ratio=18.2 percent @ 2026-07-21T08:00+02:00" in block
    assert "delta_oldest_to_newest=-0.6 kg" in block
    assert block.index("latest:") < block.index("recent_7_local_dates")
    assert "2026-07-21 [today_partial]: asleep=7h15m" in block
    assert "2026-07-21T14:00+02:00" not in block
    assert "2026-07-20: asleep=6h30m" in block
    assert "2026-07-19: no_data" in block


@pytest.mark.asyncio
async def test_health_block_never_relabels_old_sleep_as_recent(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 19),
        started_at=datetime(2026, 7, 18, 22, 0, tzinfo=UTC),
        ended_at=datetime(2026, 7, 19, 5, 0, tzinfo=UTC),
        asleep=7 * 3600,
    )

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    assert "latest_completed_sleep_past_24h: no_recent_overnight_sleep" in block
    assert "2026-07-19: asleep=7h00m" in block


@pytest.mark.asyncio
async def test_workout_block_has_past_24h_and_explicit_week_gaps(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    _workout(
        fake_pool,
        user_id,
        started_at=datetime(2026, 7, 21, 8, 0, tzinfo=UTC),
        workout_type="strength",
        duration=45 * 60,
    )
    _workout(
        fake_pool,
        user_id,
        started_at=datetime(2026, 7, 20, 16, 0, tzinfo=UTC),
        workout_type="walking",
        duration=30 * 60,
    )

    block = await _build_workout_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    assert "past_24h: strength 0h45m @ 2026-07-21T10:00+02:00" in block
    assert "2026-07-21 [today_partial]: count=1; types=strength, 45min total" in block
    assert "2026-07-20: count=1; types=walking, 30min total" in block
    assert "2026-07-19: no_workout_data" in block
    assert block.index("past_24h:") < block.index("by_date (newest_first)")


@pytest.mark.asyncio
async def test_disconnected_connection_is_not_rendered(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id, status="disconnected")
    _measurement(
        fake_pool,
        user_id,
        metric="weight",
        measured_at=NOW - timedelta(hours=2),
        value=72.4,
        unit="kg",
    )

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )
    assert block is None


@pytest.mark.asyncio
async def test_active_connection_with_no_rows_reports_missing_and_stale(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    assert "latest_completed_sleep_past_24h: no_recent_overnight_sleep" in block
    assert "measurements_past_24h: no_data" in block
    assert "sync_status: stale; last_success_at=unknown" in block
    assert "sleep_average: no_data" in block


@pytest.mark.asyncio
async def test_sleep_headline_uses_latest_complete_group_not_longest_or_partial(
    fake_pool,
):
    now = datetime(2026, 7, 21, 4, 0, tzinfo=UTC)  # 06:00 Berlin
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    # Older and longer, but still just inside the closed 24-hour window.
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 20),
        started_at=datetime(2026, 7, 19, 21, 0, tzinfo=UTC),
        ended_at=datetime(2026, 7, 20, 5, 0, tzinfo=UTC),
        asleep=8 * 3600,
    )
    # Newer complete overnight group must win even though it is shorter.
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 20),  # deliberately wrong stored date
        started_at=datetime(2026, 7, 20, 23, 0, tzinfo=UTC),
        ended_at=datetime(2026, 7, 21, 3, 0, tzinfo=UTC),
        asleep=4 * 3600,
        completeness_state="revised",
    )
    # Latest end is partial and must be ignored.
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 21),
        started_at=datetime(2026, 7, 20, 23, 30, tzinfo=UTC),
        ended_at=datetime(2026, 7, 21, 3, 30, tzinfo=UTC),
        asleep=4 * 3600,
        completeness_state="partial",
    )

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=now, timezone_name="Europe/Berlin"
    )

    assert block is not None
    headline = next(
        line for line in block.splitlines() if "latest_completed_sleep_past_24h" in line
    )
    assert "wake_date=2026-07-21" in headline
    assert "wake_local=2026-07-21T05:00+02:00" in headline
    assert "2026-07-21T05:30+02:00" not in headline
    assert "2026-07-20T07:00+02:00" not in headline


@pytest.mark.asyncio
async def test_past_24h_closed_boundaries_for_sleep_and_measurements(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    cutoff = NOW - timedelta(hours=24)
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 7, 20),
        started_at=cutoff - timedelta(hours=8),
        ended_at=cutoff,
        asleep=8 * 3600,
    )
    _measurement(
        fake_pool,
        user_id,
        metric="weight",
        measured_at=cutoff,
        value=72.0,
        unit="kg",
    )
    _measurement(
        fake_pool,
        user_id,
        metric="fat_ratio",
        measured_at=NOW,
        value=18.0,
        unit="percent",
    )
    _measurement(
        fake_pool,
        user_id,
        metric="bone_mass",
        measured_at=cutoff - timedelta(seconds=1),
        value=3.0,
        unit="kg",
    )

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    measurement_line = next(
        line for line in block.splitlines() if "measurements_past_24h" in line
    )
    assert "weight=72.0 kg" in measurement_line
    assert "fat_ratio=18.0 percent" in measurement_line
    assert "bone_mass" not in measurement_line
    assert "latest_completed_sleep_past_24h" in block


@pytest.mark.asyncio
async def test_past_24h_workouts_are_not_truncated_at_twenty(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    cutoff = NOW - timedelta(hours=24)
    for index in range(25):
        started_at = cutoff + (NOW - cutoff) * index / 24
        _workout(
            fake_pool,
            user_id,
            started_at=started_at,
            workout_type="walking",
            duration=10 * 60,
        )

    block = await _build_workout_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    past_24h = next(line for line in block.splitlines() if "past_24h:" in line)
    assert past_24h.count("walking 0h10m @") == 25
    assert "2026-07-20T12:00+02:00" in past_24h  # exact cutoff
    assert "2026-07-21T12:00+02:00" in past_24h  # exact now
