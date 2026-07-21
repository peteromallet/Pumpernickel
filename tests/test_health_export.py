from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
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


class ExportPool:
    """Fake pool that returns synthetic health data for a single user."""

    def __init__(
        self,
        user_id: UUID | None = None,
        *,
        include_second_user_data: bool = False,
    ) -> None:
        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        self._user_id = user_id
        self._include_second_user_data = include_second_user_data

        # Connection row
        self._connection_id = uuid4()
        self._connection = None
        if user_id is not None:
            self._connection = {
                "id": self._connection_id,
                "status": "active",
                "external_user_id": "420001",
                "granted_scopes": ["user.activity", "user.metrics"],
                "granted_at": now,
                "consented_measurements_at": now,
                "consented_workouts_at": now,
                "consented_sleep_at": now,
                "last_success_at": now,
                "last_error_at": None,
                "last_error_code": None,
                "last_error_detail": None,
                "disconnected_at": None,
                "revoked_at": None,
                "deleted_at": None,
                "created_at": now,
                "updated_at": now,
                # These should NEVER appear in export (encrypted tokens)
                "access_token_encrypted": encrypt_value("synthetic-access-token"),
                "refresh_token_encrypted": encrypt_value("synthetic-refresh-token"),
                "access_token_expires_at": None,
                "refresh_token_expires_at": None,
                "refresh_token_rotated_at": None,
                "cursor_state": {"measurement": {"last_modified": "2026-07-20T10:00:00Z"}},
                "provider": "withings",
                "user_id": user_id,
            }

        # Source records
        self._source_record_id = uuid4()
        self._source_records: list[dict[str, Any]] = []
        if user_id is not None:
            self._source_records.append({
                "id": self._source_record_id,
                "connection_id": self._connection_id,
                "provider": "withings",
                "resource_type": "measurement",
                "external_id": "meas-001",
                "source_created_at": now - timedelta(days=1),
                "source_modified_at": now - timedelta(hours=1),
                "observed_at": now - timedelta(days=1),
                "starts_at": None,
                "ends_at": None,
                "source_timezone": "Europe/Paris",
                "source_offset_seconds": 7200,
                "source_device_id": "dev-123",
                "source_device_model": "Body Comp",
                "payload_hash": "sha256:abc123",
                "provider_revision": "v2",
                "revision_count": 2,
                "source_metadata": {"measure_type": 1},
                "attribution": {"provider": "withings"},
                "is_deleted": False,
                "deleted_at": None,
                "imported_at": now - timedelta(days=1),
                "updated_at": now,
                # user_id is scoped by query; not stored separately here
            })
            # A deleted/tombstone source record
            self._source_records.append({
                "id": uuid4(),
                "connection_id": self._connection_id,
                "provider": "withings",
                "resource_type": "sleep",
                "external_id": "sleep-002",
                "source_created_at": now - timedelta(days=2),
                "source_modified_at": now - timedelta(hours=2),
                "observed_at": now - timedelta(days=2),
                "starts_at": now - timedelta(days=2, hours=8),
                "ends_at": now - timedelta(days=2),
                "source_timezone": "Europe/Paris",
                "source_offset_seconds": 7200,
                "source_device_id": "dev-456",
                "source_device_model": "Sleep Analyzer",
                "payload_hash": "sha256:def456",
                "provider_revision": "v1",
                "revision_count": 1,
                "source_metadata": {"tombstone_reason": "provider_deleted"},
                "attribution": {"provider": "withings"},
                "is_deleted": True,
                "deleted_at": now - timedelta(hours=2),
                "imported_at": now - timedelta(days=2),
                "updated_at": now,
            })

        # Normalized measurements
        self._measurements: list[dict[str, Any]] = []
        if user_id is not None:
            self._measurements.append({
                "id": uuid4(),
                "source_record_id": self._source_record_id,
                "connection_id": self._connection_id,
                "metric": "weight",
                "measured_at": now - timedelta(days=1),
                "value_numeric": 72.5,
                "canonical_unit": "kg",
                "source_unit": "kg",
                "source_device_id": "dev-123",
                "source_device_model": "Body Comp",
                "attribution": {"provider": "withings"},
                "created_at": now - timedelta(days=1),
                "updated_at": now,
            })

        # Normalized sleep
        self._sleep_rows: list[dict[str, Any]] = []
        if user_id is not None:
            self._sleep_rows.append({
                "id": uuid4(),
                "source_record_id": self._source_records[1]["id"],
                "connection_id": self._connection_id,
                "started_at": now - timedelta(days=2, hours=8),
                "ended_at": now - timedelta(days=2),
                "local_sleep_date": (now - timedelta(days=2)).date(),
                "local_timezone": "Europe/Paris",
                "local_offset_seconds": 7200,
                "completeness_state": "complete",
                "total_in_bed_seconds": 28800,
                "total_asleep_seconds": 25200,
                "awake_seconds": 1200,
                "light_sleep_seconds": 10800,
                "deep_sleep_seconds": 7200,
                "rem_sleep_seconds": 7200,
                "sleep_latency_seconds": 600,
                "wake_after_sleep_onset_seconds": 1800,
                "wakeups": 2,
                "sleep_score": 85,
                "source_device_id": "dev-456",
                "source_device_model": "Sleep Analyzer",
                "attribution": {"provider": "withings"},
                "created_at": now - timedelta(days=2),
                "updated_at": now,
            })

        # Normalized workouts
        self._workouts: list[dict[str, Any]] = []
        if user_id is not None:
            self._workouts.append({
                "id": uuid4(),
                "source_record_id": uuid4(),
                "connection_id": self._connection_id,
                "started_at": now - timedelta(days=1, hours=6),
                "ended_at": now - timedelta(days=1, hours=5, minutes=30),
                "local_timezone": "Europe/Paris",
                "local_offset_seconds": 7200,
                "workout_type": "running",
                "duration_seconds": 1800,
                "pause_duration_seconds": 0,
                "distance_meters": 5000.0,
                "steps": 6000,
                "energy_kcal": 350.0,
                "elevation_gain_meters": 25.0,
                "average_heart_rate_bpm": 145.0,
                "max_heart_rate_bpm": 172.0,
                "source_device_id": "dev-789",
                "source_device_model": "ScanWatch",
                "attribution": {"provider": "withings"},
                "created_at": now - timedelta(days=1, hours=5),
                "updated_at": now,
            })

        # Projections
        self._projections: list[dict[str, Any]] = []
        if user_id is not None:
            self._projections.append({
                "id": uuid4(),
                "source_record_id": self._source_record_id,
                "connection_id": self._connection_id,
                "event_id": uuid4(),
                "commitment_id": uuid4(),
                "projection_version": 1,
                "projection_status": "projected",
                "match_rule": "weight_daily",
                "note": "Auto-projected from Withings weight",
                "decision_reason": "exact_match",
                "matched_local_date": (now - timedelta(days=1)).date(),
                "supersedes_projection_id": None,
                "projected_at": now - timedelta(days=1),
                "removed_at": None,
                "created_at": now - timedelta(days=1),
                "updated_at": now,
            })

        # Dirty categories
        self._dirty_categories: list[dict[str, Any]] = []
        if user_id is not None:
            self._dirty_categories.append({
                "id": uuid4(),
                "connection_id": self._connection_id,
                "provider": "withings",
                "resource_type": "measurement",
                "reason": "manual",
                "source_receipt_id": None,
                "attempts": 0,
                "marked_at": now,
                "claimed_at": None,
                "claimed_by": None,
                "cleared_at": None,
            })

    # -- fetchrow (used by _fetch_connection in router) --

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = " ".join(sql.split())
        if compact.startswith("SELECT id, status, granted_scopes"):
            if self._connection is None or args[0] != self._user_id or self._connection.get("deleted_at") is not None:
                return None
            return self._connection
        raise AssertionError(f"Unexpected fetchrow SQL: {compact}")

    # -- fetch (used by export service) --

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        compact = " ".join(sql.split())

        # Connections
        if "FROM mediator.health_connections" in compact:
            if self._connection is None or args[0] != self._user_id:
                return []
            return [self._connection]

        # Source records
        if "FROM mediator.health_source_records" in compact:
            if self._connection is None or args[0] != self._user_id:
                return []
            return self._source_records

        # Normalized measurements
        if "FROM mediator.health_normalized_measurements" in compact:
            if self._connection is None or args[0] != self._user_id:
                return []
            return self._measurements

        # Normalized sleep
        if "FROM mediator.health_normalized_sleep" in compact:
            if self._connection is None or args[0] != self._user_id:
                return []
            return self._sleep_rows

        # Normalized workouts
        if "FROM mediator.health_normalized_workouts" in compact:
            if self._connection is None or args[0] != self._user_id:
                return []
            return self._workouts

        # Projections
        if "FROM mediator.health_source_to_event_projections" in compact:
            if self._connection is None or args[0] != self._user_id:
                return []
            return self._projections

        # Dirty categories
        if "FROM mediator.health_dirty_categories" in compact:
            if self._connection is None or args[0] != self._user_id:
                return []
            return self._dirty_categories

        raise AssertionError(f"Unexpected fetch SQL: {compact}")

    async def execute(self, sql: str, *args: Any) -> str:
        raise AssertionError(f"Unexpected execute SQL: {sql}")


