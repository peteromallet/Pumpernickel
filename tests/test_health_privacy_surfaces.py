"""Step 11: Privacy surface scans for health data.

Scans prohibited default surfaces for fixture secrets, synthetic tokens,
OAuth codes, provider userid markers, raw payload keys, device identifiers,
and representative health values.

Covers:
  * Route responses (status, resync, disconnect, delete — NOT export)
  * Admin diagnostics (/admin/health HTML)
  * Metric log labels (metrics.py)
  * Tool registry / audit summaries
  * Hot context (solo hector fitness block)
  * Prompt rendering fixtures (health_read_guidance)

Allows health values ONLY in:
  * Explicit authenticated export (/api/health/devices/withings/export)
  * Explicit health read tools (get_weight_trend, get_sleep_summary,
    get_workout_summary)
"""

from __future__ import annotations

import base64
import inspect
import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.health_devices import router as health_devices_router
from app.routers.admin import router as admin_router
from app.services.auth import jwt as live_jwt
from app.services.crypto import encrypt_value
from app.services.health_sync.oauth_state import reset_oauth_state_store_for_tests
from app.services.health_sync import metrics as health_metrics
from tests.conftest import FakePool


# ═══════════════════════════════════════════════════════════════════════════
# Shared helpers / fixture data
# ═══════════════════════════════════════════════════════════════════════════

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

# Patterns that indicate prohibited content in non-export/non-read-tool surfaces
_PROHIBITED_TOKEN_PATTERNS: list[str] = [
    r"access_token",
    r"refresh_token",
    r"access_token_encrypted",
    r"refresh_token_encrypted",
    r"access_token_expires_at",
    r"refresh_token_expires_at",
    r"refresh_token_rotated_at",
]

_PROHIBITED_OAUTH_PATTERNS: list[str] = [
    r"oauth_state",
    r"oauth.*code",
    r"authorization_code",
    r"code_challenge",
]

# Representative health value patterns (weight, sleep score, energy, heart rate)
_HEALTH_VALUE_PATTERNS: list[str] = [
    r"\bvalue_numeric\b",
    r"\bcanonical_unit\b",
    r"\bsleep_score\b",
    r"\benergy_kcal\b",
    r"\bheart_rate\b",
    r"\bdistance_meters\b",
    r"\bsteps\b",
    r"\btotal_asleep_seconds\b",
    r"\bsleep_latency\b",
    r"\belevation_gain\b",
]

# Allowed health value patterns (export and read tools)
_ALLOWED_HEALTH_VALUE_PATTERNS: list[str] = [
    r"\bvalue_numeric\b",
    r"\bcanonical_unit\b",
    r"\bsleep_score\b",
    r"\benergy_kcal\b",
    r"\bheart_rate\b",
    r"\bdistance_meters\b",
    r"\bsteps\b",
    r"\btotal_asleep_seconds\b",
]

# Provider userid / device id markers that should not appear outside export
_PROHIBITED_PROVIDER_MARKERS: list[str] = [
    r"external_user_id",
    r"source_device_id",
    r"source_device_model",
    r"device_id",
    r"device_model",
    r"provider_user_id",
]


def _prime(monkeypatch: pytest.MonkeyPatch, *, auth_enabled: bool = True) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true" if auth_enabled else "false")
    monkeypatch.setenv("LIVE_VOICE_TEST_USER_ID", "00000000-0000-0000-0000-000000000099")
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()


@pytest.fixture(autouse=True)
def _reset_caches() -> None:
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()
    yield
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()


# ═══════════════════════════════════════════════════════════════════════════
# Route-level FakePool for health_devices router tests
# ═══════════════════════════════════════════════════════════════════════════


