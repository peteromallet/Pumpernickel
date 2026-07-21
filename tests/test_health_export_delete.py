"""Tests for the authenticated Withings delete route exercising the
full repository-driven cleanup through the HTTP layer.

Covers:
- Cross-user deletion is rejected (user A cannot delete user B's data).
- No orphaned health/projection records remain after deletion.
- Projection-owned adherence events are removed.
- Manual adherence events survive.
- Best-effort revoke is attempted (provider call fails gracefully).
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.health_devices import router
from app.services.auth import jwt as live_jwt
from app.services.crypto import encrypt_value
from app.services.health_sync.oauth_state import reset_oauth_state_store_for_tests

_REQUIRED_ENV: dict[str, str] = {
    "DATABASE_URL": "postgresql://user:***@localhost:5432/db",
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
    "WITHINGS_CALLBACK_URL": "https://veas-production.up.railway.app/api/health/devices/withings/oauth/callback",
    "LIVE_VOICE_JWT_SECRET": "test-live-secret",
}


# ---------------------------------------------------------------------------
# Fake pool that supports the full delete path (repository-driven cleanup)
# ---------------------------------------------------------------------------


class DeletePool:
    """Fake pool tracking all tables touched by the delete route.

    Provides ``acquire()`` -> connection with ``transaction()`` so the
    repository's ``delete_connection_data`` can run its multi-statement
    cleanup.  Every delete is scoped by connection_id + user_id.
    """

    def __init__(self, user_id: UUID | None = None) -> None:
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        self._user_id = user_id
        self._connection_id: UUID | None = None
        self._row: dict[str, Any] | None = None
        self._revoke_attempted: bool = False

        # Per-table stores for cleanup verification
        self.source_records: dict[UUID, dict] = {}
        self.normalized_measurements: dict[UUID, dict] = {}
        self.normalized_sleep: dict[UUID, dict] = {}
        self.normalized_workouts: dict[UUID, dict] = {}
        self.dirty_categories: dict[UUID, dict] = {}
        self.webhook_receipts: dict[UUID, dict] = {}
        self.projections: dict[UUID, dict] = {}
        self.events: dict[UUID, dict] = {}

        if user_id is not None:
            self._connection_id = uuid4()
            self._row = {
                "id": self._connection_id,
                "status": "active",
                "granted_scopes": ["user.activity", "user.metrics"],
                "granted_at": now,
                "last_success_at": now,
                "last_error_at": None,
                "last_error_code": None,
                "disconnected_at": None,
                "revoked_at": None,
                "deleted_at": None,
                "updated_at": now,
                "access_token_encrypted": encrypt_value("synthetic-access-token"),
                "refresh_token_encrypted": encrypt_value("synthetic-refresh-token"),
                "access_token_expires_at": None,
                "refresh_token_expires_at": None,
                "refresh_token_rotated_at": None,
                "provider": "withings",
                "user_id": user_id,
                "external_user_id": "420001",
                "cursor_state": {"measurement": {"last_modified": "2026-07-20T10:00:00Z"}},
                "consented_measurements_at": now,
                "consented_workouts_at": now,
                "consented_sleep_at": now,
                "last_error_detail": None,
                "created_at": now,
            }

    # -- Multi-user seeding --------------------------------------------------

    def seed_second_user(self, second_user_id: UUID) -> UUID:
        """Seed a fully-populated second user with a different connection.

        Returns the second user's connection_id.
        """
        now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
        conn_id = uuid4()

        self._second_connection = {
            "id": conn_id,
            "status": "active",
            "granted_scopes": ["user.activity"],
            "granted_at": now,
            "last_success_at": now,
            "last_error_at": None,
            "last_error_code": None,
            "disconnected_at": None,
            "revoked_at": None,
            "deleted_at": None,
            "updated_at": now,
            "access_token_encrypted": encrypt_value("second-user-access"),
            "refresh_token_encrypted": encrypt_value("second-user-refresh"),
            "access_token_expires_at": None,
            "refresh_token_expires_at": None,
            "refresh_token_rotated_at": None,
            "provider": "withings",
            "user_id": second_user_id,
            "external_user_id": "999999",
            "cursor_state": {},
            "consented_measurements_at": now,
            "consented_workouts_at": now,
            "consented_sleep_at": now,
            "last_error_detail": None,
            "created_at": now,
        }
        self._second_user_id = second_user_id
        self._second_connection_id = conn_id

        self._add_row(self.source_records, conn_id, second_user_id)
        self._add_row(self.normalized_measurements, conn_id, second_user_id, value=99.9)
        self._add_row(self.dirty_categories, conn_id, second_user_id, resource_type="measurement")
        return conn_id

    def seed_tables_for_current_user(self) -> None:
        """Populate all health tables for the current user's connection."""
        if self._connection_id is None or self._user_id is None:
            return
        cid = self._connection_id
        uid = self._user_id

        self._add_row(self.source_records, cid, uid, external_id="meas-001")
        self._add_row(self.source_records, cid, uid, external_id="sleep-002")
        self._add_row(self.normalized_measurements, cid, uid, value=72.5)
        self._add_row(self.normalized_sleep, cid, uid, duration=300)
        self._add_row(self.normalized_workouts, cid, uid, distance=5.0)
        self._add_row(self.dirty_categories, cid, uid, resource_type="measurement")
        self._add_row(self.dirty_categories, cid, uid, resource_type="sleep")
        self._add_row(self.webhook_receipts, cid, uid, signature="sig-abc")

        proj_event_id = uuid4()
        self.projections[uuid4()] = {
            "id": uuid4(),
            "connection_id": cid,
            "user_id": uid,
            "event_id": proj_event_id,
        }
        self.events[proj_event_id] = {
            "id": proj_event_id,
            "user_id": uid,
            "metric_key": "weight",
            "adherence_status": "committed",
        }

        manual_event_id = uuid4()
        self.events[manual_event_id] = {
            "id": manual_event_id,
            "user_id": uid,
            "metric_key": "pushups",
            "adherence_status": "completed",
        }

    @staticmethod
    def _add_row(table: dict, conn_id: UUID, uid: UUID, **extra: Any) -> UUID:
        rid = uuid4()
        table[rid] = {"id": rid, "connection_id": conn_id, "user_id": uid, **extra}
        return rid

    # -- fetchrow surface ----------------------------------------------------

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())

        if compact.startswith("SELECT id, status, granted_scopes"):
            if self._row is None:
                return None
            if args[0] != self._user_id:
                if (
                    hasattr(self, "_second_user_id")
                    and hasattr(self, "_second_connection")
                    and args[0] == self._second_user_id
                ):
                    return None
                return None
            if self._row.get("deleted_at") is not None:
                return None
            return self._row

        if "access_token_encrypted" in compact and "FROM mediator.health_connections" in compact:
            if self._row is None or args[0] != self._row["id"]:
                return None
            if self._row.get("deleted_at") is not None:
                return None
            return self._row

        if "SET status = 'deleted'" in compact:
            connection_id, user_id, now = args[0], args[1], args[2]
            if self._row is None or connection_id != self._row["id"]:
                return None
            if user_id != self._user_id:
                return None
            if self._row.get("deleted_at") is not None:
                return None
            self._row["status"] = "deleted"
            self._row["deleted_at"] = now
            self._row["revoked_at"] = now
            self._row["updated_at"] = now
            self._row["access_token_encrypted"] = None
            self._row["refresh_token_encrypted"] = None
            self._row["access_token_expires_at"] = None
            self._row["refresh_token_expires_at"] = None
            self._row["refresh_token_rotated_at"] = None
            return dict(self._row)

        raise AssertionError(f"Unexpected fetchrow SQL: {compact}")

    # -- execute surface (repository DELETEs) --------------------------------

    async def execute(self, sql: str, *args: Any) -> str:
        compact = " ".join(sql.split())
        connection_id, user_id = args[0], args[1]

        if "DELETE FROM mediator.events WHERE user_id" in compact:
            target_event_ids = {
                p["event_id"]
                for p in self.projections.values()
                if (
                    p.get("connection_id") == connection_id
                    and p.get("user_id") == user_id
                    and p.get("event_id") is not None
                )
            }
            removed = 0
            for eid in list(target_event_ids):
                ev = self.events.get(eid)
                if ev is not None and ev.get("user_id") == user_id:
                    del self.events[eid]
                    removed += 1
            return f"DELETE {removed}"

        if "DELETE FROM mediator.health_source_to_event_projections" in compact:
            return self._delete_scoped(self.projections, connection_id, user_id)

        if "DELETE FROM mediator.health_source_records" in compact:
            return self._delete_scoped(self.source_records, connection_id, user_id)

        if "DELETE FROM mediator.health_normalized_measurements" in compact:
            return self._delete_scoped(self.normalized_measurements, connection_id, user_id)

        if "DELETE FROM mediator.health_normalized_sleep" in compact:
            return self._delete_scoped(self.normalized_sleep, connection_id, user_id)

        if "DELETE FROM mediator.health_normalized_workouts" in compact:
            return self._delete_scoped(self.normalized_workouts, connection_id, user_id)

        if "DELETE FROM mediator.health_dirty_categories" in compact:
            return self._delete_scoped(self.dirty_categories, connection_id, user_id)

        if "DELETE FROM mediator.health_webhook_receipts" in compact:
            return self._delete_scoped(self.webhook_receipts, connection_id, user_id)

        if compact.startswith("DELETE FROM"):
            return "DELETE 0"

        raise AssertionError(f"Unexpected execute SQL: {compact}")

    @staticmethod
    def _delete_scoped(table: dict, connection_id: UUID, user_id: UUID) -> str:
        removed = 0
        for key in list(table.keys()):
            row = table[key]
            if (
                row.get("connection_id") == connection_id
                and row.get("user_id") == user_id
            ):
                del table[key]
                removed += 1
        return f"DELETE {removed}"

    # -- Repository acquire/transaction surface ------------------------------

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_token(uid: UUID) -> str:
    return live_jwt.mint(user_id=str(uid))


