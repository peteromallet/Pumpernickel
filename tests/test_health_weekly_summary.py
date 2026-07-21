"""Tests for the pure weekly health digest generator.

Covers ``generate_weekly_digest()`` and ``WeeklyHealthDigest`` across
enabled/disabled, no-connection, empty, single-domain, full-data, and
user-scoping scenarios.  Uses ``FakePool`` and in-memory health tables
— no live credentials, network, or provider fakes needed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.services.health_sync.weekly_summary import (
    WeeklyHealthDigest,
    generate_weekly_digest,
)
from tests.conftest import FakePool


# ── helpers ──────────────────────────────────────────────────────────────────


def _make_connection(pool: FakePool, user_id: UUID) -> UUID:
    """Seed an active Withings connection and return its id."""
    return pool.seed_health_connection(user_id=user_id)


def _seed_weight(pool: FakePool, user_id: UUID, *, measured_at: datetime, value: float) -> None:
    """Insert a single weight measurement into the FakePool."""
    row_id = uuid4()
    pool.health_normalized_measurements[row_id] = {
        "id": row_id,
        "connection_id": uuid4(),
        "user_id": user_id,
        "source_record_id": uuid4(),
        "metric": "weight",
        "measured_at": measured_at,
        "value_numeric": value,
        "canonical_unit": "kg",
        "source_device_id": None,
        "source_device_model": None,
    }


def _seed_sleep(
    pool: FakePool,
    user_id: UUID,
    *,
    local_sleep_date: date,
    started_at: datetime,
    ended_at: datetime,
    total_asleep_seconds: int = 28800,
    sleep_score: int | None = None,
) -> None:
    """Insert a single sleep session into the FakePool."""
    row_id = uuid4()
    pool.health_normalized_sleep[row_id] = {
        "id": row_id,
        "connection_id": uuid4(),
        "user_id": user_id,
        "source_record_id": uuid4(),
        "started_at": started_at,
        "ended_at": ended_at,
        "local_sleep_date": local_sleep_date,
        "local_timezone": "UTC",
        "local_offset_seconds": 0,
        "completeness_state": "complete",
        "total_in_bed_seconds": total_asleep_seconds + 600,
        "total_asleep_seconds": total_asleep_seconds,
        "awake_seconds": 300,
        "light_sleep_seconds": 12000,
        "deep_sleep_seconds": 8000,
        "rem_sleep_seconds": 8000,
        "sleep_latency_seconds": 600,
        "wake_after_sleep_onset_seconds": 300,
        "wakeups": 2,
        "sleep_score": sleep_score,
        "source_device_id": None,
        "source_device_model": None,
    }


def _seed_workout(
    pool: FakePool,
    user_id: UUID,
    *,
    started_at: datetime,
    duration_seconds: int = 1800,
    workout_type: str = "running",
    energy_kcal: float = 250.0,
) -> None:
    """Insert a single workout into the FakePool."""
    source_id = uuid4()
    row_id = uuid4()
    pool.health_normalized_workouts[row_id] = {
        "id": row_id,
        "connection_id": uuid4(),
        "user_id": user_id,
        "source_record_id": source_id,
        "started_at": started_at,
        "ended_at": started_at + timedelta(seconds=duration_seconds),
        "local_date": started_at.date(),
        "local_timezone": "UTC",
        "local_offset_seconds": 0,
        "workout_type": workout_type,
        "duration_seconds": duration_seconds,
        "pause_duration_seconds": 0,
        "distance_meters": 5000.0,
        "steps": 6000,
        "energy_kcal": energy_kcal,
        "elevation_gain_meters": 50.0,
        "average_heart_rate_bpm": 145.0,
        "max_heart_rate_bpm": 175.0,
        "source_device_id": None,
        "source_device_model": None,
    }


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ── tests ────────────────────────────────────────────────────────────────────


class TestWeeklyDigestDisabled:
    """When the flag is off the generator returns an empty digest."""

    async def test_disabled_flag_returns_empty_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "false")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_id = uuid4()
        _make_connection(pool, user_id)
        _seed_weight(pool, user_id, measured_at=_utcnow(), value=72.0)

        result = await generate_weekly_digest(user_id=user_id, pool=pool)
        assert isinstance(result, WeeklyHealthDigest)
        assert result.connection_active is False
        assert result.freshness is None
        assert result.weight is None
        assert result.sleep is None
        assert result.workouts is None
        assert result.generated_at_utc is None


class TestWeeklyDigestNoConnection:
    """When the user has no active Withings connection."""

    async def test_no_connection_returns_empty_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_id = uuid4()

        result = await generate_weekly_digest(user_id=user_id, pool=pool)
        assert isinstance(result, WeeklyHealthDigest)
        assert result.connection_active is False
        assert result.weight is None
        assert result.sleep is None
        assert result.workouts is None


class TestWeeklyDigestEmptyData:
    """Active connection but no normalized rows."""

    async def test_active_connection_empty_data_returns_metadata_only(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_id = uuid4()
        _make_connection(pool, user_id)

        result = await generate_weekly_digest(user_id=user_id, pool=pool)
        assert result.connection_active is True
        assert result.freshness is not None
        assert result.freshness.is_fresh is False  # never synced
        assert result.weight is None
        assert result.sleep is None
        assert result.workouts is None
        assert result.generated_at_utc is None  # no health values to timestamp


class TestWeeklyDigestWeightOnly:
    """Only weight data exists."""

    async def test_weight_only_populates_weight_in_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_id = uuid4()
        _make_connection(pool, user_id)
        now = _utcnow()
        _seed_weight(pool, user_id, measured_at=now, value=75.5)

        result = await generate_weekly_digest(user_id=user_id, pool=pool)
        assert result.connection_active is True
        assert result.freshness is not None
        assert result.weight is not None
        assert result.weight.latest is not None
        assert result.weight.latest.value_numeric == 75.5
        assert result.sleep is None
        assert result.workouts is None
        assert result.generated_at_utc is not None


class TestWeeklyDigestSleepOnly:
    """Only sleep data exists."""

    async def test_sleep_only_populates_sleep_in_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_id = uuid4()
        _make_connection(pool, user_id)
        today = _utcnow()
        _seed_sleep(
            pool,
            user_id,
            local_sleep_date=today.date(),
            started_at=today - timedelta(hours=10),
            ended_at=today - timedelta(hours=2),
            total_asleep_seconds=25200,
            sleep_score=82,
        )

        result = await generate_weekly_digest(user_id=user_id, pool=pool)
        assert result.connection_active is True
        assert result.weight is None
        assert result.sleep is not None
        assert result.sleep.nights_with_data == 1
        assert result.workouts is None
        assert result.generated_at_utc is not None


class TestWeeklyDigestWorkoutOnly:
    """Only workout data exists."""

    async def test_workout_only_populates_workouts_in_digest(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_id = uuid4()
        _make_connection(pool, user_id)
        now = _utcnow()
        _seed_workout(pool, user_id, started_at=now - timedelta(hours=2))

        result = await generate_weekly_digest(user_id=user_id, pool=pool)
        assert result.connection_active is True
        assert result.weight is None
        assert result.sleep is None
        assert result.workouts is not None
        assert result.workouts.days_with_workouts == 1
        assert result.generated_at_utc is not None


class TestWeeklyDigestFullData:
    """All three domains populated."""

    async def test_full_data_populates_all_domains(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_id = uuid4()
        _make_connection(pool, user_id)
        now = _utcnow()

        _seed_weight(pool, user_id, measured_at=now, value=68.2)
        _seed_sleep(
            pool,
            user_id,
            local_sleep_date=now.date(),
            started_at=now - timedelta(hours=12),
            ended_at=now - timedelta(hours=4),
        )
        _seed_workout(pool, user_id, started_at=now - timedelta(hours=3))

        result = await generate_weekly_digest(user_id=user_id, pool=pool)
        assert result.connection_active is True
        assert result.weight is not None
        assert result.weight.latest is not None
        assert result.sleep is not None
        assert result.sleep.nights_with_data == 1
        assert result.workouts is not None
        assert result.workouts.days_with_workouts == 1
        assert result.generated_at_utc is not None


class TestWeeklyDigestUserScoping:
    """Cross-user isolation: only the target user's data is returned."""

    async def test_other_user_data_not_returned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HEALTH_WEEKLY_SUMMARY_ENABLED", "true")
        from app.config import get_settings

        get_settings.cache_clear()

        pool = FakePool()
        user_a = uuid4()
        user_b = uuid4()

        _make_connection(pool, user_a)
        _make_connection(pool, user_b)

        _seed_weight(pool, user_a, measured_at=_utcnow(), value=70.0)
        _seed_weight(pool, user_b, measured_at=_utcnow(), value=90.0)

        result_a = await generate_weekly_digest(user_id=user_a, pool=pool)
        result_b = await generate_weekly_digest(user_id=user_b, pool=pool)

        assert result_a.weight is not None
        assert result_a.weight.latest is not None
        assert result_a.weight.latest.value_numeric == 70.0

        assert result_b.weight is not None
        assert result_b.weight.latest is not None
        assert result_b.weight.latest.value_numeric == 90.0

        # Re-verify user A's value wasn't polluted
        assert result_a.weight.latest.value_numeric == 70.0


class TestWeeklyDigestDataclassDefaults:
    """Verify the WeeklyHealthDigest defaults are correct."""

    def test_default_digest_has_all_none(self) -> None:
        d = WeeklyHealthDigest()
        assert d.connection_active is False
        assert d.freshness is None
        assert d.weight is None
        assert d.sleep is None
        assert d.workouts is None
        assert d.generated_at_utc is None
