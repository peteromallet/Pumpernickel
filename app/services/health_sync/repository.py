"""Repository helpers for durable health-sync state."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from uuid import UUID

from app.services.health_sync.models import (
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    HealthSyncCursor,
    HealthSyncError,
    HealthTombstone,
    NormalizedMeasurement,
)

_LOAD_CONNECTION_BY_PROVIDER_USER_SQL = """
    SELECT id, user_id, provider, external_user_id, status, granted_scopes,
           cursor_state, updated_at
    FROM mediator.health_connections
    WHERE provider = $1
      AND external_user_id = $2
      AND deleted_at IS NULL
    LIMIT 1
"""

_LOAD_CURSOR_STATE_SQL = """
    SELECT cursor_state, updated_at
    FROM mediator.health_connections
    WHERE id = $1
      AND deleted_at IS NULL
"""

_LOAD_CONNECTION_SQL = """
    SELECT id, user_id, provider, external_user_id, status, granted_scopes,
           cursor_state, updated_at
    FROM mediator.health_connections
    WHERE id = $1
      AND deleted_at IS NULL
    LIMIT 1
"""

_LIST_CONNECTIONS_SQL = """
    SELECT id, user_id, provider, external_user_id, status, granted_scopes,
           cursor_state, updated_at
    FROM mediator.health_connections
    WHERE provider = $1
      AND deleted_at IS NULL
    ORDER BY updated_at ASC, id ASC
    LIMIT $2
"""

_STORE_CURSOR_STATE_SQL = """
    UPDATE mediator.health_connections
    SET cursor_state = $2,
        updated_at = $3
    WHERE id = $1
      AND deleted_at IS NULL
    RETURNING cursor_state, updated_at
"""

_SELECT_WEBHOOK_RECEIPT_SQL = """
    SELECT id, connection_id, user_id, provider, provider_user_id, resource_type,
           payload_hash, content_type, status, error_code, note, received_at, processed_at
    FROM mediator.health_webhook_receipts
    WHERE provider = $1
      AND payload_hash = $2
    LIMIT 1