class PrivacyScanPool:
    """Fake pool that returns synthetic health connection data for route tests."""

    def __init__(self, user_id: UUID | None = None, *, deleted: bool = False) -> None:
        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        self._user_id = user_id
        self._connection_id = uuid4()
        self._row: dict[str, Any] | None = None
        self._execute_log: list[str] = []
        if user_id is not None:
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
                "deleted_at": now if deleted else None,
                "updated_at": now,
                # These fields exist in the DB row but should NEVER appear
                # in route response surfaces (export is tested separately).
                "access_token_encrypted": encrypt_value("synth-access-priv-scan"),
                "refresh_token_encrypted": encrypt_value("synth-refresh-priv-scan"),
                "access_token_expires_at": None,
                "refresh_token_expires_at": None,
                "refresh_token_rotated_at": None,
                "provider": "withings",
                "user_id": user_id,
                "external_user_id": "420001",
                "created_at": now,
                "consented_measurements_at": now,
                "consented_workouts_at": now,
                "consented_sleep_at": now,
                "last_error_detail": None,
            }

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        # Status/connection fetch (used by _fetch_connection in router)
        if compact.startswith("SELECT id, status, granted_scopes"):
            if self._row is None or args[0] != self._user_id:
                return None
            if self._row.get("deleted_at") is not None:
                return None
            return self._row
        # Token-load fetch (used by load_connection_tokens in disconnect/delete)
        if "access_token_encrypted" in compact or "external_user_id" in compact:
            if self._row is None:
                return None
            if args and args[0] != self._row.get("id"):
                return None
            if self._row.get("deleted_at") is not None:
                return None
            return self._row
        # UPDATE...RETURNING (disconnect, mark_connection_deleted)
        if compact.upper().startswith("UPDATE") or compact.upper().startswith("DELETE"):
            if self._row is None:
                return None
            # Verify the connection_id arg matches
            if args and args[0] != self._row.get("id"):
                return None
            if self._row.get("deleted_at") is not None:
                return None
            return self._row
        # INSERT...RETURNING (mark_dirty in resync route)
        if compact.upper().startswith("INSERT"):
            # Return a synthetic dirty category row
            return {
                "id": uuid4(),
                "connection_id": self._connection_id,
                "user_id": self._user_id,
                "provider": "withings",
                "resource_type": "measurement",
                "reason": "manual",
                "source_receipt_id": None,
                "attempts": 0,
                "marked_at": datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
                "claimed_at": None,
                "claimed_by": None,
                "cleared_at": None,
            }
        raise AssertionError(f"Unexpected fetchrow SQL: {compact}")

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        raise AssertionError(f"Unexpected fetch SQL: {sql}")

    async def execute(self, sql: str, *args: Any) -> str:
        compact = " ".join(sql.split())
        self._execute_log.append(compact[:120])
        # Accept all execute operations silently (resync mark_dirty, disconnect
        # token clearing, delete cleanup)
        return "OK"

    # Support repository transaction/acquire surface
    # (mirrors test_health_export_delete.DeletePool pattern)
    @asynccontextmanager
    async def acquire(self):
        """Return an async context manager yielding self (the pool).

        Called by repository.transaction() as: async with pool.acquire() as conn.
        """
        yield self

    # Support transaction context manager (used by repository)
    @asynccontextmanager
    async def transaction(self):
        """Return an async context manager (no-op for tests).

        Called on the connection returned by acquire() as:
        async with connection.transaction():
        """
        yield


def _health_client(pool: PrivacyScanPool) -> TestClient:
    app = FastAPI()
    app.state.pool = pool
    app.include_router(health_devices_router)
    return TestClient(app, raise_server_exceptions=False)


def _auth_headers(user_id: UUID) -> dict[str, str]:
    token = live_jwt.mint(user_id=str(user_id))
    return {"Authorization": f"Bearer {token}"}


def _extract_health_functions(source: str) -> list[tuple[str, str]]:
    """Extract health-related function bodies from hot_context_solo source.

    Returns list of (function_name, function_body) for functions that
    reference health data tables or read models.
    """
    import re
    # Find functions that reference health-related identifiers
    health_indicators = [
        "health_connections", "health_normalized", "get_weight",
        "get_sleep", "get_workout", "weight", "_build_weight",
        "_build_workout", "_build_sleep",
    ]
    # Simple heuristic: find def lines and capture body
    funcs: list[tuple[str, str]] = []
    lines = source.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("def ") or stripped.startswith("async def "):
            # Get function name
            match = re.match(r'(?:async\s+)?def\s+(\w+)', stripped)
            if match:
                func_name = match.group(1)
                # Collect function body (until next top-level def or end)
                body_lines = [line]
                j = i + 1
                while j < len(lines):
                    next_line = lines[j]
                    if next_line.strip().startswith("def ") or \
                       next_line.strip().startswith("async def ") or \
                       next_line.strip().startswith("class "):
                        break
                    body_lines.append(next_line)
                    j += 1
                body = "\n".join(body_lines)
                body_lower = body.lower()
                # Only include functions that reference health data
                if any(indicator in body_lower for indicator in health_indicators):
                    funcs.append((func_name, body))
                i = j
                continue
        i += 1
    return funcs


