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
    NormalizedWorkout,
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

_DELETE_NORMALIZED_WORKOUT_SQL = """
    DELETE FROM mediator.health_normalized_workouts
    WHERE source_record_id = $1
      AND connection_id = $2
      AND user_id = $3
"""

# ── Projection ledger SQL ───────────────────────────────────────────────────

_SELECT_ACTIVE_PROJECTION_SQL = """
    SELECT id, source_record_id, connection_id, user_id,
           event_id, commitment_id, projection_version,
           projection_status, match_rule, note,
           decision_reason, matched_local_date,
           supersedes_projection_id,
           projected_at, removed_at, created_at, updated_at
    FROM mediator.health_source_to_event_projections
    WHERE source_record_id = $1
      AND user_id = $2
      AND projection_status IN ('pending', 'projected')
    ORDER BY projection_version DESC
    LIMIT 1
"""

_SELECT_ACTIVE_PROJECTION_FOR_UPDATE_SQL = """
    SELECT id, source_record_id, connection_id, user_id,
           event_id, commitment_id, projection_version,
           projection_status, match_rule, note,
           decision_reason, matched_local_date,
           supersedes_projection_id,
           projected_at, removed_at, created_at, updated_at
    FROM mediator.health_source_to_event_projections
    WHERE source_record_id = $1
      AND user_id = $2
      AND projection_status IN ('pending', 'projected')
    ORDER BY projection_version DESC
    LIMIT 1
    FOR UPDATE
"""

_SELECT_PROJECTION_BY_EVENT_SQL = """
    SELECT id, source_record_id, connection_id, user_id,
           event_id, commitment_id, projection_version,
           projection_status, match_rule, note,
           decision_reason, matched_local_date,
           supersedes_projection_id,
           projected_at, removed_at, created_at, updated_at
    FROM mediator.health_source_to_event_projections
    WHERE event_id = $1
    LIMIT 1
"""

_INSERT_PROJECTION_SQL = """
    INSERT INTO mediator.health_source_to_event_projections (
        source_record_id,
        connection_id,
        user_id,
        event_id,
        commitment_id,
        projection_version,
        projection_status,
        match_rule,
        note,
        decision_reason,
        matched_local_date,
        supersedes_projection_id,
        projected_at
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
    RETURNING id, source_record_id, connection_id, user_id,
              event_id, commitment_id, projection_version,
              projection_status, match_rule, note,
              decision_reason, matched_local_date,
              supersedes_projection_id,
              projected_at, removed_at, created_at, updated_at
"""

_SUPERSEDE_PROJECTION_SQL = """
    UPDATE mediator.health_source_to_event_projections
    SET projection_status = 'superseded',
        removed_at = $2,
        updated_at = $2
    WHERE id = $1
      AND user_id = $3
    RETURNING id, source_record_id, connection_id, user_id,
              event_id, commitment_id, projection_version,
              projection_status, match_rule, note,
              decision_reason, matched_local_date,
              supersedes_projection_id,
              projected_at, removed_at, created_at, updated_at
"""

_REMOVE_PROJECTION_SQL = """
    UPDATE mediator.health_source_to_event_projections
    SET projection_status = 'removed',
        event_id = NULL,
        removed_at = $2,
        updated_at = $2
    WHERE id = $1
      AND user_id = $3
    RETURNING id, source_record_id, connection_id, user_id,
              event_id, commitment_id, projection_version,
              projection_status, match_rule, note,
              decision_reason, matched_local_date,
              supersedes_projection_id,
              projected_at, removed_at, created_at, updated_at
"""

_DETACH_PROJECTION_EVENT_SQL = """
    UPDATE mediator.health_source_to_event_projections
    SET event_id = NULL,
        updated_at = $2
    WHERE id = $1
      AND user_id = $3
    RETURNING id
"""

_INSERT_PROJECTION_EVENT_SQL = """
    INSERT INTO mediator.events (
        commitment_id,
        user_id,
        topic_id,
        bot_id,
        metric_key,
        adherence_status,
        value_numeric,
        value_text,
        unit,
        observed_at,
        note,
        source_message_ids
    )
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
    RETURNING id, commitment_id, metric_key, adherence_status, observed_at
"""

_DELETE_PROJECTION_EVENT_SQL = """
    DELETE FROM mediator.events
    WHERE id = $1
      AND user_id = $2
    RETURNING id
"""

# ── Connection-scoped local delete primitives ──────────────────────────────
# These remove ALL local Withings health data for a single user connection.
# Every statement is scoped by both ``connection_id`` and ``user_id`` so a
# bug in argument binding cannot reach another user's rows.  Projection-
# owned adherence events are removed via the ledger subquery, which leaves
# manual ``log_event`` testimony untouched.

