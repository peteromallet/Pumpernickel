"""Encrypted health-connection token persistence and refresh helpers."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from uuid import UUID

from app.services.crypto import CryptoError, decrypt_value, encrypt_value
from app.services.health_sync.models import (
    HealthOAuthTokens,
    HealthProviderSlug,
    HealthResourceType,
    HealthSyncError,
    HealthSyncErrorKind,
)
from app.services.health_sync.provider import HealthSyncProvider

_WITHINGS_REFRESH_TOKEN_TTL = timedelta(days=365)
_REAUTH_REQUIRED_DETAIL = "Health connection requires reauthorization."
_TEMPORARY_REFRESH_FAILURE = "Health token refresh failed temporarily."
_REFRESH_FAILURE = "Health token refresh failed."
_TOKEN_DECRYPT_FAILURE = "Stored health tokens could not be decrypted."

_LOAD_CONNECTION_SQL = """
    SELECT id, user_id, provider, external_user_id, status, granted_scopes, granted_at,
           consented_measurements_at, consented_workouts_at, consented_sleep_at,
           access_token_encrypted, refresh_token_encrypted,
           access_token_expires_at, refresh_token_expires_at, refresh_token_rotated_at,
           last_error_at, last_error_code, last_error_detail,
           disconnected_at, revoked_at, deleted_at, created_at, updated_at
    FROM mediator.health_connections
    WHERE id = $1 AND deleted_at IS NULL
"""

_UPSERT_CONNECTION_SQL = """
    INSERT INTO mediator.health_connections (
        user_id,
        provider,
        external_user_id,
        status,
        granted_scopes,
        granted_at,
        consented_measurements_at,
        consented_workouts_at,
        consented_sleep_at,
        access_token_encrypted,
        refresh_token_encrypted,
        access_token_expires_at,
        refresh_token_expires_at,
        refresh_token_rotated_at,
        last_error_at,
        last_error_code,
        last_error_detail,
        disconnected_at,
        revoked_at,
        updated_at
    )
    VALUES (
        $1, $2, $3, 'active', $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
        NULL, NULL, NULL, NULL, NULL, $14
    )
    ON CONFLICT (user_id, provider) WHERE deleted_at IS NULL
    DO UPDATE
    SET external_user_id = EXCLUDED.external_user_id,
        status = 'active',
        granted_scopes = EXCLUDED.granted_scopes,
        granted_at = COALESCE(mediator.health_connections.granted_at, EXCLUDED.granted_at),
        consented_measurements_at = COALESCE(
            mediator.health_connections.consented_measurements_at,
            EXCLUDED.consented_measurements_at
        ),
        consented_workouts_at = COALESCE(
            mediator.health_connections.consented_workouts_at,
            EXCLUDED.consented_workouts_at
        ),
        consented_sleep_at = COALESCE(
            mediator.health_connections.consented_sleep_at,
            EXCLUDED.consented_sleep_at
        ),
        access_token_encrypted = EXCLUDED.access_token_encrypted,
        refresh_token_encrypted = EXCLUDED.refresh_token_encrypted,
        access_token_expires_at = EXCLUDED.access_token_expires_at,
        refresh_token_expires_at = EXCLUDED.refresh_token_expires_at,
        refresh_token_rotated_at = EXCLUDED.refresh_token_rotated_at,
        last_error_at = NULL,
        last_error_code = NULL,
        last_error_detail = NULL,
        disconnected_at = NULL,
        revoked_at = NULL,
        updated_at = EXCLUDED.updated_at
    RETURNING id, user_id, provider, external_user_id, status, granted_scopes, granted_at,
              consented_measurements_at, consented_workouts_at, consented_sleep_at,
              access_token_encrypted, refresh_token_encrypted,
              access_token_expires_at, refresh_token_expires_at, refresh_token_rotated_at,
              last_error_at, last_error_code, last_error_detail,
              disconnected_at, revoked_at, deleted_at, created_at, updated_at
"""

_ROTATE_REFRESH_SQL = """
    UPDATE mediator.health_connections
    SET external_user_id = $2,
        status = 'active',
        granted_scopes = $3,
        granted_at = $4,
        access_token_encrypted = $5,
        refresh_token_encrypted = $6,
        access_token_expires_at = $7,
        refresh_token_expires_at = $8,
        refresh_token_rotated_at = $9,
        last_error_at = NULL,
        last_error_code = NULL,
        last_error_detail = NULL,
        disconnected_at = NULL,
        revoked_at = NULL,
        updated_at = $10
    WHERE id = $1
      AND deleted_at IS NULL
      AND updated_at = $11
      AND refresh_token_encrypted IS NOT DISTINCT FROM $12
    RETURNING id, user_id, provider, external_user_id, status, granted_scopes, granted_at,
              consented_measurements_at, consented_workouts_at, consented_sleep_at,
              access_token_encrypted, refresh_token_encrypted,
              access_token_expires_at, refresh_token_expires_at, refresh_token_rotated_at,
              last_error_at, last_error_code, last_error_detail,
              disconnected_at, revoked_at, deleted_at, created_at, updated_at
