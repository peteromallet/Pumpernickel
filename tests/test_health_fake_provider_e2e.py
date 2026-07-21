from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
import logging
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.health_devices import router as health_devices_router
from app.routers.withings import router as withings_router
from app.services import crypto
from app.services.auth import jwt as live_jwt
from app.services.health_sync import (
    FakeWithingsProvider,
    HealthResourceType,
    HealthSyncStatus,
    HealthSyncWorker,
    load_connection_tokens,
    reconcile_connections,
    repository_for,
    reset_oauth_state_store_for_tests,
)
from tests.conftest import FakePool


_REQUIRED_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
    "ANTHROPIC_API_KEY": "dummy-anthropic",
    "OPENAI_API_KEY": "dummy-openai",
    "GROQ_API_KEY": "dummy-groq",
    "WHATSAPP_TOKEN": "dummy-whatsapp",
    "WHATSAPP_VERIFY_TOKEN": "dummy-verify",
    "ADMIN_PASSWORD": "dummy-admin",
    "DATA_ENCRYPTION_KEY": base64.b64encode(b"0123456789abcdef0123456789abcdef").decode(),
    "HEALTH_SYNC_ENABLED": "true",
    "HEALTH_SYNC_MEASUREMENTS_ENABLED": "true",
    "HEALTH_SYNC_WORKOUTS_ENABLED": "true",
    "HEALTH_SYNC_SLEEP_ENABLED": "true",
    "WITHINGS_CLIENT_ID": "dummy-client-id",
    "WITHINGS_CLIENT_SECRET": "dummy-client-secret",
    "WITHINGS_CALLBACK_URL": "https://example.test/api/health/devices/withings/oauth/callback",
    "LIVE_VOICE_AUTH_ENABLED": "true",
    "LIVE_VOICE_JWT_SECRET": "test-live-secret",
}


class TrackingFakeWithingsProvider(FakeWithingsProvider):
    def __init__(self, *, token_issued_at: datetime) -> None:
        super().__init__(token_issued_at=token_issued_at)
        self.fetch_history: list[tuple[HealthResourceType, object | None]] = []
        self.revoke_calls: list[tuple[str, str | None]] = []

    async def fetch_changes(
        self,
        *,
        access_token: str,
        resource_type: HealthResourceType,
        cursor,
    ):
        self.fetch_history.append((resource_type, cursor))
        return await super().fetch_changes(
            access_token=access_token,
            resource_type=resource_type,
            cursor=cursor,
        )

    async def revoke(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
    ) -> None:
        self.revoke_calls.append((access_token, refresh_token))
        await super().revoke(access_token=access_token, refresh_token=refresh_token)


