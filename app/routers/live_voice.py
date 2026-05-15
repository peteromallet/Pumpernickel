"""Live voice agent router (Sprint-0 stubs).

Endpoints under ``/api/live`` plus a stub WebSocket at ``/ws/live/{session_id}``
power the React UI in ``web/live-voice``.  This sprint wires the surface area
so the front-end can talk to a real backend; later sprints will swap the
WebSocket stub for actual realtime audio + Haiku turn handling.

TODOs intentionally left in place:
- Persona scoping (currently returns *all* registered bots; later: scope to
  the caller's ``mediator.bot_bindings`` rows).
- Auth (currently uses ``LIVE_VOICE_TEST_USER_ID`` from settings as a
  placeholder caller id).
- WebSocket handler (currently echo + a single phase message).
"""

from __future__ import annotations

import logging
import os
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from app.bots.registry import BOT_SPECS, _maybe_register_staging_bots
from app.config import get_settings
from app.db import get_pool

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Models ───────────────────────────────────────────────────────────────────


class CreateSessionRequest(BaseModel):
    bot_id: str = Field(..., description="Persona id, e.g. 'tante_rosi'")
    topic: str | None = Field(default=None, description="Optional topic label / slug")
    steering_text: str | None = Field(
        default=None,
        description="Optional steering text; presence flips mode to 'steered'",
    )


class CreateSessionResponse(BaseModel):
    session_id: UUID
    mode: str
    status: str


# ── Helpers ──────────────────────────────────────────────────────────────────


# Stable placeholder so dev runs without auth still produce a valid UUID
# in mediator.conversations.user_id (which is presumed UUID-typed).
_DEFAULT_TEST_USER_ID = UUID("00000000-0000-0000-0000-000000000001")


def _resolve_test_user_id() -> UUID:
    """Read LIVE_VOICE_TEST_USER_ID from env (Settings doesn't carry it yet).

    Kept as a module-level helper so tests can monkeypatch the env var.
    """
    raw = os.environ.get("LIVE_VOICE_TEST_USER_ID", "").strip()
    if not raw:
        return _DEFAULT_TEST_USER_ID
    try:
        return UUID(raw)
    except ValueError:
        logger.warning(
            "live_voice: LIVE_VOICE_TEST_USER_ID=%r is not a valid UUID; "
            "falling back to placeholder",
            raw,
        )
        return _DEFAULT_TEST_USER_ID


async def _conversations_table_exists(pool: Any) -> bool:
    """Check whether ``mediator.conversations`` is present.

    Used by /healthz and /sessions to give a clean error before the migration
    lands.
    """
    try:
        present = await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'mediator'
                  AND table_name = 'conversations'
            )
            """,
        )
    except Exception:
        logger.warning("live_voice: failed to probe mediator.conversations", exc_info=True)
        return False
    return bool(present)


# ── REST endpoints ───────────────────────────────────────────────────────────


@router.get("/api/live/healthz")
async def healthz(pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Liveness + dependency snapshot for the live-voice surface."""
    checks: dict[str, Any] = {}

    # DB reachable?
    try:
        await pool.fetchval("SELECT 1")
        checks["db"] = {"ok": True}
    except Exception as exc:
        checks["db"] = {"ok": False, "error": str(exc)}

    # mediator.conversations present?
    has_conversations = await _conversations_table_exists(pool)
    checks["conversations_table"] = {
        "ok": has_conversations,
        "detail": (
            "mediator.conversations present"
            if has_conversations
            else "mediator.conversations missing — run live-voice migration"
        ),
    }

    # OPENAI_API_KEY available?
    settings = get_settings()
    openai_key_present = bool(
        settings.openai_api_key and settings.openai_api_key.get_secret_value()
    )
    checks["openai_api_key"] = {"ok": openai_key_present}

    overall_ok = checks["db"]["ok"] and openai_key_present
    # NB: missing conversations table is *not* a hard fail per spec.
    return {"ok": overall_ok, "checks": checks}


