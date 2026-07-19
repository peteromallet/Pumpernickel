"""Withings router tests — fail-closed stubs."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers.withings import router


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── OAuth callback HEAD ──────────────────────────────────────────────


def test_oauth_callback_head_returns_204() -> None:
    """Withings probes this URL before showing it to the user."""
    response = _client().head("/api/health/devices/withings/oauth/callback")
    assert response.status_code == 204


# ── OAuth callback GET (fail-closed, no secret reflection) ───────────


def test_oauth_callback_get_returns_503() -> None:
    """Token exchange is not implemented — must fail closed."""
    response = _client().get("/api/health/devices/withings/oauth/callback")
    assert response.status_code == 503


def test_oauth_callback_get_does_not_echo_code_or_state() -> None:
    """The 503 response must be static; never reflect auth params."""
    response = _client().get(
        "/api/health/devices/withings/oauth/callback",
        params={
            "code": "secret-auth-code-12345",
            "state": "csrf-state-token-67890",
        },
    )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    # Must not contain the code or state in any field
    body_str = str(body)
    assert "secret-auth-code-12345" not in body_str
    assert "csrf-state-token-67890" not in body_str


def test_oauth_callback_get_ignores_error_params() -> None:
    """Even with Withings error fields, the response is the static 503."""
    response = _client().get(
        "/api/health/devices/withings/oauth/callback",
        params={"error": "access_denied", "error_description": "User declined"},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    body_str = str(body)
    assert "access_denied" not in body_str
    assert "User declined" not in body_str


# ── Notifications HEAD ───────────────────────────────────────────────


def test_notifications_head_returns_204() -> None:
    """Withings validates URL reachability."""
    response = _client().head("/api/health/devices/withings/notifications")
    assert response.status_code == 204


# ── Notifications POST (fail-closed, no body reflection) ─────────────


def test_notifications_post_returns_503() -> None:
    """Persistent ingestion is not implemented — must fail closed."""
    response = _client().post("/api/health/devices/withings/notifications")
    assert response.status_code == 503


def test_notifications_post_does_not_echo_body() -> None:
    """The 503 response must be static; never reflect form data."""
    response = _client().post(
        "/api/health/devices/withings/notifications",
        data={"userid": "12345", "startdate": "1234567890", "enddate": "1234567900"},
    )
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unavailable"
    body_str = str(body)
    assert "12345" not in body_str
    assert "1234567890" not in body_str
    assert "1234567900" not in body_str


def test_notifications_post_with_content_type_header() -> None:
    """Withings sends application/x-www-form-urlencoded."""
    response = _client().post(
        "/api/health/devices/withings/notifications",
        data={"userid": "42"},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 503
    body = response.json()
    assert "42" not in str(body)