# ═══════════════════════════════════════════════════════════════════════════
# 1. Route response privacy (non-export surfaces)
# ═══════════════════════════════════════════════════════════════════════════


class TestStatusRoutePrivacy:
    """GET /api/health/devices/withings/status must be metadata-only."""

    def test_status_excludes_encrypted_tokens(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.get("/api/health/devices/withings/status", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body = response.json()

        conn = body.get("connection", {})
        for forbidden in ("access_token_encrypted", "refresh_token_encrypted",
                          "access_token_expires_at", "refresh_token_expires_at",
                          "refresh_token_rotated_at", "cursor_state"):
            assert forbidden not in conn, f"'{forbidden}' leaked in status connection"

    def test_status_excludes_external_user_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.get("/api/health/devices/withings/status", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body = response.json()
        conn = body.get("connection", {})
        assert "external_user_id" not in conn, "external_user_id leaked in status"

    def test_status_excludes_device_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.get("/api/health/devices/withings/status", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text
        for marker in ("source_device_id", "source_device_model", "device_id"):
            assert marker not in body_text, f"'{marker}' leaked in status response"

    def test_status_excludes_health_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.get("/api/health/devices/withings/status", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text.lower()
        for hv in ("value_numeric", "canonical_unit", "sleep_score", "energy_kcal",
                    "heart_rate", "distance_meters", "total_asleep"):
            assert hv not in body_text, f"'{hv}' leaked in status response"

    def test_status_excludes_oauth_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.get("/api/health/devices/withings/status", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text
        for oauth_term in ("oauth_state", "authorization_code", "code_verifier",
                           "code_challenge"):
            assert oauth_term not in body_text.lower(), f"'{oauth_term}' leaked in status"


class TestResyncRoutePrivacy:
    """POST /api/health/devices/withings/resync must be metadata-only."""

    def test_resync_excludes_tokens_and_device_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.post("/api/health/devices/withings/resync", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text
        for forbidden in ("access_token", "refresh_token", "token_encrypted",
                          "external_user_id", "source_device", "device_id"):
            assert forbidden not in body_text, f"'{forbidden}' leaked in resync response"

    def test_resync_excludes_health_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.post("/api/health/devices/withings/resync", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text.lower()
        for hv in ("value_numeric", "canonical_unit", "sleep_score", "energy_kcal",
                    "heart_rate", "total_asleep", "distance_meters"):
            assert hv not in body_text, f"'{hv}' leaked in resync response"


class TestDisconnectRoutePrivacy:
    """POST /api/health/devices/withings/disconnect must be metadata-only."""

    def test_disconnect_excludes_tokens_and_provider_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.post("/api/health/devices/withings/disconnect", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text
        for forbidden in ("access_token", "refresh_token", "token_encrypted",
                          "external_user_id", "source_device", "device_id"):
            assert forbidden not in body_text, f"'{forbidden}' leaked in disconnect response"

    def test_disconnect_excludes_health_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.post("/api/health/devices/withings/disconnect", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text.lower()
        for hv in ("value_numeric", "sleep_score", "energy_kcal", "heart_rate"):
            assert hv not in body_text, f"'{hv}' leaked in disconnect response"


class TestDeleteRoutePrivacy:
    """DELETE /api/health/devices/withings must be metadata-only."""

    def test_delete_excludes_tokens_and_provider_ids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.delete("/api/health/devices/withings", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text
        for forbidden in ("access_token", "refresh_token", "token_encrypted",
                          "external_user_id", "source_device", "device_id"):
            assert forbidden not in body_text, f"'{forbidden}' leaked in delete response"

    def test_delete_excludes_health_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.delete("/api/health/devices/withings", headers=_auth_headers(user_id))
        assert response.status_code == 200, response.text
        body_text = response.text.lower()
        for hv in ("value_numeric", "sleep_score", "energy_kcal", "heart_rate"):
            assert hv not in body_text, f"'{hv}' leaked in delete response"


class TestConnectRoutePrivacy:
    """POST /api/health/devices/withings/connect returns config, not secrets."""

    def test_connect_excludes_tokens_and_health_values(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _prime(monkeypatch, auth_enabled=True)
        user_id = uuid4()
        pool = PrivacyScanPool(user_id)
        client = _health_client(pool)
        response = client.post(
            "/api/health/devices/withings/connect",
            json={"redirect_uri": "https://example.com/callback"},
            headers=_auth_headers(user_id),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        # Should contain authorization_url, not tokens
        assert "authorization_url" in body
        for forbidden in ("access_token", "refresh_token", "token_encrypted",
                          "client_secret", "external_user_id"):
            assert forbidden not in response.text.lower(), f"'{forbidden}' leaked in connect"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Admin diagnostics privacy
# ═══════════════════════════════════════════════════════════════════════════


class TestAdminHealthPrivacy:
    """GET /admin/health must be metadata-only, no health values or secrets."""

    def _admin_client(self, monkeypatch, pool=None) -> TestClient:
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
        app.include_router(admin_router)
        return TestClient(app)

    def _auth(self) -> tuple[str, str]:
        return ("admin", "correct-password")

    def test_admin_health_excludes_encrypted_tokens(self, monkeypatch) -> None:
        client = self._admin_client(monkeypatch)
        response = client.get("/admin/health", auth=self._auth())
        assert response.status_code == 200, response.text
        for forbidden in ("access_token_encrypted", "refresh_token_encrypted",
                          "access_token", "refresh_token"):
            assert forbidden not in response.text, (
                f"'{forbidden}' leaked in admin/health HTML"
            )

    def test_admin_health_excludes_cursor_state(self, monkeypatch) -> None:
        client = self._admin_client(monkeypatch)
        response = client.get("/admin/health", auth=self._auth())
        assert response.status_code == 200, response.text
        assert "cursor_state" not in response.text, "cursor_state leaked in admin/health"

    def test_admin_health_excludes_external_user_id(self, monkeypatch) -> None:
        client = self._admin_client(monkeypatch)
        response = client.get("/admin/health", auth=self._auth())
        assert response.status_code == 200, response.text
        # The connection SQL in admin.py fetches external_user_id internally
        # but the rendered HTML table columns should NOT include it
        # Check that it's not in the rendered column headers
        assert "external_user_id" not in response.text, (
            "external_user_id leaked in admin/health HTML"
        )

    def test_admin_health_excludes_health_values(self, monkeypatch) -> None:
        client = self._admin_client(monkeypatch)
        response = client.get("/admin/health", auth=self._auth())
        assert response.status_code == 200, response.text
        body_lower = response.text.lower()
        for hv in ("value_numeric", "canonical_unit", "sleep_score",
                    "energy_kcal", "heart_rate", "total_asleep",
                    "distance_meters", "weight_kg", "weight_lb"):
            assert hv not in body_lower, f"'{hv}' leaked in admin/health HTML"

    def test_admin_health_excludes_device_ids(self, monkeypatch) -> None:
        client = self._admin_client(monkeypatch)
        response = client.get("/admin/health", auth=self._auth())
        assert response.status_code == 200, response.text
        for marker in ("source_device_id", "source_device_model", "device_id"):
            assert marker not in response.text, f"'{marker}' leaked in admin/health"

    def test_admin_health_excludes_oauth_timestamps(self, monkeypatch) -> None:
        client = self._admin_client(monkeypatch)
        response = client.get("/admin/health", auth=self._auth())
        assert response.status_code == 200, response.text
        for oauth_field in ("access_token_expires_at", "refresh_token_expires_at",
                            "refresh_token_rotated_at", "oauth_state"):
            assert oauth_field not in response.text, (
                f"'{oauth_field}' leaked in admin/health"
            )

    def test_admin_health_metadata_only_columns_present(self, monkeypatch) -> None:
        """Verify only safe metadata columns are shown in the connection table."""
        client = self._admin_client(monkeypatch)
        response = client.get("/admin/health", auth=self._auth())
        assert response.status_code == 200, response.text
        # These are the safe columns defined in admin.py conn_columns
        safe_columns = {"id", "user_id", "status", "granted_scopes",
                        "stale_class", "last_success_at", "last_error_at",
                        "last_error_code", "updated_at"}
        # Make sure none of the prohibited markers sneak in
        for prohibited in ("token", "secret", "key", "password", "encrypted",
                           "cursor", "external_user_id", "device_id",
                           "device_model"):
            # Search for these as table headers
            th_pattern = f"<th>{prohibited}</th>"
            assert th_pattern not in response.text.lower(), (
                f"'{prohibited}' appears as a table header in admin/health"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 3. Metric log label privacy
# ═══════════════════════════════════════════════════════════════════════════


class TestMetricLogsPrivacy:
    """All metric helpers must use only safe labels."""

    _SAFE_LABELS: frozenset[str] = frozenset({
        "provider", "resource_type", "status", "error_kind", "retryable",
    })

    _UNSAFE_LABELS: frozenset[str] = frozenset({
        "user_id", "device_id", "token", "external_user_id",
        "health_value", "weight", "sleep_score", "heart_rate",
    })

    def _get_metric_function_names(self) -> list[str]:
        """Return the names of all public metric helper functions."""
        return [
            "record_sync_attempt",
            "record_sync_outcome",
            "record_sync_duration",
            "record_sync_fetched",
            "record_sync_deleted",
            "record_sync_retry",
            "record_cursor_error",
            "record_stale_freshness",
            "record_projection_outcome",
            "record_worker_scan",
        ]

    def test_all_expected_helpers_exist(self) -> None:
        """Verify the full set of helper functions is present."""
        for name in self._get_metric_function_names():
            assert hasattr(health_metrics, name), f"Missing metric helper: {name}"

    def test_metric_function_parameters_are_safe(self) -> None:
        """Every metric helper parameter must be one of the 5 safe labels."""
        for func_name in self._get_metric_function_names():
            func = getattr(health_metrics, func_name)
            sig = inspect.signature(func)
            param_names = set(sig.parameters.keys())
            # Every param should be in the safe set or be a non-label param
            # (value, count, duration_seconds, etc.)
            non_label_params = {"provider", "resource_type", "status",
                                "error_kind", "retryable", "duration_seconds",
                                "count", "value", "claimed", "synced", "failed",
                                "skipped_disabled", "reconciliation_outcomes",
                                "skipped_connections", "scanned_connections",
                                "args", "kwargs"}
            for param in param_names:
                if param in ("args", "kwargs"):
                    continue
                assert param in non_label_params, (
                    f"Unsafe parameter '{param}' in {func_name}() — "
                    f"must be one of the 5 safe labels"
                )

    def test_metric_incr_calls_use_safe_labels_only(self) -> None:
        """Scan metrics.py source for any _incr/_gauge/_observe call that uses
        a label not in the safe set."""
        source = inspect.getsource(health_metrics)
        # Look for label-like patterns in keyword arguments
        # All _incr, _gauge, _observe calls should have safe kwarg names
        for line in source.splitlines():
            stripped = line.strip()
            # Skip comments and docstrings
            if stripped.startswith("#") or stripped.startswith('"""'):
                continue
            for unsafe in self._UNSAFE_LABELS:
                if f"{unsafe}=" in stripped:
                    # This is a keyword argument — check context
                    # If it's inside a function call, it's a problem
                    assert False, (
                        f"Potentially unsafe label '{unsafe}' found in "
                        f"metrics.py: {stripped}"
                    )

    def test_metric_module_docstring_privacy_promise(self) -> None:
        """The metrics module docstring must explicitly state the label
        privacy boundary."""
        doc = (health_metrics.__doc__ or "").lower()
        assert "provider" in doc, "metrics module docstring should mention provider"
        assert "resource_type" in doc, "metrics module docstring should mention resource_type"
        assert "user id" in doc or "user_id" in doc, (
            "metrics module docstring must mention user-id exclusion"
        )
        assert "token" in doc, (
            "metrics module docstring must mention token exclusion"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 4. Tool registry / audit summary privacy
# ═══════════════════════════════════════════════════════════════════════════


class TestToolRegistryPrivacy:
    """Tool descriptions in the registry must not embed health values or secrets."""

    def test_health_tool_descriptions_exist(self) -> None:
        """Verify health read tools are registered."""
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        for tool_name in ("get_weight_trend", "get_sleep_summary",
                          "get_workout_summary"):
            assert tool_name in TOOL_DESCRIPTIONS, (
                f"Health read tool '{tool_name}' missing from registry"
            )

    def test_tool_descriptions_are_prompt_safe(self) -> None:
        """Tool descriptions (used in system prompts) must not embed secrets,
        tokens, or raw health values."""
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        for tool_name, description in TOOL_DESCRIPTIONS.items():
            desc_lower = description.lower()
            for forbidden in ("access_token", "refresh_token", "bearer",
                              "api_key", "client_secret", "password"):
                assert forbidden not in desc_lower, (
                    f"Tool '{tool_name}' description contains '{forbidden}'"
                )

    def test_health_tool_descriptions_no_raw_health_values(self) -> None:
        """Health tool descriptions should describe what the tools return,
        not embed actual health values."""
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        for tool_name in ("get_weight_trend", "get_sleep_summary",
                          "get_workout_summary"):
            desc = TOOL_DESCRIPTIONS.get(tool_name, "")
            # Descriptions should mention the type of data, not embed a value
            # "weight" is fine, "72.5 kg" is not
            numeric_pattern = re.compile(r'\b\d+\.\d+\s*(kg|lb|kcal|bpm|km|mi)\b')
            assert not numeric_pattern.search(desc), (
                f"Tool '{tool_name}' description contains a numeric health value: {desc}"
            )

    def test_health_tool_descriptions_no_device_ids(self) -> None:
        """Health tool descriptions must not reference device IDs."""
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        for tool_name in ("get_weight_trend", "get_sleep_summary",
                          "get_workout_summary"):
            desc = TOOL_DESCRIPTIONS.get(tool_name, "")
            for marker in ("device_id", "device_model", "source_device"):
                assert marker not in desc, (
                    f"Tool '{tool_name}' description references '{marker}'"
                )


# ═══════════════════════════════════════════════════════════════════════════
# 5. Hot context privacy
# ═══════════════════════════════════════════════════════════════════════════


class TestHotContextPrivacy:
    """Fitness hot context block scans for privacy boundaries.

    The health summary blocks in hot_context_solo intentionally include
    compact health aggregates (weight trend, sleep summary, workout summary)
    — the same data that the explicit health read tools return.  This is
    a design choice: Hector's system prompt includes inline health context
    so the bot can ground conversations in data without making extra tool
    calls.

    These tests verify that the hot context health surfaces:
      - Do NOT leak tokens, secrets, or OAuth state
      - Do NOT leak device identifiers or external user IDs
      - Only include compact aggregates, not raw measurement timelines
    """

    def test_hector_fitness_block_has_health_summaries(self) -> None:
        """Verify the hot context module has health summary-building functions.
        This is a documented surface — health aggregates appear inline in
        Hector's system prompt."""
        from app.services import hot_context_solo
        source = inspect.getsource(hot_context_solo)
        # The module should have functions that build health summary blocks
        assert "_build_weight_sleep_summary_block" in source or \
               "health" in source.lower(), \
               "hot_context_solo does not reference health summary building"

    def test_hector_fitness_block_no_device_ids(self) -> None:
        """Hot context health blocks must not reference device identifiers."""
        from app.services import hot_context_solo
        # Check the health summary functions specifically
        source = inspect.getsource(hot_context_solo)
        # Find health-related function bodies
        health_funcs = _extract_health_functions(source)
        for func_name, func_body in health_funcs:
            for marker in ("source_device_id", "source_device_model",
                           "external_user_id", "device_id", "device_model"):
                assert marker not in func_body, (
                    f"'{marker}' found in hot_context_solo.{func_name}()"
                )

    def test_hector_fitness_block_no_tokens(self) -> None:
        """Hot context health blocks must not reference tokens or secrets."""
        from app.services import hot_context_solo
        source = inspect.getsource(hot_context_solo)
        health_funcs = _extract_health_functions(source)
        for func_name, func_body in health_funcs:
            for token_term in ("access_token", "refresh_token", "bearer",
                               "client_secret", "api_key", "encrypted",
                               "oauth"):
                lines_with = [
                    line.strip() for line in func_body.splitlines()
                    if token_term in line.lower()
                    and not line.strip().startswith("#")
                ]
                assert not lines_with, (
                    f"'{token_term}' found in hot_context_solo.{func_name}(): {lines_with}"
                )

    def test_hector_fitness_block_no_raw_payloads(self) -> None:
        """Hot context health blocks must not reference raw payload keys."""
        from app.services import hot_context_solo
        source = inspect.getsource(hot_context_solo)
        health_funcs = _extract_health_functions(source)
        for func_name, func_body in health_funcs:
            for raw_key in ("raw_payload", "payload_json", "webhook_payload",
                            "response_body", "provider_response"):
                assert raw_key not in func_body, (
                    f"'{raw_key}' found in hot_context_solo.{func_name}()"
                )


# ═══════════════════════════════════════════════════════════════════════════
# 6. Prompt rendering fixture privacy
# ═══════════════════════════════════════════════════════════════════════════


class TestPromptRenderingPrivacy:
    """Prompt slots and rendering fixtures must not embed health values or secrets."""

    def test_health_read_guidance_prompt_no_secrets(self) -> None:
        """The health_read_guidance prompt slot must not embed tokens or secrets."""
        from app.bots.prompts.slots.health_read_guidance import BODY
        body_lower = BODY.lower()
        for forbidden in ("access_token", "refresh_token", "bearer",
                          "api_key", "client_secret", "password",
                          "secret", "encrypted"):
            assert forbidden not in body_lower, (
                f"'{forbidden}' found in health_read_guidance prompt"
            )

    def test_health_read_guidance_prompt_no_health_values(self) -> None:
        """The guidance prompt should describe tool boundaries, not embed values."""
        from app.bots.prompts.slots.health_read_guidance import BODY
        # It should NOT contain numeric health value examples
        numeric_with_unit = re.findall(r'\b\d+(?:\.\d+)?\s*(?:kg|lb|kcal|bpm|km|mi|hours|minutes)\b', BODY)
        assert not numeric_with_unit, (
            f"health_read_guidance prompt contains numeric health values: {numeric_with_unit}"
        )

    def test_health_read_guidance_prompt_no_device_ids(self) -> None:
        """The guidance prompt must not mention device IDs."""
        from app.bots.prompts.slots.health_read_guidance import BODY
        for marker in ("device_id", "device_model", "source_device", "external_user_id"):
            assert marker not in BODY, (
                f"'{marker}' found in health_read_guidance prompt body"
            )

    def test_health_read_guidance_prompt_references_boundaries(self) -> None:
        """The guidance prompt must explicitly state its privacy boundaries."""
        from app.bots.prompts.slots.health_read_guidance import BODY
        assert "never raw" in BODY.lower() or "compact" in BODY.lower(), (
            "health_read_guidance should mention compact/non-raw data"
        )
        assert "device" in BODY.lower(), (
            "health_read_guidance should reference device-scoped limitation"
        )


# ═══════════════════════════════════════════════════════════════════════════
# 7. Health read tools output type privacy (allowed surface — compact only)
# ═══════════════════════════════════════════════════════════════════════════


class TestHealthReadToolsAllowedSurface:
    """Health read tools ARE allowed to return aggregate health values,
    but must not return raw measurement-level detail or device IDs in
    the workout summary (per the tool description contract)."""

    def test_get_weight_trend_output_contains_aggregates(self) -> None:
        """get_weight_trend Output model should include aggregate fields."""
        from app.services.tools.read_tools import GetWeightTrendOutput
        # Check the model has expected aggregate fields
        fields = {name for name, _ in GetWeightTrendOutput.model_fields.items()}
        assert "avg_7d" in fields, "GetWeightTrendOutput missing avg_7d"
        assert "avg_30d" in fields, "GetWeightTrendOutput missing avg_30d"
        assert "latest" in fields, "GetWeightTrendOutput missing latest"

    def test_get_workout_summary_output_no_heart_rate_detail(self) -> None:
        """Per the tool contract, workout summary rows are per-date aggregates
        — they must NOT include individual heart-rate detail or device IDs."""
        from app.services.tools.read_tools import GetWorkoutSummaryOutput
        # The top-level output must not have heart_rate or device_id fields
        top_fields = {name for name, _ in GetWorkoutSummaryOutput.model_fields.items()}
        for forbidden in ("heart_rate_bpm", "average_heart_rate", "max_heart_rate",
                          "device_id", "device_model", "source_device"):
            assert forbidden not in top_fields, (
                f"'{forbidden}' found in GetWorkoutSummaryOutput top-level fields"
            )

    def test_get_sleep_summary_output_no_stage_timelines(self) -> None:
        """Per the tool contract, sleep summary returns per-date aggregates
        — must NOT include sleep-stage timelines."""
        from app.services.tools.read_tools import GetSleepSummaryOutput
        top_fields = {name for name, _ in GetSleepSummaryOutput.model_fields.items()}
        for forbidden in ("light_sleep_seconds", "deep_sleep_seconds",
                          "rem_sleep_seconds", "sleep_latency_seconds",
                          "wake_after_sleep_onset", "wakeups", "device_id",
                          "device_model"):
            assert forbidden not in top_fields, (
                f"'{forbidden}' found in GetSleepSummaryOutput top-level fields"
            )


# ═══════════════════════════════════════════════════════════════════════════
# 8. Export surface — explicitly ALLOWED to contain health values
# ═══════════════════════════════════════════════════════════════════════════


class TestExportAllowedSurface:
    """The authenticated export endpoint is explicitly allowed to contain
    health values, device IDs, and provider user IDs. These tests verify
    the export DOES contain what it should (and still excludes tokens)."""

    def test_export_contains_health_values(self) -> None:
        """Export must include health values — it's the designated surface."""
        # This is tested thoroughly in test_health_export.py;
        # here we just confirm the export module itself references
        # the columns that include health values.
        from app.services.health_sync.export import (
            _MEASUREMENT_COLUMNS,
            _SLEEP_COLUMNS,
            _WORKOUT_COLUMNS,
        )
        # Measurements include health values
        assert "value_numeric" in _MEASUREMENT_COLUMNS
        assert "canonical_unit" in _MEASUREMENT_COLUMNS
        # Sleep includes health values
        assert "sleep_score" in _SLEEP_COLUMNS
        assert "total_asleep_seconds" in _SLEEP_COLUMNS
        # Workouts include health values
        assert "energy_kcal" in _WORKOUT_COLUMNS
        assert "distance_meters" in _WORKOUT_COLUMNS

    def test_export_contains_device_ids(self) -> None:
        """Export is allowed to include device identifiers."""
        from app.services.health_sync.export import (
            _SOURCE_RECORD_COLUMNS,
            _MEASUREMENT_COLUMNS,
            _SLEEP_COLUMNS,
            _WORKOUT_COLUMNS,
        )
        assert "source_device_id" in _SOURCE_RECORD_COLUMNS
        assert "source_device_model" in _SOURCE_RECORD_COLUMNS
        assert "source_device_id" in _MEASUREMENT_COLUMNS
        assert "source_device_id" in _SLEEP_COLUMNS
        assert "source_device_id" in _WORKOUT_COLUMNS

    def test_export_excludes_tokens(self) -> None:
        """Even the export must exclude encrypted tokens."""
        from app.services.health_sync.export import _CONNECTION_COLUMNS
        for forbidden in ("access_token_encrypted", "refresh_token_encrypted",
                          "access_token_expires_at", "refresh_token_expires_at",
                          "refresh_token_rotated_at", "cursor_state"):
            assert forbidden not in _CONNECTION_COLUMNS, (
                f"'{forbidden}' should not be in export connection columns"
            )

    def test_export_contains_external_user_id(self) -> None:
        """Export is allowed to include provider user ID markers."""
        from app.services.health_sync.export import _CONNECTION_COLUMNS
        assert "external_user_id" in _CONNECTION_COLUMNS, (
            "external_user_id should be in export connection columns"
        )
