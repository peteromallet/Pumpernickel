"""Direct contract tests for Hector's private health hot-context blocks."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from app.services.health_sync.read_models import get_weekly_workout_summary
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


def _workout(
    pool,
    user_id,
    *,
    started_at,
    workout_type,
    duration,
    local_timezone="Europe/Berlin",
    local_offset_seconds=7200,
):
    row_id = uuid4()
    pool.health_normalized_workouts[row_id] = {
        "id": row_id,
        "source_record_id": uuid4(),
        "connection_id": next(iter(pool.health_connections)),
        "user_id": user_id,
        "started_at": started_at,
        "ended_at": (
            started_at + timedelta(seconds=duration) if duration is not None else None
        ),
        "local_timezone": local_timezone,
        "local_offset_seconds": local_offset_seconds,
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
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 6, 30),
        started_at=datetime(2026, 6, 29, 20, 0, tzinfo=UTC),
        ended_at=datetime(2026, 6, 30, 6, 0, tzinfo=UTC),
        asleep=10 * 3600,
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
    assert "sleep_average: 6h53m/night; nights_with_duration=2/7" in block
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
            duration=0 if index == 24 else 10 * 60,
        )

    block = await _build_workout_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    past_24h = next(line for line in block.splitlines() if "past_24h:" in line)
    assert past_24h.count("walking ") == 25
    assert "2026-07-20T12:00+02:00" in past_24h  # exact cutoff
    assert "2026-07-21T12:00+02:00" in past_24h  # exact now


@pytest.mark.asyncio
async def test_long_term_health_is_compact_dst_safe_and_honest_when_sparse(
    fake_pool,
):
    berlin = ZoneInfo("Europe/Berlin")
    now = datetime(2026, 4, 2, 10, 0, tzinfo=UTC)
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)

    # Three nights straddle the spring DST transition and midnight. The
    # representative bedtime must remain near midnight, never around noon.
    local_nights = [
        (datetime(2026, 3, 27, 23, 30, tzinfo=berlin), datetime(2026, 3, 28, 7, 0, tzinfo=berlin)),
        (datetime(2026, 3, 28, 23, 50, tzinfo=berlin), datetime(2026, 3, 29, 7, 0, tzinfo=berlin)),
        (datetime(2026, 3, 30, 0, 10, tzinfo=berlin), datetime(2026, 3, 30, 7, 0, tzinfo=berlin)),
    ]
    for started_local, ended_local in local_nights:
        _sleep(
            fake_pool,
            user_id,
            sleep_date=ended_local.date(),
            started_at=started_local.astimezone(UTC),
            ended_at=ended_local.astimezone(UTC),
            asleep=7 * 3600,
        )

    # One recent measurement is insufficient for a delta/rate. Two older
    # points make the 90-day endpoint trend independently meaningful.
    _measurement(
        fake_pool,
        user_id,
        metric="weight",
        measured_at=datetime(2026, 3, 29, 7, 0, tzinfo=UTC),
        value=72.0,
        unit="kg",
    )
    _measurement(
        fake_pool,
        user_id,
        metric="weight",
        measured_at=datetime(2026, 1, 15, 7, 0, tzinfo=UTC),
        value=74.0,
        unit="kg",
    )

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=now, timezone_name="Europe/Berlin"
    )

    assert block is not None
    long_lines = block.split("longer_term_at_a_glance", 1)[1].splitlines()
    assert len(long_lines) == 3  # marker tail + exactly one 30d and one 90d row
    assert sum(map(len, long_lines)) < 650
    row_30 = next(line for line in long_lines if "30d (" in line)
    row_90 = next(line for line in long_lines if "90d (" in line)
    assert "nights=3/30" in row_30
    assert "avg=7h00m" in row_30
    assert "median_bed=23:50" in row_30
    assert "median_wake=07:00" in row_30
    assert "days=1/30" in row_30
    assert "delta/rate=insufficient_data(n<2)" in row_30
    assert "days=2/90" in row_90
    assert "delta=-2.0 kg" in row_90
    assert "12:00" not in row_30


@pytest.mark.asyncio
async def test_long_term_activity_aggregates_without_daily_expansion(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    # Two recent episodes, one with unknown duration, plus one in the prior
    # 60-day comparison window.
    _workout(
        fake_pool,
        user_id,
        started_at=NOW - timedelta(days=5),
        workout_type="running",
        duration=60 * 60,
    )
    _workout(
        fake_pool,
        user_id,
        started_at=NOW - timedelta(days=10),
        workout_type="walking",
        duration=30 * 60,
    )
    _workout(
        fake_pool,
        user_id,
        started_at=NOW - timedelta(days=45),
        workout_type="cycling",
        duration=None,
    )

    block = await _build_workout_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    long_lines = block.split("longer_term_activity_at_a_glance", 1)[1].splitlines()
    assert len(long_lines) == 3
    assert sum(map(len, long_lines)) < 500
    row_30 = next(line for line in long_lines if "30d:" in line)
    row_90 = next(line for line in long_lines if "90d:" in line)
    assert "active_days=2/30; episodes=2" in row_30
    assert "known_episode_duration_sum=1h30m (episodes_with_duration=2/2)" in row_30
    assert "top_types_by_known_duration=running:1h00m,walking:0h30m" in row_30
    assert "active_days=3/90; episodes=3" in row_90
    assert "episodes_with_duration=2/3" in row_90
    assert "recent30_vs_prior60_weekly=" in row_90


@pytest.mark.asyncio
async def test_sleep_padding_and_daily_absence_semantics(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    # The 90-day first actual wake date is 2026-04-22, but the provider's
    # stored date is one day earlier. The padded query must still fetch it.
    _sleep(
        fake_pool,
        user_id,
        sleep_date=date(2026, 4, 21),
        started_at=datetime(2026, 4, 21, 20, 0, tzinfo=UTC),
        ended_at=datetime(2026, 4, 22, 5, 0, tzinfo=UTC),
        asleep=8 * 3600,
    )
    # Nap-only today and partial overnight yesterday are rows, but neither is
    # a qualifying overnight group; they must not be rendered as no_data.
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
        started_at=datetime(2026, 7, 19, 21, 0, tzinfo=UTC),
        ended_at=datetime(2026, 7, 20, 5, 0, tzinfo=UTC),
        asleep=7 * 3600,
        completeness_state="partial",
    )

    block = await _build_health_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    assert "2026-07-21 [today_partial]: no_overnight_sleep" in block
    assert "2026-07-20: no_overnight_sleep" in block
    row_90 = next(
        line
        for line in block.splitlines()
        if line.strip().startswith("90d (")
    )
    assert "nights=1/90" in row_90


@pytest.mark.asyncio
async def test_activity_first_date_uses_recorded_utc_plus_14_timezone(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    # Pago Pago is behind UTC while Kiritimati is UTC+14. This instant is the
    # first 90-day date in the recorded timezone, but falls before the old
    # current-local padded SQL bound. UTC-midnight padding must retain it.
    _workout(
        fake_pool,
        user_id,
        started_at=datetime(2026, 4, 20, 10, 30, tzinfo=UTC),
        workout_type="swimming",
        duration=30 * 60,
        local_timezone="Pacific/Kiritimati",
        local_offset_seconds=None,
    )

    block = await _build_workout_summary_block(
        user_id,
        fake_pool,
        now_utc=NOW,
        timezone_name="Pacific/Pago_Pago",
    )

    assert block is not None
    row_90 = next(line for line in block.splitlines() if line.strip().startswith("90d:"))
    assert "active_days=1/90; episodes=1" in row_90
    assert "swimming:0h30m" in row_90


@pytest.mark.asyncio
async def test_weekly_workouts_prefer_current_user_timezone(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    # The recorded Kiritimati date is Jul 21, while the current Los Angeles
    # date is Jul 20. Weekly coaching rows must retain current-user precedence.
    _workout(
        fake_pool,
        user_id,
        started_at=datetime(2026, 7, 21, 1, 0, tzinfo=UTC),
        workout_type="walking",
        duration=30 * 60,
        local_timezone="Pacific/Kiritimati",
        local_offset_seconds=None,
    )

    result = await get_weekly_workout_summary(
        user_id=user_id,
        pool=fake_pool,
        reference_date=date(2026, 7, 20),
        timezone_name="America/Los_Angeles",
    )

    assert result.days_with_workouts == 1
    assert result.summaries[0].local_date == date(2026, 7, 20)


@pytest.mark.asyncio
async def test_sparse_comparisons_always_disclose_cohort_counts(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    for days_ago in (10, 45):
        wake = NOW - timedelta(days=days_ago)
        _sleep(
            fake_pool,
            user_id,
            sleep_date=wake.astimezone(ZoneInfo("Europe/Berlin")).date(),
            started_at=wake - timedelta(hours=7),
            ended_at=wake,
            asleep=7 * 3600,
        )
        _workout(
            fake_pool,
            user_id,
            started_at=wake,
            workout_type="walking",
            duration=30 * 60,
        )

    health = await _build_health_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )
    activity = await _build_workout_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert health is not None and activity is not None
    health_90 = next(
        line for line in health.splitlines() if line.strip().startswith("90d (")
    )
    activity_90 = next(
        line for line in activity.splitlines() if line.strip().startswith("90d:")
    )
    assert "recent30_vs_prior60=insufficient_data(n=1/1)" in health_90
    assert "recent30_vs_prior60_weekly=0.2/0.1(n=1/1)" in activity_90


@pytest.mark.asyncio
async def test_overlapping_activity_episodes_are_counted_and_summed(fake_pool):
    user_id = uuid4()
    fake_pool.seed_health_connection(user_id=user_id)
    started = NOW - timedelta(days=5)
    _workout(
        fake_pool,
        user_id,
        started_at=started,
        workout_type="running",
        duration=60 * 60,
    )
    _workout(
        fake_pool,
        user_id,
        started_at=started + timedelta(minutes=15),
        workout_type="walking",
        duration=60 * 60,
    )

    block = await _build_workout_summary_block(
        user_id, fake_pool, now_utc=NOW, timezone_name="Europe/Berlin"
    )

    assert block is not None
    row_30 = next(line for line in block.splitlines() if line.strip().startswith("30d:"))
    assert "episodes=2" in row_30
    assert "known_episode_duration_sum=2h00m" in row_30
    assert "episodes_with_duration=2/2" in row_30