_MARK_CONNECTION_DELETED_SQL = """
    UPDATE mediator.health_connections
    SET status = 'deleted',
        access_token_encrypted = NULL,
        refresh_token_encrypted = NULL,
        access_token_expires_at = NULL,
        refresh_token_expires_at = NULL,
        refresh_token_rotated_at = NULL,
        deleted_at = COALESCE(deleted_at, $3),
        revoked_at = COALESCE(revoked_at, $3),
        updated_at = $3
    WHERE id = $1
      AND user_id = $2
      AND deleted_at IS NULL
    RETURNING id, user_id, provider, external_user_id, status, granted_scopes,
              cursor_state, updated_at, deleted_at, revoked_at
"""

_DELETE_CONNECTION_SOURCE_RECORDS_SQL = """
    DELETE FROM mediator.health_source_records
    WHERE connection_id = $1
      AND user_id = $2
"""

_DELETE_CONNECTION_NORMALIZED_MEASUREMENTS_SQL = """
    DELETE FROM mediator.health_normalized_measurements
    WHERE connection_id = $1
      AND user_id = $2
"""

_DELETE_CONNECTION_NORMALIZED_SLEEP_SQL = """
    DELETE FROM mediator.health_normalized_sleep
    WHERE connection_id = $1
      AND user_id = $2
"""

_DELETE_CONNECTION_NORMALIZED_WORKOUTS_SQL = """
    DELETE FROM mediator.health_normalized_workouts
    WHERE connection_id = $1
      AND user_id = $2
"""

_DELETE_CONNECTION_DIRTY_CATEGORIES_SQL = """
    DELETE FROM mediator.health_dirty_categories
    WHERE connection_id = $1
      AND user_id = $2
"""

_DELETE_CONNECTION_WEBHOOK_RECEIPTS_SQL = """
    DELETE FROM mediator.health_webhook_receipts
    WHERE connection_id = $1
      AND user_id = $2
"""

_DELETE_CONNECTION_PROJECTION_OWNED_EVENTS_SQL = """
    DELETE FROM mediator.events
    WHERE user_id = $2
      AND id IN (
          SELECT event_id
          FROM mediator.health_source_to_event_projections
          WHERE connection_id = $1
            AND user_id = $2
            AND event_id IS NOT NULL
      )
"""

_DELETE_CONNECTION_PROJECTION_LEDGER_SQL = """
    DELETE FROM mediator.health_source_to_event_projections
    WHERE connection_id = $1
      AND user_id = $2
"""

