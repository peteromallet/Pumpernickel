from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime
import secrets
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services import crypto
from app.services.health_sync.fake_withings import FakeWithingsProvider
from app.services.health_sync.models import (
    HealthOAuthTokens,
    HealthProviderSlug,
    HealthResourceType,
    HealthSyncError,
)
from app.services.health_sync.tokens import (
    HealthTokenStoreError,
    load_connection_tokens,
    refresh_connection_tokens,
    store_connection_tokens,
)
from app.services.health_sync.withings import WithingsAdapterError


def _set_key(monkeypatch: pytest.MonkeyPatch, key: bytes | None) -> None:
    if key is None:
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", "")
    else:
        monkeypatch.setenv("DATA_ENCRYPTION_KEY", base64.b64encode(key).decode())
    crypto.reset_cache_for_tests()
    from app.config import get_settings

    get_settings.cache_clear()


class HealthTokenPool:
    def __init__(self) -> None:
        self._rows_by_id: dict[UUID, dict[str, Any]] = {}
        self._row_ids_by_user_provider: dict[tuple[UUID, str], UUID] = {}

    @property
    def rows(self) -> dict[UUID, dict[str, Any]]:
        return self._rows_by_id

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO mediator.health_connections"):
            return self._upsert(*args)
        if compact.startswith("SELECT id, user_id, provider, external_user_id, status, granted_scopes"):
            row = self._rows_by_id.get(args[0])
            if row is None or row.get("deleted_at") is not None:
                return None
            return dict(row)
        if compact.startswith("UPDATE mediator.health_connections SET external_user_id = $2"):
            return self._rotate(*args)
        if compact.startswith("UPDATE mediator.health_connections SET status = 'reauth_required'"):
            return self._mark_reauth(*args)
        raise AssertionError(f"Unexpected SQL: {compact}")

    def _upsert(self, *args: Any) -> dict[str, Any]:
        (
            user_id,
            provider,
            external_user_id,
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
            updated_at,
        ) = args
        key = (user_id, provider)
        existing_id = self._row_ids_by_user_provider.get(key)
        if existing_id is None:
            connection_id = uuid4()
            created_at = updated_at
            row = {
                "id": connection_id,
                "user_id": user_id,
                "provider": provider,
                "external_user_id": external_user_id,
                "status": "active",
                "granted_scopes": list(granted_scopes),
                "granted_at": granted_at,
                "consented_measurements_at": consented_measurements_at,
                "consented_workouts_at": consented_workouts_at,
                "consented_sleep_at": consented_sleep_at,
                "access_token_encrypted": access_token_encrypted,
                "refresh_token_encrypted": refresh_token_encrypted,
                "access_token_expires_at": access_token_expires_at,
                "refresh_token_expires_at": refresh_token_expires_at,
                "refresh_token_rotated_at": refresh_token_rotated_at,
                "last_error_at": None,
                "last_error_code": None,
                "last_error_detail": None,
                "disconnected_at": None,
                "revoked_at": None,
                "deleted_at": None,
                "created_at": created_at,
                "updated_at": updated_at,
            }
            self._rows_by_id[connection_id] = row
            self._row_ids_by_user_provider[key] = connection_id
            return dict(row)

        row = self._rows_by_id[existing_id]
        row.update(
            {
                "external_user_id": external_user_id,
                "status": "active",
                "granted_scopes": list(granted_scopes),
                "granted_at": row["granted_at"] or granted_at,
                "consented_measurements_at": row["consented_measurements_at"] or consented_measurements_at,
                "consented_workouts_at": row["consented_workouts_at"] or consented_workouts_at,
                "consented_sleep_at": row["consented_sleep_at"] or consented_sleep_at,
                "access_token_encrypted": access_token_encrypted,
                "refresh_token_encrypted": refresh_token_encrypted,
                "access_token_expires_at": access_token_expires_at,
                "refresh_token_expires_at": refresh_token_expires_at,
                "refresh_token_rotated_at": refresh_token_rotated_at,
                "last_error_at": None,
                "last_error_code": None,
                "last_error_detail": None,
                "disconnected_at": None,
                "revoked_at": None,
                "updated_at": updated_at,
            }
        )
        return dict(row)

    def _rotate(self, *args: Any) -> dict[str, Any] | None:
        (
            connection_id,
            external_user_id,
            granted_scopes,
            granted_at,
            access_token_encrypted,
            refresh_token_encrypted,
            access_token_expires_at,
            refresh_token_expires_at,
            refresh_token_rotated_at,
            updated_at,
            expected_updated_at,
            expected_refresh_token_encrypted,
        ) = args
        row = self._rows_by_id.get(connection_id)
        if row is None or row.get("deleted_at") is not None:
            return None
        if row["updated_at"] != expected_updated_at:
            return None
        if row.get("refresh_token_encrypted") != expected_refresh_token_encrypted:
            return None
        row.update(
            {
                "external_user_id": external_user_id,
                "status": "active",
                "granted_scopes": list(granted_scopes),
                "granted_at": granted_at,
                "access_token_encrypted": access_token_encrypted,
                "refresh_token_encrypted": refresh_token_encrypted,
                "access_token_expires_at": access_token_expires_at,
                "refresh_token_expires_at": refresh_token_expires_at,
                "refresh_token_rotated_at": refresh_token_rotated_at,
                "last_error_at": None,
                "last_error_code": None,
                "last_error_detail": None,
                "disconnected_at": None,
                "revoked_at": None,
                "updated_at": updated_at,
            }
        )
        return dict(row)

    def _mark_reauth(self, *args: Any) -> dict[str, Any] | None:
        connection_id, last_error_at, last_error_code, last_error_detail, expected_updated_at = args
        row = self._rows_by_id.get(connection_id)
        if row is None or row.get("deleted_at") is not None:
            return None
        if row["updated_at"] != expected_updated_at:
            return None
        row.update(
            {
                "status": "reauth_required",
                "access_token_encrypted": None,
                "refresh_token_encrypted": None,
                "access_token_expires_at": None,
                "refresh_token_expires_at": None,
                "last_error_at": last_error_at,
                "last_error_code": last_error_code,
                "last_error_detail": last_error_detail,
                "updated_at": last_error_at,
            }
        )
        return dict(row)