def _prime(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    crypto.reset_cache_for_tests()
    reset_oauth_state_store_for_tests()


def _client(pool: FakePool, provider: TrackingFakeWithingsProvider) -> TestClient:
    app = FastAPI()
    app.state.pool = pool
    app.state.health_withings_provider = provider
    app.include_router(health_devices_router)
    app.include_router(withings_router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    crypto.reset_cache_for_tests()
    reset_oauth_state_store_for_tests()
    yield
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    crypto.reset_cache_for_tests()
    reset_oauth_state_store_for_tests()


@pytest.mark.asyncio
async def test_fake_provider_e2e_covers_backfill_dirty_overlap_tombstone_reconcile_and_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _prime(monkeypatch)
    caplog.set_level(logging.WARNING)
    pool = FakePool()
    repository = repository_for(pool)
    provider = TrackingFakeWithingsProvider(
        token_issued_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC)
    )
    client = _client(pool, provider)
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {live_jwt.mint(user_id=str(user_id))}"}

    connect = client.post(
        "/api/health/devices/withings/connect",
        headers=headers,
        json={"redirect_uri": "https://app.example/health/return"},
    )
    assert connect.status_code == 200, connect.text
    connect_body = connect.json()
    state = parse_qs(urlsplit(connect_body["authorization_url"]).query)["state"][0]
    assert connect_body["resource_types"] == ["measurement", "workout", "sleep"]
    assert connect_body["required_scopes"] == ["user.activity", "user.metrics"]

    callback = client.get(
        "/api/health/devices/withings/oauth/callback",
        params={"code": "synthetic-auth-code-001", "state": state},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert callback.headers["location"] == "https://app.example/health/return"

    status = client.get("/api/health/devices/withings/status", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["connection"]["status"] == "active"

    connection_id = next(iter(pool.health_connections))
    stored_tokens = await load_connection_tokens(pool, connection_id=connection_id)
    assert stored_tokens.external_user_id == "420001"

    backfill_at = datetime(2026, 7, 20, 11, 0, tzinfo=UTC)
    initial = await reconcile_connections(
        pool=pool,
        repository=repository,
        provider=provider,
        claimed_by="health-e2e-backfill",
        connection_limit=10,
        dirty_limit=0,
        now=backfill_at,
    )

    assert initial.scanned_connection_count == 1
    assert initial.skipped_connection_ids == ()
    assert len(initial.outcomes) == 3
    assert all(outcome.status is HealthSyncStatus.COMPLETED for outcome in initial.outcomes)
    assert len(pool.health_source_records) == 5
    assert len(pool.health_source_records_by_key) == len(pool.health_source_records)
    assert {
        row["resource_type"] for row in pool.health_source_records.values()
    } == {"measurement", "sleep", "workout"}

    initial_seed = backfill_at - timedelta(days=30)
    measurement_backfill = next(
        outcome for outcome in initial.outcomes if outcome.resource_type is HealthResourceType.MEASUREMENT
    )
    measurement_cursors = [
        cursor for resource_type, cursor in provider.fetch_history if resource_type is HealthResourceType.MEASUREMENT
    ]
    assert measurement_backfill.page_count == 2
    assert measurement_backfill.fetched_count == 2
    assert measurement_cursors[0].last_modified == initial_seed
    assert measurement_cursors[1].last_modified == initial_seed
    assert measurement_cursors[1].page_offset == 100
    assert any(
        resource_type is HealthResourceType.WORKOUT and cursor.last_modified == initial_seed
        for resource_type, cursor in provider.fetch_history
    )
    assert any(
        resource_type is HealthResourceType.SLEEP and cursor.last_modified == initial_seed
        for resource_type, cursor in provider.fetch_history
    )

    notification = client.post(
        "/api/health/devices/withings/notifications",
        data={"userid": "420001", "appli": "1", "date": "1721472000"},
    )
    assert notification.status_code == 200, notification.text
    assert len(pool.health_dirty_categories) == 1
    dirty = next(iter(pool.health_dirty_categories.values()))
    assert dirty["resource_type"] == "measurement"

    provider.fetch_history.clear()
    provider._fetch_scenarios[HealthResourceType.MEASUREMENT] = "measurements_revision"
    worker = HealthSyncWorker(
        pool,
        settings=get_settings(),
        provider=provider,
        repository=repository,
        worker_id="health-e2e-worker",
    )
    worker._last_reconciliation_at = backfill_at

    changed_at = datetime(2026, 7, 20, 11, 10, tzinfo=UTC)
    changed = await worker.run_once(now=changed_at)

    assert changed.claimed == 1
    assert changed.synced == 1
    assert changed.failed == 0
    assert changed.reconciliation_outcomes == 0
    overlap_cursor = provider.fetch_history[-1][1]
    assert overlap_cursor.last_modified == measurement_backfill.cursor_after.last_modified - timedelta(hours=48)
    revised = next(
        row for row in pool.health_source_records.values() if row["external_id"] == "grpid:9001002"
    )
    assert revised["revision_count"] == 2
    assert revised["provider_revision"] == "1784513040"
    assert revised["is_deleted"] is False
    assert len(pool.health_source_records_by_key) == len(pool.health_source_records)

    second_notification = client.post(
        "/api/health/devices/withings/notifications",
        data={"userid": "420001", "appli": "1", "date": "1721472060"},
    )
    assert second_notification.status_code == 200, second_notification.text

    provider.fetch_history.clear()
    provider._fetch_scenarios[HealthResourceType.MEASUREMENT] = "measurements_tombstones"
    tombstoned_at = datetime(2026, 7, 20, 11, 20, tzinfo=UTC)
    tombstoned = await worker.run_once(now=tombstoned_at)

    assert tombstoned.claimed == 1
    assert tombstoned.synced == 1
    deleted_record = next(
        row for row in pool.health_source_records.values() if row["external_id"] == "grpid:9001002"
    )
    assert deleted_record["revision_count"] == 3
    assert deleted_record["provider_revision"] == "synthetic-delete-rev-1"
    assert deleted_record["is_deleted"] is True
    assert len(pool.health_source_records) == 5
    assert len(pool.health_source_records_by_key) == len(pool.health_source_records)

    before_reconcile = {
        row["external_id"]: (row["revision_count"], row["provider_revision"], row["is_deleted"])
        for row in pool.health_source_records.values()
    }
    provider.fetch_history.clear()
    provider._fetch_scenarios[HealthResourceType.MEASUREMENT] = "measurements_tombstones"
    no_diff = await reconcile_connections(
        pool=pool,
        repository=repository,
        provider=provider,
        claimed_by="health-e2e-reconcile",
        connection_limit=10,
        dirty_limit=0,
        now=datetime(2026, 7, 20, 12, 30, tzinfo=UTC),
    )

    assert len(no_diff.outcomes) == 3
    assert all(outcome.status is HealthSyncStatus.COMPLETED for outcome in no_diff.outcomes)
    assert {
        row["external_id"]: (row["revision_count"], row["provider_revision"], row["is_deleted"])
        for row in pool.health_source_records.values()
    } == before_reconcile
    assert len(pool.health_source_records_by_key) == len(pool.health_source_records)

    disconnect = client.post("/api/health/devices/withings/disconnect", headers=headers)
    assert disconnect.status_code == 200, disconnect.text
    assert disconnect.json()["connection"]["status"] == "disconnected"
    assert disconnect.json()["connection"]["revoked_at"] is not None
    assert provider.revoke_calls == [(stored_tokens.access_token or "", stored_tokens.refresh_token)]
    assert pool.health_connections[connection_id]["status"] == "disconnected"
    assert pool.health_connections[connection_id]["access_token_encrypted"] is None
    assert pool.health_connections[connection_id]["refresh_token_encrypted"] is None

    delete = client.delete("/api/health/devices/withings", headers=headers)
    assert delete.status_code == 200, delete.text
    assert delete.json()["connection"]["status"] == "deleted"
    assert pool.health_connections[connection_id]["status"] == "deleted"
    assert pool.health_source_records == {}
    assert pool.health_source_records_by_key == {}

    captured = " ".join(record.getMessage() for record in caplog.records)
    combined = " ".join(
        [
            connect.text,
            callback.text,
            status.text,
            notification.text,
            second_notification.text,
            disconnect.text,
            delete.text,
            captured,
        ]
    )
    for forbidden in (
        "synthetic-access-token-v1",
        "synthetic-refresh-token-v1",
        "synthetic-auth-code-001",
        "70540",
        "70420",
        "userid=420001",
        "snoring",
    ):
        assert forbidden not in combined