"""

_MARK_REAUTH_REQUIRED_SQL = """
    UPDATE mediator.health_connections
    SET status = 'reauth_required',
        access_token_encrypted = NULL,
        refresh_token_encrypted = NULL,
        access_token_expires_at = NULL,
        refresh_token_expires_at = NULL,
        last_error_at = $2,
        last_error_code = $3,
        last_error_detail = $4,
        updated_at = $2
    WHERE id = $1
      AND deleted_at IS NULL
      AND updated_at = $5
    RETURNING id, user_id, provider, external_user_id, status, granted_scopes, granted_at,
              consented_measurements_at, consented_workouts_at, consented_sleep_at,
              access_token_encrypted, refresh_token_encrypted,
              access_token_expires_at, refresh_token_expires_at, refresh_token_rotated_at,
              last_error_at, last_error_code, last_error_detail,
              disconnected_at, revoked_at, deleted_at, created_at, updated_at
"""


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalized_scopes(values: Iterable[str]) -> frozenset[str]:
    scopes = {str(value).strip() for value in values if str(value).strip()}
    return frozenset(scopes)


@dataclass(frozen=True, slots=True)
class HealthConnectionTokens:
    connection_id: UUID
    user_id: UUID
    provider: HealthProviderSlug
    status: str
    external_user_id: str | None
    granted_scopes: frozenset[str]
    granted_at: datetime | None
    consented_measurements_at: datetime | None
    consented_workouts_at: datetime | None
    consented_sleep_at: datetime | None
    access_token: str | None
    refresh_token: str | None
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None
    refresh_token_rotated_at: datetime | None
    last_error_at: datetime | None
    last_error_code: str | None
    last_error_detail: str | None
    disconnected_at: datetime | None
    revoked_at: datetime | None
    updated_at: datetime
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "granted_at",
            "consented_measurements_at",
            "consented_workouts_at",
            "consented_sleep_at",
            "access_token_expires_at",
            "refresh_token_expires_at",
            "refresh_token_rotated_at",
            "last_error_at",
            "disconnected_at",
            "revoked_at",
            "updated_at",
            "created_at",
        ):
            object.__setattr__(self, field_name, _normalize_datetime(getattr(self, field_name)))
        if self.external_user_id is not None:
            object.__setattr__(self, "external_user_id", self.external_user_id.strip() or None)
        if self.last_error_code is not None:
            object.__setattr__(self, "last_error_code", self.last_error_code.strip() or None)
        if self.last_error_detail is not None:
            object.__setattr__(self, "last_error_detail", self.last_error_detail.strip() or None)
        object.__setattr__(self, "granted_scopes", _normalized_scopes(self.granted_scopes))


@dataclass(frozen=True, slots=True)
class _LoadedConnectionRow:
    state: HealthConnectionTokens
    access_token_encrypted: bytes | None
    refresh_token_encrypted: bytes | None


class HealthTokenStoreError(RuntimeError):
    """Raised when persisted health tokens cannot be loaded or refreshed safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        retryable: bool = False,
        reauth_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.reauth_required = reauth_required


def _error_from_exception(exc: Exception) -> HealthSyncError:
    candidate = getattr(exc, "error", None)
    if isinstance(candidate, HealthSyncError):
        return candidate
    return HealthSyncError.permanent_error(
        code="token_refresh_failed",
        detail="health token refresh failed",
    )


def _refresh_failure_message(error: HealthSyncError) -> str:
    if error.kind is HealthSyncErrorKind.AUTHENTICATION:
        return _REAUTH_REQUIRED_DETAIL
    if error.retryable:
        return _TEMPORARY_REFRESH_FAILURE
    return _REFRESH_FAILURE


def _resource_consent_fields(
    resource_types: Iterable[HealthResourceType] | None,
    *,
    granted_at: datetime,
) -> tuple[datetime | None, datetime | None, datetime | None]:
    selected = {HealthResourceType(resource_type) for resource_type in (resource_types or ())}
    return (
        granted_at if HealthResourceType.MEASUREMENT in selected else None,
        granted_at if HealthResourceType.WORKOUT in selected else None,
        granted_at if HealthResourceType.SLEEP in selected else None,
    )