"""

_INSERT_WEBHOOK_RECEIPT_SQL = """
    INSERT INTO mediator.health_webhook_receipts (
        connection_id,
        user_id,
        provider,
        provider_user_id,
        resource_type,
        payload_hash,
        content_type,
        status,
        note,
        received_at,
        processed_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    ON CONFLICT (provider, payload_hash) DO NOTHING
    RETURNING id, connection_id, user_id, provider, provider_user_id, resource_type,
              payload_hash, content_type, status, error_code, note, received_at, processed_at
"""

_MARK_DIRTY_SQL = """
    INSERT INTO mediator.health_dirty_categories (
        connection_id,
        user_id,
        provider,
        resource_type,
        reason,
        source_receipt_id,
        marked_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7)
    ON CONFLICT (connection_id, resource_type) WHERE cleared_at IS NULL
    DO UPDATE
    SET reason = EXCLUDED.reason,
        source_receipt_id = EXCLUDED.source_receipt_id,
        marked_at = GREATEST(
            mediator.health_dirty_categories.marked_at,
            EXCLUDED.marked_at
        ),
        claimed_at = NULL,
        claimed_by = NULL
    RETURNING id, connection_id, user_id, provider, resource_type, reason,
              source_receipt_id, attempts, marked_at, claimed_at, claimed_by, cleared_at
"""

_CLAIM_DIRTY_SQL = """
    WITH claimable_dirty AS (
        SELECT id
        FROM mediator.health_dirty_categories
        WHERE cleared_at IS NULL
          AND (
            claimed_at IS NULL
            OR claimed_at < $2
          )
        ORDER BY marked_at ASC, id ASC
        LIMIT $3
    )
    UPDATE mediator.health_dirty_categories AS dirty
    SET claimed_at = $4,
        claimed_by = $1,
        attempts = dirty.attempts + 1
    FROM claimable_dirty
    WHERE dirty.id = claimable_dirty.id
    RETURNING dirty.id, dirty.connection_id, dirty.user_id, dirty.provider,
              dirty.resource_type, dirty.reason, dirty.source_receipt_id,
              dirty.attempts, dirty.marked_at, dirty.claimed_at,
              dirty.claimed_by, dirty.cleared_at
"""

_CLEAR_DIRTY_SQL = """
    UPDATE mediator.health_dirty_categories
    SET cleared_at = $2
    WHERE id = $1
    RETURNING id, connection_id, user_id, provider, resource_type, reason,
              source_receipt_id, attempts, marked_at, claimed_at,
              claimed_by, cleared_at
"""

_MARK_SYNC_SUCCESS_SQL = """
    UPDATE mediator.health_connections
    SET last_success_at = $2,
        last_error_at = NULL,
        last_error_code = NULL,
        last_error_detail = NULL,
        updated_at = $2
    WHERE id = $1
      AND deleted_at IS NULL
    RETURNING id, user_id, provider, external_user_id, status, granted_scopes,
              cursor_state, updated_at
"""

_MARK_SYNC_ERROR_SQL = """
    UPDATE mediator.health_connections
    SET last_error_at = $2,
        last_error_code = $3,
        last_error_detail = $4,
        updated_at = $2
    WHERE id = $1
      AND deleted_at IS NULL
    RETURNING id, user_id, provider, external_user_id, status, granted_scopes,
              cursor_state, updated_at
"""

_DELETE_NORMALIZED_MEASUREMENTS_SQL = """
    DELETE FROM mediator.health_normalized_measurements
    WHERE source_record_id = $1
      AND connection_id = $2
      AND user_id = $3
"""

_INSERT_NORMALIZED_MEASUREMENT_SQL = """
    INSERT INTO mediator.health_normalized_measurements (
        source_record_id,
        connection_id,
        user_id,
        metric,
        measured_at,
        value_numeric,
        canonical_unit,
        source_unit,
        source_device_id,
        source_device_model,
        attribution
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    RETURNING id
"""

_DELETE_NORMALIZED_SLEEP_SQL = """
    DELETE FROM mediator.health_normalized_sleep
    WHERE source_record_id = $1
      AND connection_id = $2
      AND user_id = $3
"""

_INSERT_NORMALIZED_SLEEP_SQL = """
    INSERT INTO mediator.health_normalized_sleep (
        source_record_id,
        connection_id,
        user_id,
        started_at,
        ended_at,
        local_sleep_date,
        local_timezone,
        local_offset_seconds,
        completeness_state,
        total_in_bed_seconds,
        total_asleep_seconds,
        awake_seconds,
        light_sleep_seconds,
        deep_sleep_seconds,
        rem_sleep_seconds,
        sleep_latency_seconds,
        wake_after_sleep_onset_seconds,
        wakeups,
        sleep_score,
        source_device_id,
        source_device_model,
        attribution
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9,
        $10, $11, $12, $13, $14, $15, $16, $17,
        $18, $19, $20, $21, $22
    )
    RETURNING id
"""

_UPSERT_SOURCE_RECORD_SQL = """
    INSERT INTO mediator.health_source_records (
        connection_id,
        user_id,
        provider,
        resource_type,
        external_id,
        source_created_at,
        source_modified_at,
        observed_at,
        starts_at,
        ends_at,
        source_timezone,
        source_offset_seconds,
        source_device_id,
        source_device_model,
        payload_hash,
        provider_revision,
        source_metadata,
        attribution,
        is_deleted,
        deleted_at,
        imported_at,
        updated_at
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
        $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22
    )
    ON CONFLICT (connection_id, resource_type, external_id)
    DO UPDATE
    SET user_id = EXCLUDED.user_id,
        provider = EXCLUDED.provider,
        source_created_at = EXCLUDED.source_created_at,
        source_modified_at = EXCLUDED.source_modified_at,
        observed_at = EXCLUDED.observed_at,
        starts_at = EXCLUDED.starts_at,
        ends_at = EXCLUDED.ends_at,
        source_timezone = EXCLUDED.source_timezone,
        source_offset_seconds = EXCLUDED.source_offset_seconds,
        source_device_id = EXCLUDED.source_device_id,
        source_device_model = EXCLUDED.source_device_model,
        payload_hash = EXCLUDED.payload_hash,
        provider_revision = EXCLUDED.provider_revision,
        revision_count = CASE
            WHEN mediator.health_source_records.payload_hash IS DISTINCT FROM EXCLUDED.payload_hash
              OR mediator.health_source_records.provider_revision IS DISTINCT FROM EXCLUDED.provider_revision
              OR mediator.health_source_records.source_modified_at IS DISTINCT FROM EXCLUDED.source_modified_at
              OR mediator.health_source_records.is_deleted IS DISTINCT FROM EXCLUDED.is_deleted
              OR mediator.health_source_records.deleted_at IS DISTINCT FROM EXCLUDED.deleted_at
            THEN mediator.health_source_records.revision_count + 1
            ELSE mediator.health_source_records.revision_count
        END,
        source_metadata = EXCLUDED.source_metadata,
        attribution = EXCLUDED.attribution,
        is_deleted = EXCLUDED.is_deleted,
        deleted_at = EXCLUDED.deleted_at,
        updated_at = EXCLUDED.updated_at
    RETURNING id, connection_id, user_id, provider, resource_type, external_id,
              source_created_at, source_modified_at, observed_at, starts_at, ends_at,
              source_timezone, source_offset_seconds, source_device_id, source_device_model,
              payload_hash, provider_revision, revision_count, source_metadata, attribution,
              is_deleted, deleted_at, imported_at, updated_at
"""


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _mapping_row(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    return dict(row)


@dataclass(frozen=True, slots=True)
class HealthConnectionRecord:
    connection_id: UUID
    user_id: UUID
    provider: HealthProviderSlug
    external_user_id: str | None
    status: str
    granted_scopes: frozenset[str]
    cursor_state: dict[str, Any]
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class HealthWebhookReceipt:
    receipt_id: UUID
    connection_id: UUID | None
    user_id: UUID | None
    provider: HealthProviderSlug
    provider_user_id: str
    resource_type: HealthResourceType
    payload_hash: str
    content_type: str | None
    status: str
    note: str | None
    received_at: datetime
    processed_at: datetime | None


@dataclass(frozen=True, slots=True)
class HealthDirtyCategory:
    dirty_id: UUID
    connection_id: UUID
    user_id: UUID
    provider: HealthProviderSlug
    resource_type: HealthResourceType
    reason: str
    source_receipt_id: UUID | None
    attempts: int
    marked_at: datetime
    claimed_at: datetime | None
    claimed_by: str | None
    cleared_at: datetime | None


@dataclass(frozen=True, slots=True)
class StoredHealthSourceRecord:
    record_id: UUID
    connection_id: UUID
    user_id: UUID
    provider: HealthProviderSlug
    resource_type: HealthResourceType
    external_id: str
    revision_count: int
    payload_hash: str | None
    provider_revision: str | None
    is_deleted: bool
    deleted_at: datetime | None
    updated_at: datetime


def _connection_from_row(row: Mapping[str, Any]) -> HealthConnectionRecord:
    return HealthConnectionRecord(
        connection_id=row["id"],
        user_id=row["user_id"],
        provider=HealthProviderSlug(row["provider"]),
        external_user_id=row.get("external_user_id"),
        status=str(row["status"]),
        granted_scopes=frozenset(row.get("granted_scopes") or ()),
        cursor_state=dict(row.get("cursor_state") or {}),
        updated_at=_normalize_datetime(row["updated_at"]) or _utc_now(),
    )


def _receipt_from_row(row: Mapping[str, Any]) -> HealthWebhookReceipt:
    return HealthWebhookReceipt(
        receipt_id=row["id"],
        connection_id=row.get("connection_id"),
        user_id=row.get("user_id"),
        provider=HealthProviderSlug(row["provider"]),
        provider_user_id=str(row["provider_user_id"]),
        resource_type=HealthResourceType(row["resource_type"]),
        payload_hash=str(row["payload_hash"]),
        content_type=row.get("content_type"),
        status=str(row["status"]),
        note=row.get("note"),
        received_at=_normalize_datetime(row["received_at"]) or _utc_now(),
        processed_at=_normalize_datetime(row.get("processed_at")),
    )


def _dirty_from_row(row: Mapping[str, Any]) -> HealthDirtyCategory:
    return HealthDirtyCategory(
        dirty_id=row["id"],
        connection_id=row["connection_id"],
        user_id=row["user_id"],
        provider=HealthProviderSlug(row["provider"]),
        resource_type=HealthResourceType(row["resource_type"]),
        reason=str(row["reason"]),
        source_receipt_id=row.get("source_receipt_id"),
        attempts=int(row["attempts"]),
        marked_at=_normalize_datetime(row["marked_at"]) or _utc_now(),
        claimed_at=_normalize_datetime(row.get("claimed_at")),
        claimed_by=row.get("claimed_by"),
        cleared_at=_normalize_datetime(row.get("cleared_at")),
    )


def _stored_source_record_from_row(row: Mapping[str, Any]) -> StoredHealthSourceRecord:
    return StoredHealthSourceRecord(
        record_id=row["id"],
        connection_id=row["connection_id"],
        user_id=row["user_id"],
        provider=HealthProviderSlug(row["provider"]),
        resource_type=HealthResourceType(row["resource_type"]),
        external_id=str(row["external_id"]),
        revision_count=int(row["revision_count"]),
        payload_hash=row.get("payload_hash"),
        provider_revision=row.get("provider_revision"),
        is_deleted=bool(row["is_deleted"]),
        deleted_at=_normalize_datetime(row.get("deleted_at")),
        updated_at=_normalize_datetime(row["updated_at"]) or _utc_now(),
    )


class HealthSyncRepository:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[Any]:
        async with self._pool.acquire() as connection:
            async with connection.transaction():
                yield connection

    async def get_connection_by_provider_user_id(
        self,
        *,
        provider: HealthProviderSlug | str,
        provider_user_id: str,
        executor: Any | None = None,
    ) -> HealthConnectionRecord | None:
        row = await self._executor(executor).fetchrow(
            _LOAD_CONNECTION_BY_PROVIDER_USER_SQL,
            HealthProviderSlug(provider).value,
            provider_user_id.strip(),
        )
        if row is None:
            return None
        return _connection_from_row(_mapping_row(row))

    async def load_connection(
        self,
        *,
        connection_id: UUID,
        executor: Any | None = None,
    ) -> HealthConnectionRecord | None:
        row = await self._executor(executor).fetchrow(
            _LOAD_CONNECTION_SQL,
            connection_id,
        )
        if row is None:
            return None
        return _connection_from_row(_mapping_row(row))

    async def list_connections(
        self,
        *,
        provider: HealthProviderSlug | str,
        limit: int,
        executor: Any | None = None,
    ) -> tuple[HealthConnectionRecord, ...]:
        rows = await self._executor(executor).fetch(
            _LIST_CONNECTIONS_SQL,
            HealthProviderSlug(provider).value,
            max(0, int(limit)),
        )
        return tuple(_connection_from_row(_mapping_row(row)) for row in rows)

    async def load_cursor(
        self,
        *,
        connection_id: UUID,
        resource_type: HealthResourceType | str,
        executor: Any | None = None,
    ) -> HealthSyncCursor | None:
        row = await self._executor(executor).fetchrow(
            _LOAD_CURSOR_STATE_SQL,
            connection_id,
        )
        if row is None:
            return None
        cursor_state = dict(_mapping_row(row).get("cursor_state") or {})
        payload = cursor_state.get(HealthResourceType(resource_type).value)
        if not isinstance(payload, Mapping):
            return None
        return HealthSyncCursor.from_state(payload)

    async def store_cursor(
        self,
        *,
        connection_id: UUID,
        cursor: HealthSyncCursor,
        now: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthSyncCursor:
        store = self._executor(executor)
        loaded = await store.fetchrow(_LOAD_CURSOR_STATE_SQL, connection_id)
        if loaded is None:
            raise LookupError("health connection not found")
        cursor_state = dict(_mapping_row(loaded).get("cursor_state") or {})
        cursor_state[cursor.resource_type.value] = cursor.to_state()
        updated = await store.fetchrow(
            _STORE_CURSOR_STATE_SQL,
            connection_id,
            cursor_state,
            _normalize_datetime(now or _utc_now()),
        )
        if updated is None:
            raise LookupError("health connection not found")
        return cursor

    async def record_webhook_receipt(
        self,
        *,
        provider: HealthProviderSlug | str,
        provider_user_id: str,
        resource_type: HealthResourceType | str,
        payload_hash: str,
        content_type: str | None,
        status: str,
        note: str | None = None,
        connection_id: UUID | None = None,
        user_id: UUID | None = None,
        received_at: datetime | None = None,
        processed_at: datetime | None = None,
        executor: Any | None = None,
    ) -> tuple[HealthWebhookReceipt, bool]:
        store = self._executor(executor)
        inserted = await store.fetchrow(
            _INSERT_WEBHOOK_RECEIPT_SQL,
            connection_id,
            user_id,
            HealthProviderSlug(provider).value,
            provider_user_id.strip(),
            HealthResourceType(resource_type).value,
            payload_hash.strip(),
            content_type,
            status.strip(),
            note,
            _normalize_datetime(received_at or _utc_now()),
            _normalize_datetime(processed_at),
        )
        if inserted is not None:
            return _receipt_from_row(_mapping_row(inserted)), True
        existing = await store.fetchrow(
            _SELECT_WEBHOOK_RECEIPT_SQL,
            HealthProviderSlug(provider).value,
            payload_hash.strip(),
        )
        if existing is None:
            raise RuntimeError("health webhook receipt insert lost without persisted row")
        return _receipt_from_row(_mapping_row(existing)), False

    async def mark_dirty(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        provider: HealthProviderSlug | str,
        resource_type: HealthResourceType | str,
        reason: str = "webhook",
        source_receipt_id: UUID | None = None,
        marked_at: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthDirtyCategory:
        row = await self._executor(executor).fetchrow(
            _MARK_DIRTY_SQL,
            connection_id,
            user_id,
            HealthProviderSlug(provider).value,
            HealthResourceType(resource_type).value,
            reason.strip(),
            source_receipt_id,
            _normalize_datetime(marked_at or _utc_now()),
        )
        return _dirty_from_row(_mapping_row(row))

    async def claim_dirty_categories(
        self,
        *,
        claimed_by: str,
        limit: int,
        now: datetime | None = None,
        stale_after: timedelta | None = None,
    ) -> list[HealthDirtyCategory]:
        claim_at = _normalize_datetime(now or _utc_now()) or _utc_now()
        stale_before = claim_at - (stale_after or timedelta(minutes=15))
        async with self.transaction() as connection:
            rows = await connection.fetch(
                _CLAIM_DIRTY_SQL,
                claimed_by.strip(),
                stale_before,
                max(0, int(limit)),
                claim_at,
        )
        return [_dirty_from_row(_mapping_row(row)) for row in rows]

    async def clear_dirty_category(
        self,
        *,
        dirty_id: UUID,
        cleared_at: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthDirtyCategory:
        row = await self._executor(executor).fetchrow(
            _CLEAR_DIRTY_SQL,
            dirty_id,
            _normalize_datetime(cleared_at or _utc_now()),
        )
        if row is None:
            raise LookupError("health dirty category not found")
        return _dirty_from_row(_mapping_row(row))

    async def record_sync_success(
        self,
        *,
        connection_id: UUID,
        synced_at: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthConnectionRecord:
        row = await self._executor(executor).fetchrow(
            _MARK_SYNC_SUCCESS_SQL,
            connection_id,
            _normalize_datetime(synced_at or _utc_now()),
        )
        if row is None:
            raise LookupError("health connection not found")
        return _connection_from_row(_mapping_row(row))

    async def record_sync_error(
        self,
        *,
        connection_id: UUID,
        error: HealthSyncError,
        errored_at: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthConnectionRecord:
        row = await self._executor(executor).fetchrow(
            _MARK_SYNC_ERROR_SQL,
            connection_id,
            _normalize_datetime(errored_at or _utc_now()),
            error.code,
            error.detail,
        )
        if row is None:
            raise LookupError("health connection not found")
        return _connection_from_row(_mapping_row(row))

    async def upsert_source_record(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        record: HealthSourceRecord,
        now: datetime | None = None,
        executor: Any | None = None,
    ) -> StoredHealthSourceRecord:
        timestamp = _normalize_datetime(now or _utc_now())
        row = await self._executor(executor).fetchrow(
            _UPSERT_SOURCE_RECORD_SQL,
            connection_id,
            user_id,
            record.provider.value,
            record.resource_type.value,
            record.external_id,
            _normalize_datetime(record.source_created_at),
            _normalize_datetime(record.source_modified_at),
            _normalize_datetime(record.observed_at),
            _normalize_datetime(record.starts_at),
            _normalize_datetime(record.ends_at),
            record.source_timezone,
            record.source_offset_seconds,
            record.source_device_id,
            record.source_device_model,
            record.payload_hash,
            record.provider_revision,
            dict(record.source_metadata),
            dict(record.attribution),
            record.is_deleted,
            _normalize_datetime(record.deleted_at),
            timestamp,
            timestamp,
        )
        return _stored_source_record_from_row(_mapping_row(row))

    async def tombstone_source_record(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        tombstone: HealthTombstone,
        now: datetime | None = None,
        executor: Any | None = None,
    ) -> StoredHealthSourceRecord:
        return await self.upsert_source_record(
            connection_id=connection_id,
            user_id=user_id,
            record=HealthSourceRecord(
                provider=tombstone.provider,
                resource_type=tombstone.resource_type,
                external_id=tombstone.external_id,
                source_modified_at=tombstone.deleted_at,
                observed_at=tombstone.deleted_at,
                payload_hash=None,
                provider_revision=tombstone.provider_revision,
                source_metadata={"tombstone_reason": tombstone.reason},
                is_deleted=True,
                deleted_at=tombstone.deleted_at,
            ),
            now=now,
            executor=executor,
        )

    # ------------------------------------------------------------------
    # Normalized measurement helpers
    # ------------------------------------------------------------------

    async def replace_normalized_measurements(
        self,
        *,
        source_record_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        measurements: list[NormalizedMeasurement],
        executor: Any | None = None,
    ) -> list[UUID]:
        """Delete existing normalized rows then insert new ones.

        Must be called inside a transaction so the delete + insert are
        atomic with respect to the source-record upsert.
        """
        store = self._executor(executor)
        await store.execute(
            _DELETE_NORMALIZED_MEASUREMENTS_SQL,
            source_record_id,
            connection_id,
            user_id,
        )
        row_ids: list[UUID] = []
        for m in measurements:
            row = await store.fetchrow(
                _INSERT_NORMALIZED_MEASUREMENT_SQL,
                source_record_id,
                connection_id,
                user_id,
                m.metric,
                m.measured_at,
                m.value_numeric,
                m.canonical_unit,
                m.canonical_unit,  # source_unit mirrors canonical_unit
                m.source_device_id,
                m.source_device_model,
                dict(m.attribution),
            )
            row_ids.append(row["id"])
        return row_ids

    async def delete_normalized_measurements(
        self,
        *,
        source_record_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        """Remove all normalized measurement rows for a source record."""
        await self._executor(executor).execute(
            _DELETE_NORMALIZED_MEASUREMENTS_SQL,
            source_record_id,
            connection_id,
            user_id,
        )

    # ------------------------------------------------------------------
    # Normalized sleep helpers
    # ------------------------------------------------------------------

    async def replace_normalized_sleep(
        self,
        *,
        source_record_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        started_at: datetime,
        ended_at: datetime,
        local_sleep_date: Any,  # date
        local_timezone: str | None = None,
        local_offset_seconds: int | None = None,
        completeness_state: str = "partial",
        total_in_bed_seconds: int | None = None,
        total_asleep_seconds: int | None = None,
        awake_seconds: int | None = None,
        light_sleep_seconds: int | None = None,
        deep_sleep_seconds: int | None = None,
        rem_sleep_seconds: int | None = None,
        sleep_latency_seconds: int | None = None,
        wake_after_sleep_onset_seconds: int | None = None,
        wakeups: int | None = None,
        sleep_score: int | None = None,
        source_device_id: str | None = None,
        source_device_model: str | None = None,
        attribution: dict[str, Any] | None = None,
        executor: Any | None = None,
    ) -> UUID:
        """Delete existing normalized sleep row then insert a new one.

        Must be called inside a transaction for atomicity.
        """
        store = self._executor(executor)
        await store.execute(
            _DELETE_NORMALIZED_SLEEP_SQL,
            source_record_id,
            connection_id,
            user_id,
        )
        row = await store.fetchrow(
            _INSERT_NORMALIZED_SLEEP_SQL,
            source_record_id,
            connection_id,
            user_id,
            _normalize_datetime(started_at),
            _normalize_datetime(ended_at),
            local_sleep_date,
            local_timezone,
            local_offset_seconds,
            completeness_state,
            total_in_bed_seconds,
            total_asleep_seconds,
            awake_seconds,
            light_sleep_seconds,
            deep_sleep_seconds,
            rem_sleep_seconds,
            sleep_latency_seconds,
            wake_after_sleep_onset_seconds,
            wakeups,
            sleep_score,
            source_device_id,
            source_device_model,
            dict(attribution or {}),
        )
        return row["id"]

    async def delete_normalized_sleep(
        self,
        *,
        source_record_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        """Remove all normalized sleep rows for a source record."""
        await self._executor(executor).execute(
            _DELETE_NORMALIZED_SLEEP_SQL,
            source_record_id,
            connection_id,
            user_id,
        )

    def _executor(self, executor: Any | None) -> Any:
        return executor if executor is not None else self._pool


def repository_for(pool: Any) -> HealthSyncRepository:
    return HealthSyncRepository(pool)


__all__ = [
    "HealthConnectionRecord",
    "HealthDirtyCategory",
    "HealthSyncRepository",
    "HealthWebhookReceipt",
    "StoredHealthSourceRecord",
    "repository_for",
]
