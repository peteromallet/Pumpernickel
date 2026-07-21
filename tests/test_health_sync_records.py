from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

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


# ---------------------------------------------------------------------------
# T4: normalization wiring, revision replacement, tombstone deletion
# ---------------------------------------------------------------------------


async def test_measurement_sync_normalizes_and_fans_out_derived_rows() -> None:
    """After syncing measurements, normalized rows must be created for every
    metric in the source record's measure group, with correct decoded values."""
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
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
    )

    # Two source records from the two pages
    assert len(pool.health_source_records) == 2

    normalized_rows = list(pool.health_normalized_measurements.values())
    # Page 1 (grpid:9001001): weight, fat_ratio, muscle_mass = 3 metrics
    # Page 2 (grpid:9001002): weight, fat_ratio, bone_mass = 3 metrics
    assert len(normalized_rows) == 6

    # Verify fan-out: distinct (source_record_id, metric) pairs
    pairs = {(row["source_record_id"], row["metric"]) for row in normalized_rows}
    assert len(pairs) == 6

    # Spot-check decoded values for page 1 (grpid:9001001)
    page1_rows = [
        r for r in normalized_rows
        if r["source_record_id"] == list(pool.health_source_records.values())[0]["id"]
    ]
    page1_by_metric = {r["metric"]: r for r in page1_rows}
    assert page1_by_metric["weight"]["value_numeric"] == pytest.approx(70.54)
    assert page1_by_metric["weight"]["canonical_unit"] == "kg"
    assert page1_by_metric["fat_ratio"]["value_numeric"] == pytest.approx(21.2)
    assert page1_by_metric["fat_ratio"]["canonical_unit"] == "percent"
    assert page1_by_metric["muscle_mass"]["value_numeric"] == pytest.approx(14.98)
    assert page1_by_metric["muscle_mass"]["canonical_unit"] == "kg"

    # All rows carry attribution
    for r in normalized_rows:
        assert r["attribution"]["fixture_scenario"] in (
            "measurements_page_1",
            "measurements_page_2",
        )
        assert r["connection_id"] == connection_id
        assert r["user_id"] == user_id


async def test_measurement_revision_replaces_stale_normalized_rows() -> None:
    """When a measurement record is revised, the old normalized rows must be
    replaced (not duplicated) with the new decoded values."""
    pool = FakePool()
    repository = repository_for(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    # Initial sync (pages 1 + 2)
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

    # Count normalized rows after initial sync: 6 (3/page × 2 pages)
    assert len(pool.health_normalized_measurements) == 6

    # Find the source record for grpid:9001002 (from page 2)
    page2_source = next(
        row for row in pool.health_source_records.values()
        if row["external_id"] == "grpid:9001002"
    )
    page2_record_id = page2_source["id"]

    # Capture initial values for grpid:9001002
    initial_page2_rows = [
        r for r in pool.health_normalized_measurements.values()
        if r["source_record_id"] == page2_record_id
    ]
    initial_weight = next(r for r in initial_page2_rows if r["metric"] == "weight")
    assert initial_weight["value_numeric"] == pytest.approx(70.48)  # 70480 × 10⁻³

    # Sync the revision (replaces grpid:9001002 with new values)
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

    # Total normalized rows must remain 6 (page1 unchanged, page2 replaced)
    assert len(pool.health_normalized_measurements) == 6

    # Revised values for grpid:9001002: weight=70.42, fat_ratio=20.9, bone_mass=5.45
    revised_page2_rows = [
        r for r in pool.health_normalized_measurements.values()
        if r["source_record_id"] == page2_record_id
    ]
    assert len(revised_page2_rows) == 3  # weight + fat_ratio + bone_mass (muscle_mass gone)
    revised_by_metric = {r["metric"]: r for r in revised_page2_rows}
    assert revised_by_metric["weight"]["value_numeric"] == pytest.approx(70.42)  # 70420 × 10⁻³
    assert revised_by_metric["fat_ratio"]["value_numeric"] == pytest.approx(20.9)  # 209 × 10⁻¹
    assert revised_by_metric["bone_mass"]["value_numeric"] == pytest.approx(5.45)  # 5450 × 10⁻³
    assert "muscle_mass" not in revised_by_metric  # no longer present in revision

    # Page 1 rows untouched
    page1_source = next(
        row for row in pool.health_source_records.values()
        if row["external_id"] == "grpid:9001001"
    )
    page1_rows = [
        r for r in pool.health_normalized_measurements.values()
        if r["source_record_id"] == page1_source["id"]
    ]
    assert len(page1_rows) == 3
    page1_by_metric = {r["metric"]: r for r in page1_rows}
    assert page1_by_metric["weight"]["value_numeric"] == pytest.approx(70.54)  # unchanged


async def test_measurement_tombstone_deletes_derived_rows() -> None:
    """When a measurement record receives a tombstone, its normalized rows
    must be deleted so that latest/rolling queries exclude stale values."""
    pool = FakePool()
    repository = repository_for(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    # Initial sync to populate records
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

    assert len(pool.health_normalized_measurements) == 6
    assert len(pool.health_source_records) == 2

    # Find the source record that will be tombstoned (grpid:9001002)
    page2_source = next(
        row for row in pool.health_source_records.values()
        if row["external_id"] == "grpid:9001002"
    )
    page2_record_id = page2_source["id"]

    # Sync tombstones
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

    # Source record still exists (soft-deleted for audit)
    assert len(pool.health_source_records) == 2
    tombstoned = pool.health_source_records[page2_record_id]
    assert tombstoned["is_deleted"] is True

    # Normalized rows for the tombstoned record must be deleted
    page2_normalized = [
        r for r in pool.health_normalized_measurements.values()
        if r["source_record_id"] == page2_record_id
    ]
    assert len(page2_normalized) == 0

    # Page 1 (grpid:9001001) normalized rows still intact
    page1_source = next(
        row for row in pool.health_source_records.values()
        if row["external_id"] == "grpid:9001001"
    )
    page1_rows = [
        r for r in pool.health_normalized_measurements.values()
        if r["source_record_id"] == page1_source["id"]
    ]
    assert len(page1_rows) == 3
    assert page1_source["is_deleted"] is False


async def test_normalized_rows_not_produced_for_workout_or_sleep() -> None:
    """Only measurement records should produce normalized_measurements rows;
    workout and sleep syncs must not create measurement derived rows."""
    pool = FakePool()
    repository = repository_for(pool)
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.WORKOUT,
        now=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
    )
    await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.SLEEP,
        now=datetime(2026, 7, 20, 7, 35, tzinfo=UTC),
    )

    # No normalized measurement rows from workout or sleep syncs
    assert len(pool.health_normalized_measurements) == 0
    # Workout and sleep source records exist
    assert len(pool.health_source_records) == 3  # 1 workout + 2 sleep


async def test_cursor_advances_after_derived_writes_in_transaction() -> None:
    """The sync cursor must advance only after both source and derived
    writes succeed within the same transaction."""
    pool = FakePool()
    repository = repository_for(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)

    outcome = await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
    )

    # Outcome must report success
    assert outcome.status.value == "completed"
    # Cursor after must be set (page 2's last_modified)
    assert outcome.cursor_after is not None
    # Normalized rows exist (derived writes succeeded before cursor advancement)
    assert len(pool.health_normalized_measurements) == 6
    # Source records exist
    assert len(pool.health_source_records) == 2