@router.get("/api/live/personas")
async def list_personas() -> list[dict[str, str]]:
    """Return personas the caller may steer.

    Sprint-0: returns *all* registered bots.
    TODO: scope to the caller's rows in ``mediator.bot_bindings`` once auth
    is wired (then this becomes per-user).
    """
    _maybe_register_staging_bots()
    return [
        {
            "id": spec.bot_id,
            "display_name": spec.display_name,
            "topic": spec.primary_topic_slug,
        }
        for spec in sorted(BOT_SPECS.values(), key=lambda s: s.display_name.lower())
    ]


@router.get("/api/live/config")
async def public_config() -> dict[str, Any]:
    """Public client config — used by the React app to render conditional UI."""
    settings = get_settings()
    openai_key_present = bool(
        settings.openai_api_key and settings.openai_api_key.get_secret_value()
    )
    return {
        "discord_oauth_enabled": False,  # TODO: flip when OAuth lands.
        "openai_voice_enabled": openai_key_present,
        "env_name": settings.env_name,
    }


@router.post("/api/live/sessions", response_model=CreateSessionResponse)
async def create_session(
    body: CreateSessionRequest,
    pool: Any = Depends(get_pool),
) -> CreateSessionResponse:
    """Create a new live-voice conversation row in ``mediator.conversations``."""
    if not await _conversations_table_exists(pool):
        raise HTTPException(
            status_code=503,
            detail="live conversations not yet migrated",
        )

    # Validate bot_id against the registry (fail fast with 400 instead of FK
    # violation surfacing as a 500).
    _maybe_register_staging_bots()
    if body.bot_id not in BOT_SPECS:
        known = ", ".join(sorted(BOT_SPECS))
        raise HTTPException(
            status_code=400,
            detail=f"unknown bot_id={body.bot_id!r}; known: {known}",
        )

    mode = "steered" if (body.steering_text or "").strip() else "open"
    status_value = "prepping"
    user_id = _resolve_test_user_id()  # TODO: replace with auth user id.
    session_id = uuid4()

    try:
        await pool.execute(
            """
            INSERT INTO mediator.conversations
                (id, user_id, bot_id, topic, mode, status, steering_text)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            """,
            session_id,
            user_id,
            body.bot_id,
            body.topic,
            mode,
            status_value,
            body.steering_text,
        )
    except Exception as exc:
        logger.exception("live_voice: failed to insert conversation row")
        raise HTTPException(
            status_code=500,
            detail=f"failed to create live session: {exc}",
        ) from exc

    return CreateSessionResponse(session_id=session_id, mode=mode, status=status_value)


@router.get("/api/live/sessions/{session_id}")
async def get_session(session_id: UUID, pool: Any = Depends(get_pool)) -> dict[str, Any]:
    """Return a single conversation row (or 404)."""
    if not await _conversations_table_exists(pool):
        raise HTTPException(
            status_code=503,
            detail="live conversations not yet migrated",
        )
    try:
        row = await pool.fetchrow(
            "SELECT * FROM mediator.conversations WHERE id = $1",
            session_id,
        )
    except Exception as exc:
        logger.exception("live_voice: failed to fetch conversation row")
        raise HTTPException(
            status_code=500,
            detail=f"failed to fetch live session: {exc}",
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {key: value for key, value in dict(row).items()}


# ── WebSocket stub ───────────────────────────────────────────────────────────


@router.websocket("/ws/live/{session_id}")
async def live_socket(websocket: WebSocket, session_id: str) -> None:
    """Sprint-0 stub: accept, send a ready phase, then echo.

    Real audio framing + Haiku tool calls land in a later sprint.
    """
    await websocket.accept()
    try:
        await websocket.send_json(
            {
                "type": "phase",
                "label": "Live voice agent backend ready (sprint-0 stub)",
                "session_id": session_id,
            }
        )
        while True:
            try:
                message = await websocket.receive_text()
            except WebSocketDisconnect:
                return
            await websocket.send_json({"type": "echo", "payload": message})
    except WebSocketDisconnect:
        return
    except Exception:
        logger.exception("live_voice: websocket handler crashed")
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
