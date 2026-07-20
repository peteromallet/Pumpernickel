from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services import crypto
from app.services.health_sync import (
    FakeWithingsProvider,
    HealthFetchResult,
    HealthOAuthTokens,
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    HealthSyncCursor,
    HealthSyncStatus,
    WITHINGS_PROVIDER_CAPABILITIES,
    reconcile_connections,
    repository_for,
    store_connection_tokens,
)
from tests.conftest import FakePool


CALLBACK_URL = "https://example.test/api/health/devices/withings/oauth/callback"


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", base64.b64encode(bytes(range(32))).decode())
    crypto.reset_cache_for_tests()
    from app.config import get_settings

    get_settings.cache_clear()


async def _store_valid_connection(
    *,
    pool: FakePool,
    monkeypatch: pytest.MonkeyPatch,
    provider: FakeWithingsProvider,
    user_id,
    scopes: frozenset[str],
) -> tuple[str, str]:
    _set_key(monkeypatch)
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    stored = await store_connection_tokens(
        pool,
        user_id=user_id,
        provider=HealthProviderSlug.WITHINGS,
        tokens=HealthOAuthTokens(
            access_token=exchanged.access_token,
            refresh_token=exchanged.refresh_token,
            expires_at=exchanged.expires_at,
            external_user_id=exchanged.external_user_id,
            granted_scopes=scopes,
        ),
        resource_types=[HealthResourceType.MEASUREMENT],
        now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
    )
    assert stored.external_user_id is not None
    return str(stored.connection_id), stored.external_user_id


class _CursorTrackingProvider:
    name = "withings"
    capabilities = WITHINGS_PROVIDER_CAPABILITIES

    def __init__(self) -> None:
        self.seen_cursors: list[HealthSyncCursor | None] = []

    async def exchange_code(self, *, code: str, redirect_uri: str):
        raise NotImplementedError

    async def refresh_token(self, *, refresh_token: str):
        raise NotImplementedError

    async def fetch_changes(
        self,
        *,
        access_token: str,
        resource_type: HealthResourceType,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        self.seen_cursors.append(cursor)
        assert cursor is not None
        assert cursor.last_modified is not None
        return HealthFetchResult(
            resource_type=resource_type,
            records=(
                HealthSourceRecord(
                    provider=HealthProviderSlug.WITHINGS,
                    resource_type=resource_type,
                    external_id="measure-30d",
                    source_modified_at=cursor.last_modified + timedelta(minutes=1),
                    observed_at=cursor.last_modified + timedelta(minutes=1),
                    payload_hash="hash-30d",
                ),
            ),
            next_cursor=HealthSyncCursor(
                resource_type=resource_type,
                last_modified=cursor.last_modified + timedelta(minutes=1),
            ),
            has_more=False,
        )

    async def revoke(self, *, access_token: str, refresh_token: str | None = None) -> None:
        raise NotImplementedError


async def test_reconciliation_seeds_initial_30_day_backfill_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = _CursorTrackingProvider()
    user_id = uuid4()
    _set_key(monkeypatch)
    await store_connection_tokens(
        pool,
        user_id=user_id,
        provider=HealthProviderSlug.WITHINGS,
        tokens=HealthOAuthTokens(
            access_token="cursor-seed-token",
            refresh_token="cursor-seed-refresh",
            external_user_id="420001",
            granted_scopes=frozenset({"user.metrics"}),
        ),
        resource_types=[HealthResourceType.MEASUREMENT],
        now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
    )

    now = datetime(2026, 7, 20, 10, 30, tzinfo=UTC)
    summary = await reconcile_connections(
        pool=pool,
        repository=repository,
        provider=provider,
        claimed_by="health-reconcile-a",
        connection_limit=10,
        dirty_limit=0,
        now=now,
    )

    assert summary.scanned_connection_count == 1
    assert summary.skipped_connection_ids == ()
    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].status is HealthSyncStatus.COMPLETED
    assert provider.seen_cursors[0] == HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=now - timedelta(days=30),
    )


async def test_reconciliation_recovers_missed_measurement_updates_with_pagination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = FakePool()
    repository = repository_for(pool)
    initial_provider = FakeWithingsProvider()
    user_id = uuid4()
    await _store_valid_connection(
        pool=pool,
        monkeypatch=monkeypatch,
        provider=initial_provider,
        user_id=user_id,
        scopes=frozenset({"user.metrics"}),
    )

    first = await reconcile_connections(
        pool=pool,
        repository=repository,
        provider=initial_provider,
        claimed_by="health-reconcile-b",
        connection_limit=10,
        dirty_limit=0,
        now=datetime(2026, 7, 20, 11, 0, tzinfo=UTC),
    )

    assert len(first.outcomes) == 1
    assert first.outcomes[0].status is HealthSyncStatus.COMPLETED
    assert first.outcomes[0].page_count == 2
    assert len(pool.health_source_records) == 2

    revision_provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "measurements_revision"}
    )
    second = await reconcile_connections(
        pool=pool,
        repository=repository,
        provider=revision_provider,
        claimed_by="health-reconcile-c",
        connection_limit=10,
        dirty_limit=0,
        now=datetime(2026, 7, 20, 11, 10, tzinfo=UTC),
    )

    assert len(second.outcomes) == 1
    revised = next(
        row for row in pool.health_source_records.values() if row["external_id"] == "grpid:9001002"
    )
    assert revised["revision_count"] == 2
    assert revised["provider_revision"] == "1784513040"
    assert pool.health_dirty_categories == {}


async def test_reconciliation_skips_disconnected_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = FakeWithingsProvider()
    active_user_id = uuid4()
    disconnected_user_id = uuid4()
    _set_key(monkeypatch)

    active = await store_connection_tokens(
        pool,
        user_id=active_user_id,
        provider=HealthProviderSlug.WITHINGS,
        tokens=HealthOAuthTokens(
            access_token=(await provider.exchange_code(code="synthetic-auth-code-001", redirect_uri=CALLBACK_URL)).access_token,
            refresh_token="synthetic-refresh-token-active",
            external_user_id="420001",
            granted_scopes=frozenset({"user.metrics"}),
        ),
        resource_types=[HealthResourceType.MEASUREMENT],
        now=datetime(2026, 7, 20, 11, 20, tzinfo=UTC),
    )
    disconnected = await store_connection_tokens(
        pool,
        user_id=disconnected_user_id,
        provider=HealthProviderSlug.WITHINGS,
        tokens=HealthOAuthTokens(
            access_token=(await provider.exchange_code(code="synthetic-auth-code-001", redirect_uri=CALLBACK_URL)).access_token,
            refresh_token="synthetic-refresh-token-disconnected",
            external_user_id="420002",
            granted_scopes=frozenset({"user.metrics"}),
        ),
        resource_types=[HealthResourceType.MEASUREMENT],
        now=datetime(2026, 7, 20, 11, 21, tzinfo=UTC),
    )
    pool.health_connections[disconnected.connection_id]["status"] = "disconnected"
    pool.health_connections[disconnected.connection_id]["disconnected_at"] = datetime(
        2026, 7, 20, 11, 22, tzinfo=UTC
    )

    summary = await reconcile_connections(
        pool=pool,
        repository=repository,
        provider=provider,
        claimed_by="health-reconcile-d",
        connection_limit=10,
        dirty_limit=0,
        now=datetime(2026, 7, 20, 11, 30, tzinfo=UTC),
    )

    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].status is HealthSyncStatus.COMPLETED
    assert disconnected.connection_id in summary.skipped_connection_ids
    assert all(row["connection_id"] == active.connection_id for row in pool.health_source_records.values())
