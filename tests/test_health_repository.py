from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.health_sync.models import (
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    HealthSyncCursor,
    HealthTombstone,
)
from app.services.health_sync.repository import HealthSyncRepository
from tests.conftest import FakePool


@pytest.mark.asyncio
async def test_cursor_storage_round_trip() -> None:
    pool = FakePool()
    connection_id = pool.seed_health_connection(user_id=uuid4())
    repository = HealthSyncRepository(pool)
    cursor = HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        page_offset=75,
        etag="etag-1",
    )

    await repository.store_cursor(connection_id=connection_id, cursor=cursor)
    loaded = await repository.load_cursor(
        connection_id=connection_id,
        resource_type=HealthResourceType.MEASUREMENT,
    )

    assert loaded is not None
    assert loaded.to_state() == cursor.to_state()


@pytest.mark.asyncio
async def test_claim_dirty_categories_is_limited_and_can_reclaim_stale_rows() -> None:
    pool = FakePool()
    repository = HealthSyncRepository(pool)
    first_connection_id = pool.seed_health_connection(user_id=uuid4(), external_user_id="420001")
    second_connection_id = pool.seed_health_connection(user_id=uuid4(), external_user_id="420002")
    first_user_id = pool.health_connections[first_connection_id]["user_id"]
    second_user_id = pool.health_connections[second_connection_id]["user_id"]

    await repository.mark_dirty(
        connection_id=first_connection_id,
        user_id=first_user_id,
        provider=HealthProviderSlug.WITHINGS,
        resource_type=HealthResourceType.MEASUREMENT,
        marked_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )
    await repository.mark_dirty(
        connection_id=second_connection_id,
        user_id=second_user_id,
        provider=HealthProviderSlug.WITHINGS,
        resource_type=HealthResourceType.SLEEP,
        marked_at=datetime(2026, 7, 20, 12, 5, tzinfo=UTC),
    )

    first_claim = await repository.claim_dirty_categories(
        claimed_by="worker-a",
        limit=1,
        now=datetime(2026, 7, 20, 12, 10, tzinfo=UTC),
    )
    assert [item.connection_id for item in first_claim] == [first_connection_id]
    assert first_claim[0].attempts == 1

    second_claim = await repository.claim_dirty_categories(
        claimed_by="worker-b",
        limit=1,
        now=datetime(2026, 7, 20, 12, 11, tzinfo=UTC),
    )
    assert [item.connection_id for item in second_claim] == [second_connection_id]

    pool.health_dirty_categories[first_claim[0].dirty_id]["claimed_at"] = datetime(
        2026, 7, 20, 11, 30, tzinfo=UTC
    )
    reclaimed = await repository.claim_dirty_categories(
        claimed_by="worker-c",
        limit=1,
        now=datetime(2026, 7, 20, 12, 45, tzinfo=UTC),
        stale_after=timedelta(minutes=15),
    )
    assert [item.dirty_id for item in reclaimed] == [first_claim[0].dirty_id]
    assert reclaimed[0].attempts == 2
    assert reclaimed[0].claimed_by == "worker-c"


@pytest.mark.asyncio
async def test_source_record_upsert_and_tombstone_remain_scoped_by_connection() -> None:
    pool = FakePool()
    repository = HealthSyncRepository(pool)
    user_id = uuid4()
    first_connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
    second_connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420002")

    first = await repository.upsert_source_record(
        connection_id=first_connection_id,
        user_id=user_id,
        record=HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.MEASUREMENT,
            external_id="measure-1",
            source_modified_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
            payload_hash="hash-v1",
        ),
        now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
    )
    second = await repository.upsert_source_record(
        connection_id=second_connection_id,
        user_id=user_id,
        record=HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.MEASUREMENT,
            external_id="measure-1",
            source_modified_at=datetime(2026, 7, 20, 12, 2, tzinfo=UTC),
            payload_hash="hash-v2",
        ),
        now=datetime(2026, 7, 20, 12, 2, tzinfo=UTC),
    )
    tombstoned = await repository.tombstone_source_record(
        connection_id=first_connection_id,
        user_id=user_id,
        tombstone=HealthTombstone(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.MEASUREMENT,
            external_id="measure-1",
            deleted_at=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
        ),
        now=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
    )

    assert first.record_id != second.record_id
    assert second.is_deleted is False
    assert tombstoned.is_deleted is True
    assert tombstoned.revision_count == 2
    assert pool.health_source_records[second.record_id]["is_deleted"] is False


@pytest.mark.asyncio
async def test_repository_transaction_rolls_back_health_mutations() -> None:
    pool = FakePool()
    repository = HealthSyncRepository(pool)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
    cursor = HealthSyncCursor(
        resource_type=HealthResourceType.WORKOUT,
        last_modified=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
    )

    with pytest.raises(RuntimeError):
        async with repository.transaction() as connection:
            await repository.store_cursor(
                connection_id=connection_id,
                cursor=cursor,
                executor=connection,
            )
            await repository.upsert_source_record(
                connection_id=connection_id,
                user_id=user_id,
                record=HealthSourceRecord(
                    provider=HealthProviderSlug.WITHINGS,
                    resource_type=HealthResourceType.WORKOUT,
                    external_id="workout-1",
                    source_modified_at=datetime(2026, 7, 20, 12, 30, tzinfo=UTC),
                    payload_hash="hash-v1",
                ),
                now=datetime(2026, 7, 20, 12, 31, tzinfo=UTC),
                executor=connection,
            )
            raise RuntimeError("force rollback")

    assert pool.health_source_records == {}
    assert pool.health_dirty_categories == {}
    loaded = await repository.load_cursor(
        connection_id=connection_id,
        resource_type=HealthResourceType.WORKOUT,
    )
    assert loaded is None
