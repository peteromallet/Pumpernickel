"""Authenticated health-data export for the current user's Withings connection.

Exports:
  * Connection metadata (no encrypted tokens or OAuth state)
  * Source-record provenance metadata (no raw provider payloads)
  * Normalized measurement, sleep, and workout rows
  * Projection ledger rows
  * Deletion/tombstone state on source records

Excludes:
  * Encrypted tokens (access_token_encrypted, refresh_token_encrypted)
  * OAuth state rows
  * Raw provider payloads (not stored)
  * Webhook receipt rows
  * Cross-user data (scoped to authenticated user_id)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID


def _isoformat(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


async def export_withings_data(
    pool: Any,
    *,
    user_id: UUID,
) -> dict[str, Any]:
    """Return a JSON-serialisable export dict for *user_id*.

    Only Withings-provider data is included.  Every query is scoped to
    *user_id* so cross-user data can never leak.
    """
    now = datetime.now(tz=UTC)

    # -- connections (metadata-only: no encrypted tokens, no cursor_state) --
    connections = await _export_connections(pool, user_id)

    # Collect connection ids for source-record / projection lookups.
    connection_ids = [conn["id"] for conn in connections]

    # -- source records (provenance metadata; no raw payloads) --
    source_records = await _export_source_records(pool, connection_ids, user_id)

    # -- normalized rows --
    measurements = await _export_normalized_measurements(pool, user_id)
    sleep_rows = await _export_normalized_sleep(pool, user_id)
    workouts = await _export_normalized_workouts(pool, user_id)

    # -- projection ledger --
    projections = await _export_projections(pool, user_id)

    # -- dirty/deletion state --
    dirty_categories = await _export_dirty_categories(pool, connection_ids, user_id)

    return {
        "provider": "withings",
        "exported_at": _isoformat(now),
        "user_id": str(user_id),
        "connections": connections,
        "source_records": source_records,
        "normalized_measurements": measurements,
        "normalized_sleep": sleep_rows,
        "normalized_workouts": workouts,
        "projections": projections,
        "dirty_categories": dirty_categories,
    }


# ---------------------------------------------------------------------------
# Private query helpers
# ---------------------------------------------------------------------------

_CONNECTION_COLUMNS = (
    "id",
    "status",
    "external_user_id",
    "granted_scopes",
    "granted_at",
    "consented_measurements_at",
    "consented_workouts_at",
    "consented_sleep_at",
    "last_success_at",
    "last_error_at",
    "last_error_code",
    "last_error_detail",
    "disconnected_at",
    "revoked_at",
    "deleted_at",
    "created_at",
    "updated_at",
)
# Deliberately omitted: access_token_encrypted, refresh_token_encrypted,
# access_token_expires_at, refresh_token_expires_at, refresh_token_rotated_at,
# cursor_state, provider (always 'withings'), user_id (already in export).


async def _export_connections(pool: Any, user_id: UUID) -> list[dict[str, Any]]:
    sql = f"""
        SELECT {", ".join(_CONNECTION_COLUMNS)}
        FROM mediator.health_connections
        WHERE user_id = $1
          AND provider = 'withings'
        ORDER BY updated_at DESC
    """
    rows = await pool.fetch(sql, user_id)
    return [_serialize_row(row, _CONNECTION_COLUMNS) for row in rows]


_SOURCE_RECORD_COLUMNS = (
    "id",
    "connection_id",
    "provider",
    "resource_type",
    "external_id",
    "source_created_at",
    "source_modified_at",
    "observed_at",
    "starts_at",
    "ends_at",
    "source_timezone",
    "source_offset_seconds",
    "source_device_id",
    "source_device_model",
    "payload_hash",
    "provider_revision",
    "revision_count",
    "source_metadata",
    "attribution",
    "is_deleted",
    "deleted_at",
    "imported_at",
    "updated_at",
)
# user_id omitted (already scoped).


async def _export_source_records(
    pool: Any,
    connection_ids: list[UUID],
    user_id: UUID,
) -> list[dict[str, Any]]:
    if not connection_ids:
        return []
    sql = f"""
        SELECT {", ".join(_SOURCE_RECORD_COLUMNS)}
        FROM mediator.health_source_records
        WHERE user_id = $1
          AND connection_id = ANY($2::uuid[])
        ORDER BY updated_at DESC
    """
    rows = await pool.fetch(sql, user_id, connection_ids)
    return [_serialize_row(row, _SOURCE_RECORD_COLUMNS) for row in rows]


_MEASUREMENT_COLUMNS = (
    "id",
    "source_record_id",
    "connection_id",
    "metric",
    "measured_at",
    "value_numeric",
    "canonical_unit",
    "source_unit",
    "source_device_id",
    "source_device_model",
    "attribution",
    "created_at",
    "updated_at",
)


async def _export_normalized_measurements(
    pool: Any,
    user_id: UUID,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT {", ".join(_MEASUREMENT_COLUMNS)}
        FROM mediator.health_normalized_measurements
        WHERE user_id = $1
        ORDER BY measured_at DESC
    """
    rows = await pool.fetch(sql, user_id)
    return [_serialize_row(row, _MEASUREMENT_COLUMNS) for row in rows]


