"""Public Withings OAuth callback and notification routes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app.config import Settings, get_settings
from app.db import get_pool
from app.services.health_sync import (
    OAuthStateError,
    OAuthStateStore,
    WithingsNotificationError,
    WithingsProvider,
    get_oauth_state_store,
    ingest_withings_notification,
    store_connection_tokens,
)

router = APIRouter()

_UNAVAILABLE_BODY = {
    "status": "unavailable",
    "detail": "Withings integration is unavailable.",
}
_INVALID_CALLBACK_BODY = {
    "status": "invalid_request",
    "detail": "Invalid Withings callback.",
}
_INVALID_NOTIFICATION_BODY = {
    "status": "invalid_request",
    "detail": "Invalid Withings notification.",
}
_UNSUPPORTED_NOTIFICATION_BODY = {
    "status": "unsupported_media_type",
    "detail": "Unsupported notification content type.",
}


def _oauth_state_store(request: Request) -> OAuthStateStore:
    store = getattr(request.app.state, "health_oauth_state_store", None)
    if isinstance(store, OAuthStateStore):
        return store
    return get_oauth_state_store()


def _withings_provider(request: Request, settings: Settings) -> Any:
    provider = getattr(request.app.state, "health_withings_provider", None)
    if provider is not None:
        return provider
    return WithingsProvider(
        client_id=settings.withings_client_id.get_secret_value(),
        client_secret=settings.withings_client_secret.get_secret_value(),
        api_base_url=settings.withings_api_endpoint,
    )


def _allowed_redirect_uri(redirect_uri: str) -> str | None:
    candidate = redirect_uri.strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if parsed.fragment or not parsed.netloc:
        return None
    if parsed.scheme == "https":
        return candidate
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}:
        return candidate
    return None


def _static_invalid_callback(status_code: int = 400) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=_INVALID_CALLBACK_BODY)


def _notification_error_response(exc: WithingsNotificationError) -> JSONResponse:
    if exc.status_code == 415:
        return JSONResponse(status_code=415, content=_UNSUPPORTED_NOTIFICATION_BODY)
    return JSONResponse(status_code=exc.status_code, content=_INVALID_NOTIFICATION_BODY)


@router.head("/api/health/devices/withings/oauth/callback")
async def oauth_callback_head() -> Response:
    return Response(status_code=200)


@router.get("/api/health/devices/withings/oauth/callback")
async def oauth_callback_get(
    request: Request,
    pool: Any = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not settings.health_sync_enabled:
        return JSONResponse(status_code=503, content=_UNAVAILABLE_BODY)

    code = str(request.query_params.get("code", "")).strip()
    state = str(request.query_params.get("state", "")).strip()
    if not code or not state:
        return _static_invalid_callback()

    try:
        consumed = _oauth_state_store(request).consume_callback(state=state)
    except OAuthStateError:
        return _static_invalid_callback()

    redirect_uri = _allowed_redirect_uri(consumed.redirect_uri)
    if redirect_uri is None:
        return _static_invalid_callback()

    try:
        tokens = await _withings_provider(request, settings).exchange_code(
            code=code,
            redirect_uri=settings.withings_callback_url,
        )
        await store_connection_tokens(
            pool,
            user_id=consumed.user_id,
            provider="withings",
            tokens=tokens,
        )
    except Exception:
        return JSONResponse(status_code=503, content=_UNAVAILABLE_BODY)

    return RedirectResponse(url=redirect_uri, status_code=303)


@router.head("/api/health/devices/withings/notifications")
async def notifications_head() -> Response:
    return Response(status_code=200)


@router.post("/api/health/devices/withings/notifications")
async def notifications_post(
    request: Request,
    pool: Any = Depends(get_pool),
    settings: Settings = Depends(get_settings),
) -> Response:
    if not settings.health_sync_enabled:
        return JSONResponse(status_code=503, content=_UNAVAILABLE_BODY)

    content_type = request.headers.get("content-type")
    if not content_type or not content_type.casefold().startswith(
        "application/x-www-form-urlencoded"
    ):
        return JSONResponse(status_code=415, content=_UNSUPPORTED_NOTIFICATION_BODY)

    form_data = await request.form()
    form = {
        str(key): _first_form_value(value)
        for key, value in form_data.multi_items()
    }
    try:
        await ingest_withings_notification(
            pool,
            content_type=content_type,
            form=form,
        )
    except WithingsNotificationError as exc:
        return _notification_error_response(exc)
    return Response(status_code=200)


def _first_form_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    if isinstance(value, Mapping):
        return str(dict(value))
    return str(value)
