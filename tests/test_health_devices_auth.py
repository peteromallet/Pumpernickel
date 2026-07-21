from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
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
    "WITHINGS_CALLBACK_URL": "https://veas-production.up.railway.app/api/health/devices/withings/oauth/callback",
    "LIVE_VOICE_JWT_SECRET": "test-live-secret",
}


class HealthPool:
    def __init__(self, user_id: UUID | None = None) -> None:
        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        self._user_id = user_id
        self._row = None
        self._deleted_source_record_count = 0
        self._dirty_categories: list[dict[str, Any]] = []
        if user_id is not None:
            self._row = {
                "id": uuid4(),
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
                "access_token_encrypted": encrypt_value("synthetic-access-token-auth"),
                "refresh_token_encrypted": encrypt_value("synthetic-refresh-token-auth"),
                "access_token_expires_at": None,
                "refresh_token_expires_at": None,
                "refresh_token_rotated_at": None,
                "provider": "withings",
                "user_id": user_id,
                "external_user_id": "420001",
                "consented_measurements_at": now,
                "consented_workouts_at": now,
                "consented_sleep_at": now,
                "last_error_detail": None,
                "created_at": now,
            }

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id, status, granted_scopes"):
            if self._row is None or args[0] != self._user_id or self._row["deleted_at"] is not None:
                return None
            return self._row
        if compact.startswith("SELECT id, user_id, provider, external_user_id, status, granted_scopes"):
            if self._row is None or args[0] != self._row["id"] or self._row["deleted_at"] is not None:
                return None
            return self._row
        if "SET status = 'disconnected'" in compact:
            if self._row is None or args[0] != self._row["id"] or self._row["deleted_at"] is not None:
                return None
            self._row["status"] = "disconnected"
            self._row["disconnected_at"] = datetime(2026, 7, 20, 12, 5, tzinfo=UTC)
            self._row["revoked_at"] = self._row["disconnected_at"]
            self._row["updated_at"] = self._row["disconnected_at"]
            self._row["access_token_encrypted"] = None
            self._row["refresh_token_encrypted"] = None
            self._row["access_token_expires_at"] = None
            self._row["refresh_token_expires_at"] = None
            return self._row
        if "SET status = 'deleted'" in compact:
            if self._row is None or args[0] != self._row["id"] or self._row["deleted_at"] is not None:
                return None
            self._row["status"] = "deleted"
            self._row["deleted_at"] = datetime(2026, 7, 20, 12, 6, tzinfo=UTC)
            self._row["revoked_at"] = self._row["deleted_at"]
            self._row["updated_at"] = self._row["deleted_at"]
            self._row["access_token_encrypted"] = None
            self._row["refresh_token_encrypted"] = None
            self._row["access_token_expires_at"] = None
            self._row["refresh_token_expires_at"] = None
            return self._row
        if compact.startswith("INSERT INTO mediator.health_dirty_categories"):
            connection_id, user_id, provider, resource_type, reason, source_receipt_id, marked_at = args
            if connection_id != self._row["id"]:
                raise AssertionError(f"Dirty marking for wrong connection: {connection_id}")
            existing = next(
                (
                    r
                    for r in self._dirty_categories
                    if r["connection_id"] == connection_id and r["resource_type"] == resource_type and r.get("cleared_at") is None
                ),
                None,
            )
            if existing is None:
                dirty_id = uuid4()
                dirty_row = {
                    "id": dirty_id,
                    "connection_id": connection_id,
                    "user_id": user_id,
                    "provider": provider,
                    "resource_type": resource_type,
                    "reason": reason,
                    "source_receipt_id": source_receipt_id,
                    "attempts": 0,
                    "marked_at": marked_at,
                    "claimed_at": None,
                    "claimed_by": None,
                    "cleared_at": None,
                }
                self._dirty_categories.append(dirty_row)
                return dict(dirty_row)
            existing["reason"] = reason
            existing["source_receipt_id"] = source_receipt_id
            existing["marked_at"] = max(existing["marked_at"], marked_at)
            existing["claimed_at"] = None
            existing["claimed_by"] = None
            return dict(existing)
        raise AssertionError(f"Unexpected SQL: {compact}")

    async def execute(self, sql: str, *args: Any) -> str:
        compact = " ".join(sql.split())
        if compact.startswith("DELETE FROM mediator.health_source_records"):
            if self._row is None or args[0] != self._row["id"]:
                return "DELETE 0"
            self._deleted_source_record_count = 1
            return "DELETE 1"
        # Silently accept all other DELETEs (repository-driven cleanup).
        if compact.startswith("DELETE FROM"):
            return "DELETE 0"
        raise AssertionError(f"Unexpected SQL: {compact}")

    @asynccontextmanager
    async def acquire(self):
        """Minimal acquire that yields self as the connection."""
        yield self

    @asynccontextmanager
    async def transaction(self):
        """Nested transaction passthrough."""
        yield