def _refresh_token_expiry(
    refresh_token: str | None,
    *,
    issued_at: datetime,
    previous_expiry: datetime | None = None,
    previous_refresh_token: str | None = None,
) -> datetime | None:
    if refresh_token is None:
        return None
    if previous_expiry is not None and refresh_token == previous_refresh_token:
        return previous_expiry
    return issued_at + _WITHINGS_REFRESH_TOKEN_TTL


def _bytes_or_none(value: Any) -> bytes | None:
    if value is None:
        return None
    if isinstance(value, memoryview):
        return bytes(value)
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, bytes):
        return value
    raise TypeError(f"expected bytea-compatible value, got {type(value)!r}")


def _mapping_row(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    return dict(row)


def _decode_connection_tokens(row: Mapping[str, Any]) -> _LoadedConnectionRow:
    try:
        access_token_encrypted = _bytes_or_none(row.get("access_token_encrypted"))
        refresh_token_encrypted = _bytes_or_none(row.get("refresh_token_encrypted"))
        access_token = decrypt_value(access_token_encrypted)
        refresh_token = decrypt_value(refresh_token_encrypted)
    except (CryptoError, TypeError) as exc:
        raise HealthTokenStoreError(
            _TOKEN_DECRYPT_FAILURE,
            code="token_decrypt_failed",
        ) from exc

    state = HealthConnectionTokens(
        connection_id=row["id"],
        user_id=row["user_id"],
        provider=HealthProviderSlug(row["provider"]),
        status=str(row["status"]),
        external_user_id=row.get("external_user_id"),
        granted_scopes=frozenset(row.get("granted_scopes") or ()),
        granted_at=row.get("granted_at"),
        consented_measurements_at=row.get("consented_measurements_at"),
        consented_workouts_at=row.get("consented_workouts_at"),
        consented_sleep_at=row.get("consented_sleep_at"),
        access_token=access_token,
        refresh_token=refresh_token,
        access_token_expires_at=row.get("access_token_expires_at"),
        refresh_token_expires_at=row.get("refresh_token_expires_at"),
        refresh_token_rotated_at=row.get("refresh_token_rotated_at"),
        last_error_at=row.get("last_error_at"),
        last_error_code=row.get("last_error_code"),
        last_error_detail=row.get("last_error_detail"),
        disconnected_at=row.get("disconnected_at"),
        revoked_at=row.get("revoked_at"),
        created_at=row.get("created_at"),
        updated_at=row["updated_at"],
    )
    return _LoadedConnectionRow(
        state=state,
        access_token_encrypted=access_token_encrypted,
        refresh_token_encrypted=refresh_token_encrypted,
    )


async def _load_connection_row(pool: Any, *, connection_id: UUID) -> _LoadedConnectionRow:
    row = await pool.fetchrow(_LOAD_CONNECTION_SQL, connection_id)
    if row is None:
        raise HealthTokenStoreError(
            "Health connection not found.",
            code="connection_not_found",
        )
    return _decode_connection_tokens(_mapping_row(row))


async def load_connection_tokens(pool: Any, *, connection_id: UUID) -> HealthConnectionTokens:
    """Load and decrypt the current token state for a health connection."""
    return (await _load_connection_row(pool, connection_id=connection_id)).state


async def store_connection_tokens(
    pool: Any,
    *,
    user_id: UUID,
    provider: HealthProviderSlug | str,
    tokens: HealthOAuthTokens,
    granted_at: datetime | None = None,
    resource_types: Iterable[HealthResourceType] | None = None,
    now: datetime | None = None,
) -> HealthConnectionTokens:
    """Insert or update the active connection row with encrypted tokens."""
    issued_at = _normalize_datetime(now or granted_at or _utc_now())
    assert issued_at is not None
    consented_measurements_at, consented_workouts_at, consented_sleep_at = _resource_consent_fields(
        resource_types,
        granted_at=issued_at,
    )
    row = await pool.fetchrow(
        _UPSERT_CONNECTION_SQL,
        user_id,
        HealthProviderSlug(provider).value,
        tokens.external_user_id,
        sorted(tokens.granted_scopes),
        issued_at,
        consented_measurements_at,
        consented_workouts_at,
        consented_sleep_at,
        encrypt_value(tokens.access_token),
        encrypt_value(tokens.refresh_token),
        _normalize_datetime(tokens.expires_at),
        _refresh_token_expiry(tokens.refresh_token, issued_at=issued_at),
        None,
        issued_at,
    )
    return _decode_connection_tokens(_mapping_row(row)).state


async def _mark_reauth_required(
    pool: Any,
    *,
    snapshot: _LoadedConnectionRow,
    error_code: str,
    now: datetime,
) -> HealthConnectionTokens | None:
    row = await pool.fetchrow(
        _MARK_REAUTH_REQUIRED_SQL,
        snapshot.state.connection_id,
        now,
        error_code,
        _REAUTH_REQUIRED_DETAIL,
        snapshot.state.updated_at,
    )
    if row is None:
        return None
    return _decode_connection_tokens(_mapping_row(row)).state


async def refresh_connection_tokens(
    pool: Any,
    *,
    connection_id: UUID,
    provider: HealthSyncProvider,
    now: datetime | None = None,
) -> HealthConnectionTokens:
    """Refresh a connection's access token without losing a rotated refresh token.

    The update uses compare-and-swap on the stored refresh-token ciphertext and
    `updated_at`. If another worker wins the race first, the loser re-reads the
    row and returns the current token state instead of clobbering it.
    """
    snapshot = await _load_connection_row(pool, connection_id=connection_id)
    if snapshot.state.refresh_token is None:
        marked = await _mark_reauth_required(
            pool,
            snapshot=snapshot,
            error_code="missing_refresh_token",
            now=_normalize_datetime(now or _utc_now()) or _utc_now(),
        )
        if marked is not None:
            raise HealthTokenStoreError(
                _REAUTH_REQUIRED_DETAIL,
                code="missing_refresh_token",
                reauth_required=True,
            )
        latest = await load_connection_tokens(pool, connection_id=connection_id)
        if latest.status == "reauth_required":
            raise HealthTokenStoreError(
                _REAUTH_REQUIRED_DETAIL,
                code="missing_refresh_token",
                reauth_required=True,
            )
        return latest

    try:
        refreshed = await provider.refresh_token(refresh_token=snapshot.state.refresh_token)
    except Exception as exc:
        error = _error_from_exception(exc)
        if error.kind is not HealthSyncErrorKind.AUTHENTICATION:
            raise HealthTokenStoreError(
                _refresh_failure_message(error),
                code=error.code,
                retryable=error.retryable,
            ) from exc

        latest = await _load_connection_row(pool, connection_id=connection_id)
        if (
            latest.state.updated_at != snapshot.state.updated_at
            or latest.refresh_token_encrypted != snapshot.refresh_token_encrypted
        ):
            return latest.state

        reauth_state = await _mark_reauth_required(
            pool,
            snapshot=snapshot,
            error_code=error.code,
            now=_normalize_datetime(now or _utc_now()) or _utc_now(),
        )
        if reauth_state is None:
            latest_after_conflict = await load_connection_tokens(pool, connection_id=connection_id)
            if latest_after_conflict.status != "reauth_required":
                return latest_after_conflict
        raise HealthTokenStoreError(
            _REAUTH_REQUIRED_DETAIL,
            code=error.code,
            reauth_required=True,
        ) from exc

    refreshed_at = _normalize_datetime(now or _utc_now())
    assert refreshed_at is not None
    next_refresh_token = refreshed.refresh_token or snapshot.state.refresh_token
    rotated_at = snapshot.state.refresh_token_rotated_at
    if next_refresh_token is not None and next_refresh_token != snapshot.state.refresh_token:
        rotated_at = refreshed_at

    row = await pool.fetchrow(
        _ROTATE_REFRESH_SQL,
        connection_id,
        refreshed.external_user_id or snapshot.state.external_user_id,
        sorted(refreshed.granted_scopes or snapshot.state.granted_scopes),
        snapshot.state.granted_at or refreshed_at,
        encrypt_value(refreshed.access_token),
        encrypt_value(next_refresh_token),
        _normalize_datetime(refreshed.expires_at) or snapshot.state.access_token_expires_at,
        _refresh_token_expiry(
            next_refresh_token,
            issued_at=refreshed_at,
            previous_expiry=snapshot.state.refresh_token_expires_at,
            previous_refresh_token=snapshot.state.refresh_token,
        ),
        rotated_at,
        refreshed_at,
        snapshot.state.updated_at,
        snapshot.refresh_token_encrypted,
    )
    if row is not None:
        return _decode_connection_tokens(_mapping_row(row)).state

    latest = await load_connection_tokens(pool, connection_id=connection_id)
    if latest.updated_at != snapshot.state.updated_at or latest.refresh_token != snapshot.state.refresh_token:
        return latest
    raise HealthTokenStoreError(
        "Health token refresh could not be persisted safely.",
        code="refresh_conflict",
        retryable=True,
    )


__all__ = [
    "HealthConnectionTokens",
    "HealthTokenStoreError",
    "load_connection_tokens",
    "refresh_connection_tokens",
    "store_connection_tokens",
]