_INSERT_NORMALIZED_WORKOUT_SQL = """
    INSERT INTO mediator.health_normalized_workouts (
        source_record_id,
        connection_id,
        user_id,
        started_at,
        ended_at,
        local_timezone,
        local_offset_seconds,
        workout_type,
        duration_seconds,
        pause_duration_seconds,
        distance_meters,
        steps,
        energy_kcal,
        elevation_gain_meters,
        average_heart_rate_bpm,
        max_heart_rate_bpm,
        source_device_id,
        source_device_model,
        attribution
    )
    VALUES (
        $1, $2, $3, $4, $5, $6, $7, $8, $9,
        $10, $11, $12, $13, $14, $15, $16, $17,
        $18, $19
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


@dataclass(frozen=True, slots=True)
class HealthProjectionRecord:
    """A single row from the projection ledger.

    ``event_id`` is None when the projection has not yet created (or has
    detached) its projection-owned event.  Only projection-owned events
    — those linked through this ledger row — may be mutated by projection
    code.  Manual ``log_event`` testimony is never touched.
    """

    projection_id: UUID
    source_record_id: UUID
    connection_id: UUID
    user_id: UUID
    event_id: UUID | None
    commitment_id: UUID | None
    projection_version: int
    projection_status: str
    match_rule: str | None
    note: str | None
    decision_reason: str | None
    matched_local_date: Any  # date | None
    supersedes_projection_id: UUID | None
    projected_at: datetime | None
    removed_at: datetime | None
    created_at: datetime
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


def _projection_from_row(row: Mapping[str, Any]) -> HealthProjectionRecord:
    return HealthProjectionRecord(
        projection_id=row["id"],
        source_record_id=row["source_record_id"],
        connection_id=row["connection_id"],
        user_id=row["user_id"],
        event_id=row.get("event_id"),
        commitment_id=row.get("commitment_id"),
        projection_version=int(row["projection_version"]),
        projection_status=str(row["projection_status"]),
        match_rule=row.get("match_rule"),
        note=row.get("note"),
        decision_reason=row.get("decision_reason"),
        matched_local_date=row.get("matched_local_date"),
        supersedes_projection_id=row.get("supersedes_projection_id"),
        projected_at=_normalize_datetime(row.get("projected_at")),
        removed_at=_normalize_datetime(row.get("removed_at")),
        created_at=_normalize_datetime(row["created_at"]) or _utc_now(),
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

    # ------------------------------------------------------------------
    # Normalized workout helpers
    # ------------------------------------------------------------------

    async def replace_normalized_workout(
        self,
        *,
        source_record_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        workout: NormalizedWorkout,
        executor: Any | None = None,
    ) -> UUID:
        """Delete existing normalized workout row then insert a new one.

        Must be called inside a transaction for atomicity with the
        source-record upsert.
        """
        store = self._executor(executor)
        await store.execute(
            _DELETE_NORMALIZED_WORKOUT_SQL,
            source_record_id,
            connection_id,
            user_id,
        )
        row = await store.fetchrow(
            _INSERT_NORMALIZED_WORKOUT_SQL,
            source_record_id,
            connection_id,
            user_id,
            workout.started_at,
            workout.ended_at,
            workout.local_timezone,
            workout.local_offset_seconds,
            workout.workout_type,
            workout.duration_seconds,
            workout.pause_duration_seconds,
            workout.distance_meters,
            workout.steps,
            workout.energy_kcal,
            workout.elevation_gain_meters,
            workout.average_heart_rate_bpm,
            workout.max_heart_rate_bpm,
            workout.source_device_id,
            workout.source_device_model,
            dict(workout.attribution),
        )
        return row["id"]

    async def delete_normalized_workout(
        self,
        *,
        source_record_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        """Remove all normalized workout rows for a source record."""
        await self._executor(executor).execute(
            _DELETE_NORMALIZED_WORKOUT_SQL,
            source_record_id,
            connection_id,
            user_id,
        )

    # ------------------------------------------------------------------
    # Projection ledger primitives
    # ------------------------------------------------------------------

    async def find_active_projection(
        self,
        *,
        source_record_id: UUID,
        user_id: UUID,
        for_update: bool = False,
        executor: Any | None = None,
    ) -> HealthProjectionRecord | None:
        """Return the active projection for *source_record_id*, or None.

        When *for_update* is True the row is locked (``FOR UPDATE``)
        so callers can safely supersede or remove it inside a transaction.
        """
        sql = (
            _SELECT_ACTIVE_PROJECTION_FOR_UPDATE_SQL
            if for_update
            else _SELECT_ACTIVE_PROJECTION_SQL
        )
        row = await self._executor(executor).fetchrow(
            sql,
            source_record_id,
            user_id,
        )
        if row is None:
            return None
        return _projection_from_row(_mapping_row(row))

    async def find_projection_by_event(
        self,
        *,
        event_id: UUID,
        executor: Any | None = None,
    ) -> HealthProjectionRecord | None:
        """Return the projection that owns *event_id*, or None.

        If a projection row is returned then *event_id* is
        projection-owned and may be safely mutated by projection code.
        If None is returned then the event is manual testimony and
        must never be touched by projection code.
        """
        row = await self._executor(executor).fetchrow(
            _SELECT_PROJECTION_BY_EVENT_SQL,
            event_id,
        )
        if row is None:
            return None
        return _projection_from_row(_mapping_row(row))

    async def insert_projection(
        self,
        *,
        source_record_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        event_id: UUID | None = None,
        commitment_id: UUID | None = None,
        projection_version: int = 1,
        projection_status: str = "pending",
        match_rule: str | None = None,
        note: str | None = None,
        decision_reason: str | None = None,
        matched_local_date: Any = None,
        supersedes_projection_id: UUID | None = None,
        projected_at: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthProjectionRecord:
        """Insert a new projection row into the ledger.

        Must be called inside a transaction.  The caller is responsible
        for enforcing the at-most-one-active constraint before calling
        this method.
        """
        row = await self._executor(executor).fetchrow(
            _INSERT_PROJECTION_SQL,
            source_record_id,
            connection_id,
            user_id,
            event_id,
            commitment_id,
            projection_version,
            projection_status,
            match_rule,
            note,
            decision_reason,
            matched_local_date,
            supersedes_projection_id,
            _normalize_datetime(projected_at or _utc_now()),
        )
        return _projection_from_row(_mapping_row(row))

    async def supersede_projection(
        self,
        *,
        existing_projection_id: UUID,
        user_id: UUID,
        now: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthProjectionRecord:
        """Mark an existing projection as superseded.

        Sets ``projection_status = 'superseded'`` and stamps
        ``removed_at``.  Returns the updated row.  The caller must
        insert the new version in the same transaction.

        Does NOT detach or delete the projection-owned event — the
        new version may re-use the same ``event_id``.
        """
        row = await self._executor(executor).fetchrow(
            _SUPERSEDE_PROJECTION_SQL,
            existing_projection_id,
            _normalize_datetime(now or _utc_now()),
            user_id,
        )
        if row is None:
            raise LookupError("projection not found or not owned by user")
        return _projection_from_row(_mapping_row(row))

    async def remove_projection(
        self,
        *,
        projection_id: UUID,
        user_id: UUID,
        now: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthProjectionRecord:
        """Mark a projection as removed and detach its event.

        Sets ``projection_status = 'removed'``, sets ``event_id = NULL``,
        and stamps ``removed_at``.  The event itself is NOT deleted (it
        may still be referenced by adherence computations), but the link
        is severed so future projection runs do not see a stale event.
        """
        row = await self._executor(executor).fetchrow(
            _REMOVE_PROJECTION_SQL,
            projection_id,
            _normalize_datetime(now or _utc_now()),
            user_id,
        )
        if row is None:
            raise LookupError("projection not found or not owned by user")
        return _projection_from_row(_mapping_row(row))

    async def detach_projection_event(
        self,
        *,
        projection_id: UUID,
        user_id: UUID,
        now: datetime | None = None,
        executor: Any | None = None,
    ) -> UUID:
        """Set ``event_id = NULL`` on a projection row without changing status.

        Returns the *projection_id* on success.  Use this when the
        projection-owned event needs to be removed (e.g., during
        supersession where the new version gets a fresh event).
        """
        row = await self._executor(executor).fetchrow(
            _DETACH_PROJECTION_EVENT_SQL,
            projection_id,
            _normalize_datetime(now or _utc_now()),
            user_id,
        )
        if row is None:
            raise LookupError("projection not found or not owned by user")
        return row["id"]

    async def create_projection_event(
        self,
        *,
        commitment_id: UUID,
        user_id: UUID,
        topic_id: UUID,
        bot_id: str = "hector",
        metric_key: str = "workout",
        adherence_status: str = "done",
        value_numeric: float | None = None,
        value_text: str | None = None,
        unit: str | None = None,
        observed_at: datetime | None = None,
        note: str | None = None,
        source_message_ids: list[UUID] | None = None,
        executor: Any | None = None,
    ) -> dict[str, Any]:
        """Create a projection-owned event in ``mediator.events``.

        Returns a dict with ``id``, ``commitment_id``, ``metric_key``,
        ``adherence_status``, and ``observed_at`` so callers can link
        the event to a projection via ``insert_projection(event_id=...)``.
        """
        row = await self._executor(executor).fetchrow(
            _INSERT_PROJECTION_EVENT_SQL,
            commitment_id,
            user_id,
            topic_id,
            bot_id,
            metric_key,
            adherence_status,
            value_numeric,
            value_text,
            unit,
            _normalize_datetime(observed_at or _utc_now()),
            note,
            source_message_ids or [],
        )
        return {
            "id": row["id"],
            "commitment_id": row["commitment_id"],
            "metric_key": row["metric_key"],
            "adherence_status": row["adherence_status"],
            "observed_at": row["observed_at"],
        }

    async def delete_projection_event(
        self,
        *,
        event_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> bool:
        """Delete a projection-owned event after verifying ownership.

        Returns True if the event was deleted.  The caller MUST first
        verify ownership via ``find_projection_by_event`` — this method
        does an additional user-scoped guard as defense-in-depth.

        Manual ``log_event`` testimony (events not linked by any
        projection row) will never match the user-scoped DELETE and
        will therefore return False.
        """
        row = await self._executor(executor).fetchrow(
            _DELETE_PROJECTION_EVENT_SQL,
            event_id,
            user_id,
        )
        return row is not None

    # ------------------------------------------------------------------
    # Connection-scoped local delete primitives
    # ------------------------------------------------------------------

    async def mark_connection_deleted(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        now: datetime | None = None,
        executor: Any | None = None,
    ) -> HealthConnectionRecord:
        """Mark a connection deleted and clear encrypted token fields.

        The update is scoped by both ``connection_id`` and ``user_id`` and
        only fires when the connection is not already deleted, so it doubles
        as an ownership guard.  Raises ``LookupError`` when no matching
        connection is found (missing, already deleted, or not owned by the
        caller).  ``COALESCE`` keeps any previously-recorded ``deleted_at`` /
        ``revoked_at`` so repeated calls remain idempotent at the SQL layer.
        """
        row = await self._executor(executor).fetchrow(
            _MARK_CONNECTION_DELETED_SQL,
            connection_id,
            user_id,
            _normalize_datetime(now or _utc_now()),
        )
        if row is None:
            raise LookupError("health connection not found or not owned by user")
        return _connection_from_row(_mapping_row(row))

    async def delete_connection_source_records(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        await self._executor(executor).execute(
            _DELETE_CONNECTION_SOURCE_RECORDS_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_normalized_measurements(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        await self._executor(executor).execute(
            _DELETE_CONNECTION_NORMALIZED_MEASUREMENTS_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_normalized_sleep(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        await self._executor(executor).execute(
            _DELETE_CONNECTION_NORMALIZED_SLEEP_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_normalized_workouts(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        await self._executor(executor).execute(
            _DELETE_CONNECTION_NORMALIZED_WORKOUTS_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_dirty_categories(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        await self._executor(executor).execute(
            _DELETE_CONNECTION_DIRTY_CATEGORIES_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_webhook_receipts(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        await self._executor(executor).execute(
            _DELETE_CONNECTION_WEBHOOK_RECEIPTS_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_projection_owned_events(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        """Delete adherence events owned by this connection's projections.

        Only events linked through the projection ledger for this
        connection+user are removed; manual ``log_event`` testimony is
        preserved because it has no ledger row.
        """
        await self._executor(executor).execute(
            _DELETE_CONNECTION_PROJECTION_OWNED_EVENTS_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_projection_ledger(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        executor: Any | None = None,
    ) -> None:
        await self._executor(executor).execute(
            _DELETE_CONNECTION_PROJECTION_LEDGER_SQL,
            connection_id,
            user_id,
        )

    async def delete_connection_data(
        self,
        *,
        connection_id: UUID,
        user_id: UUID,
        now: datetime | None = None,
    ) -> HealthConnectionRecord:
        """Delete all local Withings health data for a user's connection.

        Runs every removal inside a single transaction so the cleanup is
        atomic: either the connection is fully torn down locally or no
        partial state is left behind.  Order matters — projection-owned
        adherence events are removed first (their ``event_id`` is resolved
        from the projection ledger via a subquery), then the ledger itself,
        then normalized rows, webhook receipts, dirty categories, source
        records, and finally the connection is marked deleted with its
        encrypted token fields cleared.

        Every statement is scoped by both ``connection_id`` and ``user_id``,
        so another user's rows and manual adherence events can never be
        reached even under argument-binding bugs.  Raises ``LookupError``
        when the connection does not exist or is not owned by the user.
        """
        timestamp = _normalize_datetime(now or _utc_now())
        async with self.transaction() as connection:
            await connection.execute(
                _DELETE_CONNECTION_PROJECTION_OWNED_EVENTS_SQL,
                connection_id,
                user_id,
            )
            await connection.execute(
                _DELETE_CONNECTION_PROJECTION_LEDGER_SQL,
                connection_id,
                user_id,
            )
            await connection.execute(
                _DELETE_CONNECTION_NORMALIZED_WORKOUTS_SQL,
                connection_id,
                user_id,
            )
            await connection.execute(
                _DELETE_CONNECTION_NORMALIZED_SLEEP_SQL,
                connection_id,
                user_id,
            )
            await connection.execute(
                _DELETE_CONNECTION_NORMALIZED_MEASUREMENTS_SQL,
                connection_id,
                user_id,
            )
            await connection.execute(
                _DELETE_CONNECTION_WEBHOOK_RECEIPTS_SQL,
                connection_id,
                user_id,
            )
            await connection.execute(
                _DELETE_CONNECTION_DIRTY_CATEGORIES_SQL,
                connection_id,
                user_id,
            )
            await connection.execute(
                _DELETE_CONNECTION_SOURCE_RECORDS_SQL,
                connection_id,
                user_id,
            )
            row = await connection.fetchrow(
                _MARK_CONNECTION_DELETED_SQL,
                connection_id,
                user_id,
                timestamp,
            )
        if row is None:
            raise LookupError("health connection not found or not owned by user")
        return _connection_from_row(_mapping_row(row))

    def _executor(self, executor: Any | None) -> Any:
        return executor if executor is not None else self._pool


def repository_for(pool: Any) -> HealthSyncRepository:
    return HealthSyncRepository(pool)


__all__ = [
    "HealthConnectionRecord",
    "HealthDirtyCategory",
    "HealthProjectionRecord",
    "HealthSyncRepository",
    "HealthWebhookReceipt",
    "StoredHealthSourceRecord",
    "repository_for",
]
