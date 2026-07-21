from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.services.health_sync import (
    FakeWithingsProvider,
    HealthResourceType,
    HealthSyncCursor,
    HealthSyncCursorError,
    HealthSyncErrorKind,
    repository_for,
    sync_connection_resource,
    sync_dirty_categories,
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


async def test_sync_dirty_categories_claims_measurement_work_and_advances_cursor() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    await repository.mark_dirty(
        connection_id=connection_id,
        user_id=user_id,
        provider=provider.capabilities.provider,
        resource_type=HealthResourceType.MEASUREMENT,
        marked_at=datetime(2026, 7, 20, 7, 0, tzinfo=UTC),
    )

    sync_at = datetime(2026, 7, 20, 7, 30, tzinfo=UTC)
    outcomes = await sync_dirty_categories(
        repository=repository,
        provider=provider,
        claimed_by="worker-health-a",
        limit=1,
        access_token_loader=lambda _: access_token,
        now=sync_at,
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.page_count == 2
    assert outcome.fetched_count == 2
    assert outcome.cursor_before is None
    assert outcome.cursor_after == HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime.fromtimestamp(1784510220, tz=UTC),
    )
    assert len(pool.health_source_records) == 2
    assert sorted(row["external_id"] for row in pool.health_source_records.values()) == [
        "grpid:9001001",
        "grpid:9001002",
    ]
    cleared = next(iter(pool.health_dirty_categories.values()))
    assert cleared["claimed_by"] == "worker-health-a"
    assert cleared["cleared_at"] == sync_at
    stored_cursor = await repository.load_cursor(
        connection_id=connection_id,
        resource_type=HealthResourceType.MEASUREMENT,
    )
    assert stored_cursor == outcome.cursor_after
    assert stored_cursor is not None
    assert stored_cursor.page_offset is None


async def test_sync_applies_48_hour_overlap_without_creating_duplicate_source_rows() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    first_sync = await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 7, 30, tzinfo=UTC),
    )
    second_sync = await sync_connection_resource(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
    )

    assert first_sync.cursor_after == HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime.fromtimestamp(1784510220, tz=UTC),
    )
    assert second_sync.cursor_before == first_sync.cursor_after
    assert second_sync.cursor_after == first_sync.cursor_after
    assert second_sync.page_count == 2
    assert len(pool.health_source_records) == 2
    assert {row["revision_count"] for row in pool.health_source_records.values()} == {1}
    assert len(pool.health_source_records_by_key) == len(pool.health_source_records)


async def test_sync_rejects_malformed_persisted_cursor_without_fetching_provider() -> None:
    class TrackingProvider(FakeWithingsProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fetch_call_count = 0

        async def fetch_changes(self, **kwargs):  # type: ignore[override]
            self.fetch_call_count += 1
            return await super().fetch_changes(**kwargs)

    pool = FakePool()
    repository = repository_for(pool)
    provider = TrackingProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(
        user_id=user_id,
        external_user_id="420001",
        cursor_state={
            "measurement": {
                "resource_type": "measurement",
                "last_modified": "2026-07-20T07:00:00Z",
                "page_offset": "not-an-int",
            }
        },
    )

    with pytest.raises(HealthSyncCursorError) as excinfo:
        await sync_connection_resource(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
        )

    assert excinfo.value.error.kind is HealthSyncErrorKind.INVALID_CURSOR_STATE
    assert provider.fetch_call_count == 0
    assert pool.health_source_records == {}


async def test_sync_rollback_leaves_cursor_unchanged_when_upsert_fails() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    existing_cursor = HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime.fromtimestamp(1784510220, tz=UTC),
    )
    connection_id = pool.seed_health_connection(
        user_id=user_id,
        external_user_id="420001",
        cursor_state={"measurement": existing_cursor.to_state()},
    )

    real_upsert = repository.upsert_source_record
    call_count = 0

    async def fail_second_upsert(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("simulated source-record failure")
        return await real_upsert(**kwargs)

    repository.upsert_source_record = fail_second_upsert  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="simulated source-record failure"):
        await sync_connection_resource(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 8, 0, tzinfo=UTC),
        )

    assert pool.health_source_records == {}
    loaded_cursor = await repository.load_cursor(
        connection_id=connection_id,
        resource_type=HealthResourceType.MEASUREMENT,
    )
    assert loaded_cursor == existing_cursor