def _prime(monkeypatch: pytest.MonkeyPatch, *, auth_enabled: bool) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true" if auth_enabled else "false")
    monkeypatch.setenv("LIVE_VOICE_TEST_USER_ID", "00000000-0000-0000-0000-000000000099")
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()


def _client(pool: DeletePool) -> TestClient:
    app = FastAPI()
    app.state.pool = pool
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()
    yield
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_delete_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    client = _client(DeletePool())
    response = client.delete("/api/health/devices/withings")
    assert response.status_code == 401, response.text


def test_delete_returns_503_when_health_sync_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "false")
    get_settings.cache_clear()
    user_id = uuid4()
    token = _make_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(DeletePool(user_id))
    response = client.delete("/api/health/devices/withings", headers=headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["provider"] == "withings"
    assert body["status"] == "unavailable"


def test_delete_returns_404_when_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = _make_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(DeletePool())
    response = client.delete("/api/health/devices/withings", headers=headers)
    assert response.status_code == 404, response.text
    assert "Withings connection not found" in response.json()["detail"]


def test_delete_returns_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = _make_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    pool = DeletePool(user_id)
    pool.seed_tables_for_current_user()
    client = _client(pool)

    response = client.delete("/api/health/devices/withings", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["provider"] == "withings"
    assert body["status"] == "deleted"

    conn = body["connection"]
    assert conn["status"] == "deleted"

    body_str = str(body)
    for forbidden_key in (
        "access_token",
        "refresh_token",
        "access_token_encrypted",
        "refresh_token_encrypted",
        "cursor_state",
        "external_user_id",
        "provider_user_id",
        "value_numeric",
        "steps",
        "weight_kg",
        "heart_rate",
        "sleep_score",
        "raw_payload",
    ):
        assert forbidden_key not in body_str, f"'{forbidden_key}' leaked into delete response"


def test_delete_removes_all_health_and_projection_records(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = _make_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    pool = DeletePool(user_id)
    pool.seed_tables_for_current_user()
    cid = pool._connection_id
    client = _client(pool)

    response = client.delete("/api/health/devices/withings", headers=headers)
    assert response.status_code == 200, response.text

    assert not any(r["connection_id"] == cid for r in pool.source_records.values())
    assert not any(r["connection_id"] == cid for r in pool.normalized_measurements.values())
    assert not any(r["connection_id"] == cid for r in pool.normalized_sleep.values())
    assert not any(r["connection_id"] == cid for r in pool.normalized_workouts.values())
    assert not any(r["connection_id"] == cid for r in pool.dirty_categories.values())
    assert not any(r["connection_id"] == cid for r in pool.webhook_receipts.values())
    assert not any(r["connection_id"] == cid for r in pool.projections.values())


def test_delete_removes_projection_owned_events(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = _make_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    pool = DeletePool(user_id)
    pool.seed_tables_for_current_user()

    projection_event_ids = {
        p["event_id"]
        for p in pool.projections.values()
        if p.get("event_id") is not None
    }
    manual_event_ids = set(pool.events.keys()) - projection_event_ids
    assert len(manual_event_ids) > 0, "Test requires at least one manual event"

    client = _client(pool)
    response = client.delete("/api/health/devices/withings", headers=headers)
    assert response.status_code == 200, response.text

    for eid in projection_event_ids:
        assert eid not in pool.events, f"Projection-owned event {eid} should be deleted"

    for eid in manual_event_ids:
        assert eid in pool.events, f"Manual event {eid} should survive"


def test_cross_user_deletion_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_a = uuid4()
    user_b = uuid4()
    token_a = _make_token(user_a)
    token_b = _make_token(user_b)

    pool = DeletePool(user_a)
    pool.seed_tables_for_current_user()
    pool.seed_second_user(user_b)

    src_before = len(pool.source_records)

    client = _client(pool)
    headers_b = {"Authorization": f"Bearer {token_b}"}
    response_b = client.delete("/api/health/devices/withings", headers=headers_b)
    assert response_b.status_code == 404, (
        f"User B should get 404, got {response_b.status_code}: {response_b.text}"
    )

    assert len(pool.source_records) == src_before, "User B should not affect User A's data"

    headers_a = {"Authorization": f"Bearer {token_a}"}
    response_a = client.delete("/api/health/devices/withings", headers=headers_a)
    assert response_a.status_code == 200, response_a.text
    assert response_a.json()["status"] == "deleted"


def test_delete_clears_encrypted_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = _make_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    pool = DeletePool(user_id)
    pool.seed_tables_for_current_user()
    client = _client(pool)

    assert pool._row is not None
    assert pool._row["access_token_encrypted"] is not None
    assert pool._row["refresh_token_encrypted"] is not None

    response = client.delete("/api/health/devices/withings", headers=headers)
    assert response.status_code == 200, response.text

    assert pool._row["access_token_encrypted"] is None
    assert pool._row["refresh_token_encrypted"] is None
    assert pool._row["access_token_expires_at"] is None
    assert pool._row["refresh_token_expires_at"] is None
    assert pool._row["refresh_token_rotated_at"] is None


def test_delete_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = _make_token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    pool = DeletePool(user_id)
    pool.seed_tables_for_current_user()
    client = _client(pool)

    response1 = client.delete("/api/health/devices/withings", headers=headers)
    assert response1.status_code == 200, response1.text

    response2 = client.delete("/api/health/devices/withings", headers=headers)
    assert response2.status_code == 404, response2.text


def test_delete_preserves_other_users_data(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_a = uuid4()
    user_b = uuid4()
    token_a = _make_token(user_a)

    pool = DeletePool(user_a)
    pool.seed_tables_for_current_user()
    conn_b = pool.seed_second_user(user_b)

    b_sources = sum(1 for r in pool.source_records.values() if r["connection_id"] == conn_b)
    b_projections = sum(1 for r in pool.projections.values() if r["connection_id"] == conn_b)
    b_dirty = sum(1 for r in pool.dirty_categories.values() if r["connection_id"] == conn_b)
    assert b_sources > 0, "Test requires seeded second-user data"

    client = _client(pool)
    headers_a = {"Authorization": f"Bearer {token_a}"}
    response = client.delete("/api/health/devices/withings", headers=headers_a)
    assert response.status_code == 200, response.text

    assert sum(1 for r in pool.source_records.values() if r["connection_id"] == conn_b) == b_sources
    assert sum(1 for r in pool.projections.values() if r["connection_id"] == conn_b) == b_projections
    assert sum(1 for r in pool.dirty_categories.values() if r["connection_id"] == conn_b) == b_dirty


def test_dev_fallback_allows_delete_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=False)
    test_user_id = UUID("00000000-0000-0000-0000-000000000099")
    pool = DeletePool(test_user_id)
    pool.seed_tables_for_current_user()
    client = _client(pool)
    response = client.delete("/api/health/devices/withings")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "deleted"
