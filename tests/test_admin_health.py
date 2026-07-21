"""Tests for the /admin/health operator diagnostics surface."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.admin import router
from tests.conftest import FakePool


def _client(monkeypatch, pool=None) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@localhost:5432/db")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "dummy-service-role")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-openai")
    monkeypatch.setenv("GROQ_API_KEY", "dummy-groq")
    monkeypatch.setenv("WHATSAPP_TOKEN", "dummy-whatsapp")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "12345")
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "dummy-verify")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "dummy-secret")
    monkeypatch.setenv("ADMIN_PASSWORD", "correct-password")
    monkeypatch.setenv("PARTNER_PHONE_A", "15555550100")
    monkeypatch.setenv("PARTNER_PHONE_B", "15555550101")
    get_settings.cache_clear()
    app = FastAPI()
    app.state.pool = pool or FakePool()
    app.include_router(router)
    return TestClient(app)


def _auth() -> tuple[str, str]:
    return ("admin", "correct-password")


# ── Authentication ───────────────────────────────────────────────────────────


def test_health_admin_requires_basic_auth(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin/health")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Basic realm="admin"'


def test_health_admin_rejects_wrong_credentials(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin/health", auth=("admin", "wrong-password"))
    assert response.status_code == 401


def test_health_admin_accepts_valid_credentials(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200


# ── Page structure ───────────────────────────────────────────────────────────


def test_health_page_has_title(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "Health Sync Diagnostics" in response.text


def test_health_page_shows_config_section(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "Configuration" in response.text
    assert "health_sync_enabled" in response.text
    assert "health_sync_measurements_enabled" in response.text
    assert "health_sync_workouts_enabled" in response.text
    assert "health_sync_sleep_enabled" in response.text
    assert "health_workout_projection_enabled" in response.text


def test_health_page_shows_summary_section(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "Summary" in response.text
    assert "total_connections" in response.text
    assert "fresh_connections" in response.text
    assert "stale_connections" in response.text
    assert "never_synced_connections" in response.text


def test_health_page_is_read_only(monkeypatch) -> None:
    client = _client(monkeypatch)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "<form" not in response.text.lower()


# ── Privacy: no health values, tokens, or sensitive data ────────────────────


def test_health_page_excludes_tokens(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        access_token_encrypted=b"secret-access-token",
        refresh_token_encrypted=b"secret-refresh-token",
        updated_at=now,
    )
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    # Token values must never appear
    assert "secret-access-token" not in response.text
    assert "secret-refresh-token" not in response.text
    # Column names for tokens must not appear
    assert "access_token_encrypted" not in response.text
    assert "refresh_token_encrypted" not in response.text


def test_health_page_excludes_provider_user_id(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        external_user_id="provider-user-999",
        updated_at=now,
    )
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "provider-user-999" not in response.text
    assert "external_user_id" not in response.text


def test_health_page_excludes_cursor_state(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        cursor_state={"last_modified": "2026-01-01T00:00:00Z"},
        updated_at=now,
    )
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "cursor_state" not in response.text
    assert "last_modified" not in response.text


def test_health_page_excludes_raw_payload_and_device_id(monkeypatch) -> None:
    """Verify that raw payload keys and device ids never leak into admin HTML."""
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(user_id=user_id, updated_at=now)
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "raw_payload" not in response.text
    assert "source_device_id" not in response.text
    assert "payload_hash" not in response.text


def test_health_page_excludes_health_values(monkeypatch) -> None:
    """Verify that measurement values (weight, sleep scores, etc.) never appear."""
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(user_id=user_id, updated_at=now)
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    # Health value keys/patterns that must never appear
    for forbidden in (
        "weight_kg",
        "value_numeric",
        "weight",
        "sleep_score",
        "heart_rate",
        "steps",
        "fat_ratio",
        "muscle_mass",
        "bone_mass",
        "spo2",
        "diastolic",
        "systolic",
    ):
        assert forbidden not in response.text.lower(), f"'{forbidden}' leaked into admin page"


# ── Stale classification ────────────────────────────────────────────────────


def test_health_page_classifies_fresh_connection(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        status="active",
        updated_at=now,
    )
    # Inject a fresh last_success_at
    conn_id = pool.health_connections_by_user_provider[(user_id, "withings")]
    pool.health_connections[conn_id]["last_success_at"] = now - timedelta(hours=1)
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "fresh" in response.text


def test_health_page_classifies_stale_connection(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        status="active",
        updated_at=now,
    )
    # Inject a stale last_success_at (>24h ago)
    conn_id = pool.health_connections_by_user_provider[(user_id, "withings")]
    pool.health_connections[conn_id]["last_success_at"] = now - timedelta(hours=25)
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "stale" in response.text


def test_health_page_classifies_never_synced_connection(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        status="active",
        updated_at=now,
    )
    # last_success_at stays None (default)
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "never_synced" in response.text


# ── Connection data integrity ───────────────────────────────────────────────


def test_health_page_shows_connection_id_and_user_id(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(user_id=user_id, updated_at=now)
    conn_id = pool.health_connections_by_user_provider[(user_id, "withings")]
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert str(conn_id) in response.text
    assert str(user_id) in response.text


def test_health_page_shows_enabled_categories_via_config(monkeypatch) -> None:
    monkeypatch.setenv("HEALTH_SYNC_MEASUREMENTS_ENABLED", "true")
    monkeypatch.setenv("HEALTH_SYNC_WORKOUTS_ENABLED", "false")
    monkeypatch.setenv("HEALTH_SYNC_SLEEP_ENABLED", "true")
    get_settings.cache_clear()
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(user_id=user_id, updated_at=now)
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    # Config values should be visible
    assert "True" in response.text
    assert "False" in response.text
    get_settings.cache_clear()


def test_health_page_escapes_html(monkeypatch) -> None:
    """Verify that any user-controlled data is HTML-escaped."""
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        status="<script>alert(1)</script>",
        updated_at=now,
    )
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in response.text


def test_health_page_handles_empty_connections(monkeypatch) -> None:
    pool = FakePool()
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "total_connections" in response.text
    # Should render with "No rows" for the connections table
    assert "No rows" in response.text


def test_health_page_excludes_oauth_expiry_and_rotation_timestamps(monkeypatch) -> None:
    """OAuth token expiry/rotation timestamps must not leak."""
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(user_id=user_id, updated_at=now)
    # Inject sensitive timestamps
    conn_id = pool.health_connections_by_user_provider[(user_id, "withings")]
    pool.health_connections[conn_id]["access_token_expires_at"] = now + timedelta(hours=1)
    pool.health_connections[conn_id]["refresh_token_expires_at"] = now + timedelta(days=90)
    pool.health_connections[conn_id]["refresh_token_rotated_at"] = now
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "access_token_expires_at" not in response.text
    assert "refresh_token_expires_at" not in response.text
    assert "refresh_token_rotated_at" not in response.text


def test_health_page_shows_status_and_granted_scopes(monkeypatch) -> None:
    pool = FakePool()
    user_id = uuid4()
    now = datetime.now(UTC)
    pool.seed_health_connection(
        user_id=user_id,
        status="active",
        granted_scopes=["user.metrics", "user.activity"],
        updated_at=now,
    )
    client = _client(monkeypatch, pool)
    response = client.get("/admin/health", auth=_auth())
    assert response.status_code == 200
    assert "active" in response.text
    assert "user.metrics" in response.text
    assert "user.activity" in response.text