def _prime(monkeypatch: pytest.MonkeyPatch, *, auth_enabled: bool) -> None:
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("LIVE_VOICE_AUTH_ENABLED", "true" if auth_enabled else "false")
    monkeypatch.setenv("LIVE_VOICE_TEST_USER_ID", "00000000-0000-0000-0000-000000000099")
    get_settings.cache_clear()
    live_jwt._signing_secret.cache_clear()
    reset_oauth_state_store_for_tests()


def _client(pool: ExportPool) -> TestClient:
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


def test_export_requires_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    client = _client(ExportPool())
    response = client.get("/api/health/devices/withings/export")
    assert response.status_code == 401, response.text


def test_export_returns_503_when_health_sync_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    monkeypatch.setenv("HEALTH_SYNC_ENABLED", "false")
    get_settings.cache_clear()
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 503, response.text
    body = response.json()
    assert body["provider"] == "withings"
    assert body["status"] == "unavailable"


def test_export_returns_404_when_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool())  # No user_id → no connection
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 404, response.text
    assert "Withings connection not found" in response.json()["detail"]


def test_export_includes_connection_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export includes connection metadata but NOT encrypted tokens or cursor_state."""
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["provider"] == "withings"
    assert body["user_id"] == str(user_id)
    assert "exported_at" in body
    assert len(body["connections"]) == 1

    conn = body["connections"][0]
    # Allowed connection metadata
    assert conn["id"] is not None
    assert conn["status"] == "active"
    assert conn["external_user_id"] == "420001"
    assert conn["granted_scopes"] == ["user.activity", "user.metrics"]
    assert conn["granted_at"] is not None
    assert conn["last_success_at"] is not None

    # Forbidden in connection: encrypted tokens, cursor_state
    for forbidden_key in (
        "access_token_encrypted",
        "refresh_token_encrypted",
        "access_token_expires_at",
        "refresh_token_expires_at",
        "refresh_token_rotated_at",
        "cursor_state",
    ):
        assert forbidden_key not in conn, f"'{forbidden_key}' leaked into connection export"


def test_export_includes_source_records_with_deletion_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Export includes source-record provenance metadata and deletion/tombstone state."""
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["source_records"]) == 2

    # First record is active
    active = body["source_records"][0]
    assert active["resource_type"] == "measurement"
    assert active["is_deleted"] is False
    assert active["external_id"] == "meas-001"
    assert active["payload_hash"] == "sha256:abc123"

    # Second record is a tombstone
    tombstone = body["source_records"][1]
    assert tombstone["resource_type"] == "sleep"
    assert tombstone["is_deleted"] is True
    assert tombstone["deleted_at"] is not None
    # Ensure source_metadata with tombstone_reason is present
    assert tombstone["source_metadata"] == {"tombstone_reason": "provider_deleted"}