_SLEEP_COLUMNS = (
    "id",
    "source_record_id",
    "connection_id",
    "started_at",
    "ended_at",
    "local_sleep_date",
    "local_timezone",
    "local_offset_seconds",
    "completeness_state",
    "total_in_bed_seconds",
    "total_asleep_seconds",
    "awake_seconds",
    "light_sleep_seconds",
    "deep_sleep_seconds",
    "rem_sleep_seconds",
    "sleep_latency_seconds",
    "wake_after_sleep_onset_seconds",
    "wakeups",
    "sleep_score",
    "source_device_id",
    "source_device_model",
    "attribution",
    "created_at",
    "updated_at",
)


async def _export_normalized_sleep(
    pool: Any,
    user_id: UUID,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT {", ".join(_SLEEP_COLUMNS)}
        FROM mediator.health_normalized_sleep
        WHERE user_id = $1
        ORDER BY started_at DESC
    """
    rows = await pool.fetch(sql, user_id)
    return [_serialize_row(row, _SLEEP_COLUMNS) for row in rows]


_WORKOUT_COLUMNS = (
    "id",
    "source_record_id",
    "connection_id",
    "started_at",
    "ended_at",
    "local_timezone",
    "local_offset_seconds",
    "workout_type",
    "duration_seconds",
    "pause_duration_seconds",
    "distance_meters",
    "steps",
    "energy_kcal",
    "elevation_gain_meters",
    "average_heart_rate_bpm",
    "max_heart_rate_bpm",
    "source_device_id",
    "source_device_model",
    "attribution",
    "created_at",
    "updated_at",
)


async def _export_normalized_workouts(
    pool: Any,
    user_id: UUID,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT {", ".join(_WORKOUT_COLUMNS)}
        FROM mediator.health_normalized_workouts
        WHERE user_id = $1
        ORDER BY started_at DESC
    """
    rows = await pool.fetch(sql, user_id)
    return [_serialize_row(row, _WORKOUT_COLUMNS) for row in rows]


_PROJECTION_COLUMNS = (
    "id",
    "source_record_id",
    "connection_id",
    "event_id",
    "commitment_id",
    "projection_version",
    "projection_status",
    "match_rule",
    "note",
    "decision_reason",
    "matched_local_date",
    "supersedes_projection_id",
    "projected_at",
    "removed_at",
    "created_at",
    "updated_at",
)


async def _export_projections(
    pool: Any,
    user_id: UUID,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT {", ".join(_PROJECTION_COLUMNS)}
        FROM mediator.health_source_to_event_projections
        WHERE user_id = $1
        ORDER BY created_at DESC
    """
    rows = await pool.fetch(sql, user_id)
    return [_serialize_row(row, _PROJECTION_COLUMNS) for row in rows]


_DIRTY_COLUMNS = (
    "id",
    "connection_id",
    "provider",
    "resource_type",
    "reason",
    "source_receipt_id",
    "attempts",
    "marked_at",
    "claimed_at",
    "claimed_by",
    "cleared_at",
)


async def _export_dirty_categories(
    pool: Any,
    connection_ids: list[UUID],
    user_id: UUID,
) -> list[dict[str, Any]]:
    if not connection_ids:
        return []
    sql = f"""
        SELECT {", ".join(_DIRTY_COLUMNS)}
        FROM mediator.health_dirty_categories
        WHERE user_id = $1
          AND connection_id = ANY($2::uuid[])
        ORDER BY marked_at DESC
    """
    rows = await pool.fetch(sql, user_id, connection_ids)
    return [_serialize_row(row, _DIRTY_COLUMNS) for row in rows]


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _serialize_row(row: Any, columns: tuple[str, ...]) -> dict[str, Any]:
    """Convert an asyncpg Row to a JSON-safe dict."""
    result: dict[str, Any] = {}
    for col in columns:
        value = row[col]
        if isinstance(value, datetime):
            result[col] = _isoformat(value)
        elif isinstance(value, UUID):
            result[col] = str(value)
        elif isinstance(value, set):
            result[col] = sorted(value)
        elif isinstance(value, frozenset):
            result[col] = sorted(value)
        else:
            result[col] = value
    return result