def _prime(monkeypatch: pytest.MonkeyPatch, *, auth_enabled: bool) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true" if auth_enabled else "false")
    monkeypatch.setenv("LIVE_VOICE_TEST_USER_ID", "00000000-0000-0000-0000-000000000099")
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()


def _client(pool: HealthPool) -> TestClient:
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


def test_routes_require_auth_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    client = _client(HealthPool())

    requests = [
        ("post", "/api/health/devices/withings/connect", {"json": {"redirect_uri": "https://app.example/return"}}),
        ("get", "/api/health/devices/withings/status", {}),
        ("post", "/api/health/devices/withings/resync", {}),
        ("post", "/api/health/devices/withings/disconnect", {}),
        ("delete", "/api/health/devices/withings", {}),
    ]

    for method, path, kwargs in requests:
        response = getattr(client, method)(path, **kwargs)
        assert response.status_code == 401, (method, path, response.text)


def test_authenticated_routes_return_metadata_only(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid4()
    _prime(monkeypatch, auth_enabled=True)
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    pool = HealthPool(user_id)
    client = _client(pool)

    connect = client.post(
        "/api/health/devices/withings/connect",
        headers=headers,
        json={"redirect_uri": "https://app.example/health/return"},
    )
    assert connect.status_code == 200, connect.text
    connect_body = connect.json()
    assert connect_body["provider"] == "withings"
    assert connect_body["status"] == "ready_for_oauth"
    assert "state=" in connect_body["authorization_url"]

    status = client.get("/api/health/devices/withings/status", headers=headers)
    assert status.status_code == 200, status.text
    assert status.json()["connection"]["status"] == "active"

    resync = client.post("/api/health/devices/withings/resync", headers=headers)
    assert resync.status_code == 200, resync.text
    resync_body = resync.json()
    assert resync_body["status"] == "accepted"
    assert resync_body["detail"] == "Resync queued for enabled categories."

    # Verify dirty categories were marked for each enabled resource type
    assert len(pool._dirty_categories) == 3
    dirty_resource_types = {d["resource_type"] for d in pool._dirty_categories}
    assert dirty_resource_types == {"measurement", "workout", "sleep"}
    for dirty in pool._dirty_categories:
        assert dirty["connection_id"] == pool._row["id"]
        assert dirty["user_id"] == user_id
        assert dirty["provider"] == "withings"
        assert dirty["reason"] == "manual"

    # Verify resync response is metadata-only: no cursor, tokens, raw payloads,
    # provider user ids, device ids, or health values
    resync_str = str(resync_body)
    for forbidden_key in (
        "access_token",
        "refresh_token",
        "oauth_code",
        "raw_payload",
        "cursor_state",
        "provider_user_id",
        "external_user_id",
        "device_id",
        "steps",
        "weight_kg",
        "heart_rate",
        "value_numeric",
        "sleep_score",
    ):
        assert forbidden_key not in resync_str, f"'{forbidden_key}' leaked into resync response: {resync_str}"

    disconnect = client.post("/api/health/devices/withings/disconnect", headers=headers)
    assert disconnect.status_code == 200, disconnect.text
    assert disconnect.json()["connection"]["status"] == "disconnected"
    assert disconnect.json()["connection"]["revoked_at"] is not None

    delete = client.delete("/api/health/devices/withings", headers=headers)
    assert delete.status_code == 200, delete.text
    assert delete.json()["connection"]["status"] == "deleted"

    combined = str(
        {
            "connect": connect_body,
            "status": status.json(),
            "resync": resync_body,
            "disconnect": disconnect.json(),
            "delete": delete.json(),
        }
    )
    for forbidden_key in ("access_token", "refresh_token", "oauth_code", "raw_payload", "cursor_state"):
        assert forbidden_key not in combined


def test_resync_returns_503_when_health_sync_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "false")
    get_settings.cache_clear()
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(HealthPool(user_id))

    response = client.post("/api/health/devices/withings/resync", headers=headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["provider"] == "withings"
    assert body["status"] == "unavailable"
    assert "detail" in body


def test_resync_returns_404_when_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(HealthPool())  # No user_id → no connection

    response = client.post("/api/health/devices/withings/resync", headers=headers)
    assert response.status_code == 404, response.text
    assert "Withings connection not found" in response.json()["detail"]


def test_resync_scopes_to_authenticated_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the authenticated user's active Withings connection is dirtied."""
    _prime(monkeypatch, auth_enabled=True)
    user_a = uuid4()
    user_b = uuid4()
    token_a = live_jwt.mint(user_id=str(user_a))
    pool = HealthPool(user_a)
    client = _client(pool)

    # User B's token should NOT match user A's connection
    token_b = live_jwt.mint(user_id=str(user_b))
    headers_b = {"Authorization": f"Bearer {token_b}"}
    response_b = client.post("/api/health/devices/withings/resync", headers=headers_b)
    assert response_b.status_code == 404, f"User B should not find User A's connection: {response_b.text}"

    # User A's token matches
    headers_a = {"Authorization": f"Bearer {token_a}"}
    response_a = client.post("/api/health/devices/withings/resync", headers=headers_a)
    assert response_a.status_code == 200, response_a.text
    body_a = response_a.json()
    assert body_a["status"] == "accepted"

    # All dirty categories belong to user A's connection
    assert len(pool._dirty_categories) == 3
    for dirty in pool._dirty_categories:
        assert dirty["user_id"] == user_a
        assert dirty["connection_id"] == pool._row["id"]


def test_resync_queues_only_resources_covered_by_granted_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    pool = HealthPool(user_id)
    assert pool._row is not None
    pool._row["granted_scopes"] = ["user.metrics"]
    token = live_jwt.mint(user_id=str(user_id))

    response = _client(pool).post(
        "/api/health/devices/withings/resync",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["resource_types"] == ["measurement"]
    assert [row["resource_type"] for row in pool._dirty_categories] == [
        "measurement"
    ]


def test_resync_response_has_required_metadata_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(HealthPool(user_id))

    response = client.post("/api/health/devices/withings/resync", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    # Top-level shape
    assert body["provider"] == "withings"
    assert body["status"] == "accepted"
    assert isinstance(body["detail"], str)
    assert isinstance(body["resource_types"], list)
    for rt in body["resource_types"]:
        assert rt in ("measurement", "workout", "sleep")

    # Connection metadata shape (no sensitive fields)
    conn = body["connection"]
    assert isinstance(conn["status"], str)
    assert isinstance(conn["granted_scopes"], list)
    assert isinstance(conn["updated_at"], str) or conn["updated_at"] is None

    # These fields must never appear in the response
    forbidden = (
        "cursor_state", "access_token", "refresh_token",
        "provider_user_id", "external_user_id",
        "device_id", "source_device_id",
        "steps", "weight_kg", "heart_rate", "value_numeric",
        "sleep_score", "raw_payload", "oauth_code",
    )
    body_str = str(body)
    for key in forbidden:
        assert key not in body_str, f"'{key}' found in resync response"


def test_dev_fallback_allows_health_routes_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=False)
    test_user_id = UUID("00000000-0000-0000-0000-000000000099")
    client = _client(HealthPool(test_user_id))

    response = client.get("/api/health/devices/withings/status")

    assert response.status_code == 200, response.text
    assert response.json()["connection"]["status"] == "active"