class CoordinatedRefreshProvider:
    def __init__(self, delegate: FakeWithingsProvider, *, participants: int) -> None:
        self._delegate = delegate
        self._participants = participants
        self._entered = 0
        self._ready = asyncio.Event()
        self.name = delegate.name
        self.capabilities = delegate.capabilities

    async def refresh_token(self, *, refresh_token: str) -> HealthOAuthTokens:
        self._entered += 1
        if self._entered >= self._participants:
            self._ready.set()
        await self._ready.wait()
        return await self._delegate.refresh_token(refresh_token=refresh_token)


class LeakyRefreshProvider:
    name = "withings"
    capabilities = FakeWithingsProvider().capabilities

    async def refresh_token(self, *, refresh_token: str) -> HealthOAuthTokens:
        raise WithingsAdapterError(
            HealthSyncError.retryable_error(
                code="provider_temporarily_unavailable",
                detail=f"refresh token {refresh_token} was rejected by upstream",
            )
        )


@pytest.mark.asyncio
async def test_store_tokens_encrypts_and_loads_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    pool = HealthTokenPool()
    user_id = uuid4()
    granted_at = datetime(2026, 7, 20, 13, 0, tzinfo=UTC)

    stored = await store_connection_tokens(
        pool,
        user_id=user_id,
        provider=HealthProviderSlug.WITHINGS,
        granted_at=granted_at,
        tokens=HealthOAuthTokens(
            access_token="access-token-v1",
            refresh_token="refresh-token-v1",
            expires_at=datetime(2026, 7, 20, 16, 0, tzinfo=UTC),
            external_user_id="420001",
            granted_scopes=frozenset({"user.metrics", "user.activity"}),
        ),
        resource_types=(HealthResourceType.MEASUREMENT, HealthResourceType.WORKOUT),
    )

    persisted = pool.rows[stored.connection_id]
    assert persisted["access_token_encrypted"].startswith(b"AGV1")
    assert persisted["refresh_token_encrypted"].startswith(b"AGV1")
    assert b"access-token-v1" not in persisted["access_token_encrypted"]
    assert b"refresh-token-v1" not in persisted["refresh_token_encrypted"]

    loaded = await load_connection_tokens(pool, connection_id=stored.connection_id)

    assert loaded.status == "active"
    assert loaded.access_token == "access-token-v1"
    assert loaded.refresh_token == "refresh-token-v1"
    assert loaded.external_user_id == "420001"
    assert loaded.granted_scopes == frozenset({"user.activity", "user.metrics"})
    assert loaded.consented_measurements_at == granted_at
    assert loaded.consented_workouts_at == granted_at
    assert loaded.consented_sleep_at is None


