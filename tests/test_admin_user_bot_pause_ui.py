"""Tests for per-(user,bot) pause operator UI."""

from __future__ import annotations

import base64

import pytest
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


def _auth_headers() -> dict:
    raw = base64.b64encode(b"admin:correct-password").decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.mark.asyncio
async def test_get_user_bot_pauses_renders(monkeypatch, fake_pool):
    """GET /admin/user-bot-pauses renders the table."""
    client = _client(monkeypatch, fake_pool)
    resp = client.get("/admin/user-bot-pauses", headers=_auth_headers())
    assert resp.status_code == 200
    html = resp.text
    assert "User-Bot Pauses" in html


@pytest.mark.asyncio
async def test_post_round_trips_paused_state(monkeypatch, fake_pool):
    """POST /admin/user-bot-pause round-trips and paused state reflected on subsequent GET."""
    from uuid import uuid4

    user_id = uuid4()
    bot_id = "test-bot"

    client = _client(monkeypatch, fake_pool)

    # Pause the bot for this user
    resp = client.post(
        "/admin/user-bot-pause",
        data={"user_id": str(user_id), "bot_id": bot_id, "paused": "true"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["user_id"] == str(user_id)
    assert data["bot_id"] == bot_id
    assert data["paused"] is True

    # Verify paused state reflected on GET
    resp = client.get("/admin/user-bot-pauses", headers=_auth_headers())
    assert resp.status_code == 200
    html = resp.text
    assert str(user_id) in html
    assert bot_id in html

    # Resume the bot for this user
    resp = client.post(
        "/admin/user-bot-pause",
        data={"user_id": str(user_id), "bot_id": bot_id, "paused": "false"},
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["paused"] is False


@pytest.mark.asyncio
async def test_nav_link_wired(monkeypatch, fake_pool):
    """The new nav link appears in the admin nav."""
    client = _client(monkeypatch, fake_pool)
    resp = client.get("/admin/turns", headers=_auth_headers())
    assert resp.status_code == 200
    html = resp.text
    assert 'href="/admin/user-bot-pauses"' in html
    assert "Pauses" in html