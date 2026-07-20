from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.services.health_sync import (
    FakeWithingsProvider,
    HealthResourceType,
    repository_for,
    sync_connection_resource,
)
from tests.conftest import FakePool


CALLBACK_URL = "https://example.test/api/health/devices/withings/oauth/callback"


async def _rotated_access_token(provider: FakeWithingsProvider) -> str:
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")
    return refreshed.access_token


async def test_revision_and_tombstone_replay_remain_idempotent() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    initial_provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(initial_provider)
    await sync_connection_resource(
        repository=repository,
        provider=initial_provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
    )

    revision_provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "measurements_revision"}
    )
    revision_access_token = await _rotated_access_token(revision_provider)
    await sync_connection_resource(
        repository=repository,
        provider=revision_provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=revision_access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )
    await sync_connection_resource(
        repository=repository,
        provider=revision_provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=revision_access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 8, 5, tzinfo=UTC),
    )

    revised = next(
        row for row in pool.health_source_records.values() if row["external_id"] == "grpid:9001002"
    )
    assert revised["revision_count"] == 2
    assert revised["provider_revision"] == "1784513040"
    assert revised["is_deleted"] is False

    tombstone_provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "measurements_tombstones"}
    )
    tombstone_access_token = await _rotated_access_token(tombstone_provider)
    await sync_connection_resource(
        repository=repository,
        provider=tombstone_provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=tombstone_access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 8, 10, tzinfo=UTC),
    )
    await sync_connection_resource(
        repository=repository,
        provider=tombstone_provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=tombstone_access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 8, 15, tzinfo=UTC),
    )

    tombstoned = next(
        row for row in pool.health_source_records.values() if row["external_id"] == "grpid:9001002"
    )
    assert tombstoned["revision_count"] == 3
    assert tombstoned["is_deleted"] is True
    assert tombstoned["provider_revision"] == "synthetic-delete-rev-1"
    assert len(pool.health_source_records) == 2
    assert len(pool.health_source_records_by_key) == len(pool.health_source_records)


async def test_workout_and_sleep_sync_store_normalized_records() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    workout_outcome = await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.WORKOUT,
        now=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
    )
    sleep_outcome = await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.SLEEP,
        now=datetime(2026, 7, 20, 7, 35, tzinfo=UTC),
    )

    stored_workouts = [
        row for row in pool.health_source_records.values() if row["resource_type"] == "workout"
    ]
    stored_sleep = [
        row for row in pool.health_source_records.values() if row["resource_type"] == "sleep"
    ]

    assert workout_outcome.fetched_count == 1
    assert sleep_outcome.fetched_count == 2
    assert len(stored_workouts) == 1
    assert stored_workouts[0]["external_id"] == "workout:9102001"
    assert stored_workouts[0]["starts_at"] == datetime.fromtimestamp(1784490000, tz=UTC)
    assert stored_workouts[0]["ends_at"] == datetime.fromtimestamp(1784492460, tz=UTC)
    assert len(stored_sleep) == 2
    assert {row["external_id"] for row in stored_sleep} == {
        "sleep_summary:9203001",
        'sleep:fallback:{"enddate":1784494800,"model":"Sleep Sensor","model_id":63,"startdate":1784469600}',
    }
    assert all(row["is_deleted"] is False for row in stored_workouts + stored_sleep)