@pytest.mark.asyncio
async def test_refresh_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    pool = HealthTokenPool()
    user_id = uuid4()
    stored = await store_connection_tokens(
        pool,
        user_id=user_id,
        provider=HealthProviderSlug.WITHINGS,
        tokens=HealthOAuthTokens(
            access_token="access-token-v1",
            refresh_token="refresh-token-v1",
            expires_at=datetime(2026, 7, 20, 16, 0, tzinfo=UTC),
            external_user_id="420001",
            granted_scopes=frozenset({"user.metrics"}),
        ),
        now=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
    )

    with pytest.raises(HealthTokenStoreError) as excinfo:
        await refresh_connection_tokens(
            pool,
            connection_id=stored.connection_id,
            provider=LeakyRefreshProvider(),
            now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
        )

    exc = excinfo.value
    assert exc.retryable is True
    assert exc.code == "provider_temporarily_unavailable"
    assert str(exc) == "Health token refresh failed temporarily."
    assert "refresh-token-v1" not in str(exc)
    assert pool.rows[stored.connection_id]["last_error_detail"] is None


@pytest.mark.asyncio
async def test_concurrent_refresh_returns_single_current_token_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    pool = HealthTokenPool()
    user_id = uuid4()
    delegate = FakeWithingsProvider()
    exchanged = await delegate.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri="https://example.test/api/health/devices/withings/oauth/callback",
    )
    stored = await store_connection_tokens(
        pool,
        user_id=user_id,
        provider=HealthProviderSlug.WITHINGS,
        tokens=exchanged,
        now=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
    )
    provider = CoordinatedRefreshProvider(delegate, participants=2)

    first, second = await asyncio.gather(
        refresh_connection_tokens(
            pool,
            connection_id=stored.connection_id,
            provider=provider,
            now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
        ),
        refresh_connection_tokens(
            pool,
            connection_id=stored.connection_id,
            provider=provider,
            now=datetime(2026, 7, 20, 14, 0, tzinfo=UTC),
        ),
    )

    assert first.access_token == "synthetic-access-token-v2"
    assert second.access_token == "synthetic-access-token-v2"
    assert first.refresh_token == "synthetic-refresh-token-v2"
    assert second.refresh_token == "synthetic-refresh-token-v2"
    assert first.status == "active"
    assert second.status == "active"
    persisted = pool.rows[stored.connection_id]
    assert crypto.decrypt_value(persisted["refresh_token_encrypted"]) == "synthetic-refresh-token-v2"
    assert persisted["status"] == "active"
    assert persisted["refresh_token_rotated_at"] == datetime(2026, 7, 20, 14, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_revoked_refresh_marks_connection_reauth_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_key(monkeypatch, secrets.token_bytes(32))
    pool = HealthTokenPool()
    user_id = uuid4()
    provider = FakeWithingsProvider()
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri="https://example.test/api/health/devices/withings/oauth/callback",
    )
    stored = await store_connection_tokens(
        pool,
        user_id=user_id,
        provider=HealthProviderSlug.WITHINGS,
        tokens=exchanged,
        now=datetime(2026, 7, 20, 13, 0, tzinfo=UTC),
    )
    await provider.revoke(
        access_token=exchanged.access_token,
        refresh_token=exchanged.refresh_token,
    )

    with pytest.raises(HealthTokenStoreError) as excinfo:
        await refresh_connection_tokens(
            pool,
            connection_id=stored.connection_id,
            provider=provider,
            now=datetime(2026, 7, 20, 14, 30, tzinfo=UTC),
        )

    exc = excinfo.value
    assert exc.code == "invalid_refresh_token"
    assert exc.reauth_required is True
    assert str(exc) == "Health connection requires reauthorization."
    assert "synthetic-refresh-token-v1" not in str(exc)
    persisted = pool.rows[stored.connection_id]
    assert persisted["status"] == "reauth_required"
    assert persisted["access_token_encrypted"] is None
    assert persisted["refresh_token_encrypted"] is None
    assert persisted["last_error_code"] == "invalid_refresh_token"
    assert persisted["last_error_detail"] == "Health connection requires reauthorization."
