from __future__ import annotations

import base64
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.withings import router
from app.services.health_sync.fake_withings import FakeWithingsProvider
from app.services.health_sync.oauth_state import OAuthStateStore, reset_oauth_state_store_for_tests
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
    "WITHINGS_CLIENT_ID": "dummy-client-id",
    "WITHINGS_CLIENT_SECRET": "dummy-client-secret",
    "WITHINGS_CALLBACK_URL": "https://example.test/api/health/devices/withings/oauth/callback",
}


def _prime(monkeypatch: pytest.MonkeyPatch) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()
    reset_oauth_state_store_for_tests()


def _client(
    *,
    pool: FakePool | None = None,
    state_store: OAuthStateStore | None = None,
    provider: object | None = None,
) -> TestClient:
    app = FastAPI()
    app.state.pool = pool or FakePool()
    if state_store is not None:
        app.state.health_oauth_state_store = state_store
    if provider is not None:
        app.state.health_withings_provider = provider
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    get_settings.cache_clear()
    reset_oauth_state_store_for_tests()
    yield
    get_settings.cache_clear()
    reset_oauth_state_store_for_tests()


def test_head_endpoints_return_exact_200(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch)
    client = _client()

    callback = client.head("/api/health/devices/withings/oauth/callback")
    notifications = client.head("/api/health/devices/withings/notifications")

    assert callback.status_code == 200
    assert callback.text == ""
    assert notifications.status_code == 200
    assert notifications.text == ""


def test_invalid_callback_stays_static_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime(monkeypatch)
    client = _client()

    response = client.get(
        "/api/health/devices/withings/oauth/callback",
        params={
            "code": "secret-auth-code-12345",
            "state": "csrf-state-token-67890",
            "error_description": "User declined",
        },
    )

    assert response.status_code == 400
    body = response.json()
    assert body == {
        "status": "invalid_request",
        "detail": "Invalid Withings callback.",
    }
    assert "secret-auth-code-12345" not in str(body)
    assert "csrf-state-token-67890" not in str(body)
    assert "User declined" not in str(body)


def test_successful_callback_validates_state_and_persists_encrypted_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime(monkeypatch)
    pool = FakePool()
    state_store = OAuthStateStore(signing_secret=b"test-oauth-state-secret")
    redirect_uri = "https://app.example/health/return"
    user_id = uuid4()
    issued = state_store.issue(user_id=user_id, redirect_uri=redirect_uri)
    client = _client(
        pool=pool,
        state_store=state_store,
        provider=FakeWithingsProvider(),
    )

    response = client.get(
        "/api/health/devices/withings/oauth/callback",
        params={"code": "synthetic-auth-code-001", "state": issued.state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == redirect_uri
    assert len(pool.health_connections) == 1
    persisted = next(iter(pool.health_connections.values()))
    assert persisted["user_id"] == user_id
    assert persisted["provider"] == "withings"
    assert persisted["external_user_id"] == "420001"
    assert persisted["access_token_encrypted"].startswith(b"AGV1")
    assert persisted["refresh_token_encrypted"].startswith(b"AGV1")
    assert b"synthetic-access-token-v1" not in persisted["access_token_encrypted"]
    assert b"synthetic-refresh-token-v1" not in persisted["refresh_token_encrypted"]

    replay = client.get(
        "/api/health/devices/withings/oauth/callback",
        params={"code": "synthetic-auth-code-001", "state": issued.state},
    )
    assert replay.status_code == 400


def test_callback_rejects_non_allowlisted_redirect_uri(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime(monkeypatch)
    pool = FakePool()
    state_store = OAuthStateStore(signing_secret=b"test-oauth-state-secret")
    issued = state_store.issue(
        user_id=uuid4(),
        redirect_uri="http://evil.example/capture",
    )
    client = _client(
        pool=pool,
        state_store=state_store,
        provider=FakeWithingsProvider(),
    )

    response = client.get(
        "/api/health/devices/withings/oauth/callback",
        params={"code": "synthetic-auth-code-001", "state": issued.state},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Invalid Withings callback."
    assert pool.health_connections == {}


def test_notifications_route_validates_and_marks_dirty_without_fetching_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime(monkeypatch)
    pool = FakePool()
    user_id = uuid4()
    pool.seed_health_connection(
        user_id=user_id,
        external_user_id="420001",
    )
    client = _client(pool=pool)

    response = client.post(
        "/api/health/devices/withings/notifications",
        data={"userid": "420001", "appli": "1", "date": "1721472000"},
    )

    assert response.status_code == 200
    assert response.text == ""
    assert len(pool.health_webhook_receipts) == 1
    receipt = next(iter(pool.health_webhook_receipts.values()))
    assert receipt["status"] == "queued"
    assert receipt["connection_id"] is not None
    assert len(pool.health_dirty_categories) == 1
    dirty = next(iter(pool.health_dirty_categories.values()))
    assert dirty["resource_type"] == "measurement"
    assert dirty["source_receipt_id"] == receipt["id"]


def test_invalid_notification_remains_static_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _prime(monkeypatch)
    client = _client()

    response = client.post(
        "/api/health/devices/withings/notifications",
        json={"userid": "420001", "appli": "1", "deviceid": "secret-device-id"},
    )

    assert response.status_code == 415
    body = response.json()
    assert body == {
        "status": "unsupported_media_type",
        "detail": "Unsupported notification content type.",
    }
    assert "420001" not in str(body)
    assert "secret-device-id" not in str(body)
