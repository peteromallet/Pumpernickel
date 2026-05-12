"""S4 — per-(user, bot) pause WRITE path tests.

Covers the set_user_bot_paused helper (round-trip via in-memory mock pool)
and the POST /admin/user-bot-pause endpoint behind authenticate_admin.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import get_settings
from app.routers.admin import router
from app.services.system_state import set_user_bot_paused, user_bot_paused
from tests.conftest import FakePool


class _PauseMockPool:
    """Minimal pool that handles the INSERT...ON CONFLICT and the SELECT
    for set_user_bot_paused / user_bot_paused round-trip."""

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, str], bool] = {}

    async def execute(self, sql: str, *args: Any) -> None:
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO user_bot_state (user_id, bot_id, paused, updated_at)"):
            user_id, bot_id, paused = args[0], args[1], args[2]
            self.rows[(user_id, bot_id)] = bool(paused)
            return None
        raise AssertionError(f"unexpected execute SQL: {compact}")

    async def fetchval(self, sql: str, *args: Any) -> Any:
        compact = " ".join(sql.split())
        if compact.startswith("SELECT paused FROM user_bot_state WHERE user_id"):
            return self.rows.get((args[0], args[1]))
        raise AssertionError(f"unexpected fetchval SQL: {compact}")


@pytest.mark.asyncio
async def test_set_user_bot_paused_round_trip() -> None:
    pool = _PauseMockPool()
    user_id = uuid4()

    assert await user_bot_paused(pool, user_id, "mediator") is False
    await set_user_bot_paused(pool, user_id, "mediator", True)
    assert await user_bot_paused(pool, user_id, "mediator") is True
    await set_user_bot_paused(pool, user_id, "mediator", False)
    assert await user_bot_paused(pool, user_id, "mediator") is False


def _client(monkeypatch, pool: Any) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
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
    app.state.pool = pool
    app.include_router(router)
    return TestClient(app)


def test_user_bot_pause_endpoint_requires_auth(monkeypatch) -> None:
    pool = _PauseMockPool()
    client = _client(monkeypatch, pool)
    user_id = uuid4()
    resp = client.post(
        "/admin/user-bot-pause",
        data={"user_id": str(user_id), "bot_id": "mediator", "paused": "true"},
    )
    assert resp.status_code == 401


def test_user_bot_pause_endpoint_upserts(monkeypatch) -> None:
    pool = _PauseMockPool()
    client = _client(monkeypatch, pool)
    user_id = uuid4()
    resp = client.post(
        "/admin/user-bot-pause",
        data={"user_id": str(user_id), "bot_id": "mediator", "paused": "true"},
        auth=("admin", "correct-password"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {"user_id": str(user_id), "bot_id": "mediator", "paused": True}
    assert pool.rows[(user_id, "mediator")] is True

    # Toggle back to false
    resp = client.post(
        "/admin/user-bot-pause",
        data={"user_id": str(user_id), "bot_id": "mediator", "paused": "false"},
        auth=("admin", "correct-password"),
    )
    assert resp.status_code == 200
    assert pool.rows[(user_id, "mediator")] is False