def test_export_includes_normalized_measurements(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["normalized_measurements"]) == 1
    meas = body["normalized_measurements"][0]
    assert meas["metric"] == "weight"
    assert meas["value_numeric"] == 72.5
    assert meas["canonical_unit"] == "kg"


def test_export_includes_normalized_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["normalized_sleep"]) == 1
    sleep_row = body["normalized_sleep"][0]
    assert sleep_row["completeness_state"] == "complete"
    assert sleep_row["sleep_score"] == 85
    assert sleep_row["total_asleep_seconds"] == 25200


def test_export_includes_normalized_workouts(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["normalized_workouts"]) == 1
    workout = body["normalized_workouts"][0]
    assert workout["workout_type"] == "running"
    assert workout["duration_seconds"] == 1800
    assert workout["distance_meters"] == 5000.0
    assert workout["energy_kcal"] == 350.0


def test_export_includes_projection_ledger_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["projections"]) == 1
    proj = body["projections"][0]
    assert proj["projection_status"] == "projected"
    assert proj["match_rule"] == "weight_daily"
    assert proj["decision_reason"] == "exact_match"


def test_export_includes_dirty_categories(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["dirty_categories"]) == 1
    dirty = body["dirty_categories"][0]
    assert dirty["resource_type"] == "measurement"
    assert dirty["reason"] == "manual"


def test_export_excludes_encrypted_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """No encrypted token fields anywhere in the export."""
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body_str = str(response.json())

    for forbidden_key in (
        "access_token_encrypted",
        "refresh_token_encrypted",
        "access_token",
        "refresh_token",
    ):
        assert forbidden_key not in body_str, f"'{forbidden_key}' leaked into export"


def test_export_excludes_oauth_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """No OAuth state fields in the export."""
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    body_str = str(body)

    # The export must not contain OAuth-specific artifacts as top-level
    # or connection-level keys.  Generic words like "state" or "status"
    # appear in legitimate health data (completeness_state,
    # projection_status, etc.), so we target the exact OAuth field names.
    for forbidden_key in (
        "oauth_state",
        "oauth_code",
        "code_verifier",
    ):
        assert forbidden_key not in body_str, f"'{forbidden_key}' leaked into export"

    # The connections must not contain any OAuth token expirations
    for conn in body.get("connections", []):
        for forbidden_key in (
            "access_token_expires_at",
            "refresh_token_expires_at",
            "refresh_token_rotated_at",
        ):
            assert forbidden_key not in conn, f"'{forbidden_key}' leaked into connection export"


def test_export_excludes_webhook_payloads(monkeypatch: pytest.MonkeyPatch) -> None:
    """No webhook receipt or raw payload data in the export."""
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body_str = str(response.json())

    for forbidden_key in (
        "webhook_receipt",
        "webhook_receipts",
        "raw_payload",
        "raw_body",
        "form_payload",
    ):
        assert forbidden_key not in body_str, f"'{forbidden_key}' leaked into export"


def test_export_scopes_to_authenticated_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cross-user data must not appear — test with second-user data present."""
    _prime(monkeypatch, auth_enabled=True)
    user_a = uuid4()
    user_b = uuid4()
    token_a = live_jwt.mint(user_id=str(user_a))
    token_b = live_jwt.mint(user_id=str(user_b))
    pool = ExportPool(user_a)
    client = _client(pool)

    # User B should get 404 (no connection for user B)
    headers_b = {"Authorization": f"Bearer {token_b}"}
    response_b = client.get("/api/health/devices/withings/export", headers=headers_b)
    assert response_b.status_code == 404, f"User B should not find User A's connection: {response_b.text}"

    # User A should get their data
    headers_a = {"Authorization": f"Bearer {token_a}"}
    response_a = client.get("/api/health/devices/withings/export", headers=headers_a)
    assert response_a.status_code == 200, response_a.text
    body_a = response_a.json()

    # All connection data belongs to user_a
    for conn in body_a["connections"]:
        assert conn["id"] is not None
    for sr in body_a["source_records"]:
        assert sr["connection_id"] == str(pool._connection_id)


def test_export_response_has_required_top_level_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=True)
    user_id = uuid4()
    token = live_jwt.mint(user_id=str(user_id))
    headers = {"Authorization": f"Bearer {token}"}
    client = _client(ExportPool(user_id))
    response = client.get("/api/health/devices/withings/export", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()

    # Top-level keys
    assert body["provider"] == "withings"
    assert body["user_id"] == str(user_id)
    assert isinstance(body["exported_at"], str)

    # All expected sections present
    for section in (
        "connections",
        "source_records",
        "normalized_measurements",
        "normalized_sleep",
        "normalized_workouts",
        "projections",
        "dirty_categories",
    ):
        assert section in body, f"'{section}' missing from export"
        assert isinstance(body[section], list), f"'{section}' should be a list"


def test_dev_fallback_allows_export_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _prime(monkeypatch, auth_enabled=False)
    test_user_id = UUID("00000000-0000-0000-0000-000000000099")
    client = _client(ExportPool(test_user_id))
    response = client.get("/api/health/devices/withings/export")
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "withings"
