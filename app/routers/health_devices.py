"""Authenticated health-device routes with metadata-only responses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.db import get_pool
from app.routers.live_voice import get_current_user as get_live_voice_current_user
from app.services.health_sync import (
    HealthResourceType,
    WithingsProvider,
    export_withings_data,
    load_connection_tokens,
    WITHINGS_PROVIDER_CAPABILITIES,
)
from app.services.health_sync.oauth_state import get_oauth_state_store
from app.services.health_sync.repository import repository_for

router = APIRouter()

_WITHINGS_AUTHORIZE_URL = "https://account.withings.com/oauth2_user/authorize2"


class ConnectBody(BaseModel):
    redirect_uri: str = Field(min_length=1)
    resource_types: list[HealthResourceType] | None = None


def get_health_current_user(request: Request) -> UUID:
    """Dedicated health auth dependency with the live JWT/dev fallback semantics."""
    return get_live_voice_current_user(request)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _selected_resource_types(
    requested: list[HealthResourceType] | None,
    settings: Settings,
) -> tuple[HealthResourceType, ...]:
    if requested:
        deduped = tuple(dict.fromkeys(requested))
        return deduped

    enabled: list[HealthResourceType] = []
    if settings.health_sync_measurements_enabled:
        enabled.append(HealthResourceType.MEASUREMENT)
    if settings.health_sync_workouts_enabled:
        enabled.append(HealthResourceType.WORKOUT)
    if settings.health_sync_sleep_enabled:
        enabled.append(HealthResourceType.SLEEP)
    return tuple(enabled)


def _required_scopes(resource_types: tuple[HealthResourceType, ...]) -> list[str]:
    scopes = {
        scope
        for resource_type in resource_types
        for scope in WITHINGS_PROVIDER_CAPABILITIES.category_for(resource_type).required_scopes
    }
    return sorted(scopes)


def _granted_resource_types(
    resource_types: tuple[HealthResourceType, ...],
    granted_scopes: list[str] | tuple[str, ...] | set[str] | frozenset[str],
) -> tuple[HealthResourceType, ...]:
    granted = frozenset(granted_scopes)
    if not granted:
        return resource_types
    return tuple(
        resource_type
        for resource_type in resource_types
        if WITHINGS_PROVIDER_CAPABILITIES.category_for(
            resource_type
        ).required_scopes.issubset(granted)
    )


def _metadata_only_connection(row: Any) -> dict[str, Any]:
    if row is None:
        return {"status": "not_connected"}
    return {
        "status": row["status"],
        "granted_scopes": sorted(row.get("granted_scopes") or []),
        "granted_at": _isoformat(row.get("granted_at")),
        "last_success_at": _isoformat(row.get("last_success_at")),
        "last_error_at": _isoformat(row.get("last_error_at")),
        "last_error_code": row.get("last_error_code"),
        "disconnected_at": _isoformat(row.get("disconnected_at")),
        "revoked_at": _isoformat(row.get("revoked_at")),
        "deleted_at": _isoformat(row.get("deleted_at")),
        "updated_at": _isoformat(row.get("updated_at")),
    }


def _feature_disabled_response() -> tuple[int, dict[str, Any]]:
    return (
        503,
        {
            "provider": "withings",
            "status": "unavailable",
            "detail": "Health sync is disabled.",
        },
    )


async def _fetch_connection(pool: Any, user_id: UUID) -> Any:
    return await pool.fetchrow(
        """
        SELECT id, status, granted_scopes, granted_at, last_success_at, last_error_at,
               last_error_code, disconnected_at, revoked_at, deleted_at, updated_at
        FROM mediator.health_connections
        WHERE user_id = $1 AND provider = 'withings' AND deleted_at IS NULL
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        user_id,
    )


def _withings_provider(request: Request, settings: Settings) -> Any:
    provider = getattr(request.app.state, "health_withings_provider", None)
    if provider is not None:
        return provider
    return WithingsProvider(
        client_id=settings.withings_client_id.get_secret_value(),
        client_secret=settings.withings_client_secret.get_secret_value(),
        api_base_url=settings.withings_api_endpoint,
    )


@router.post("/api/health/devices/withings/connect")
async def connect_withings(
    body: ConnectBody,
    user_id: UUID = Depends(get_health_current_user),
    settings: Settings = Depends(get_settings),
) -> tuple[dict[str, Any], int] | dict[str, Any]:
    if not settings.health_sync_enabled:
        status_code, payload = _feature_disabled_response()
        return JSONResponse(status_code=status_code, content=payload)
    resource_types = _selected_resource_types(body.resource_types, settings)
    scopes = _required_scopes(resource_types)
    issued = get_oauth_state_store().issue(user_id=user_id, redirect_uri=body.redirect_uri)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": settings.withings_client_id.get_secret_value(),
            "redirect_uri": settings.withings_callback_url,
            "scope": ",".join(scopes),
            "state": issued.state,
        }
    )
    return {
        "provider": "withings",
        "status": "ready_for_oauth",
        "resource_types": [resource_type.value for resource_type in resource_types],
        "required_scopes": scopes,
        "expires_at": _isoformat(issued.expires_at),
        "authorization_url": f"{_WITHINGS_AUTHORIZE_URL}?{query}",
    }


@router.get("/api/health/devices/withings/status")
async def withings_status(
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_health_current_user),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.health_sync_enabled:
        status_code, payload = _feature_disabled_response()
        return JSONResponse(status_code=status_code, content=payload)
    row = await _fetch_connection(pool, user_id)
    return {
        "provider": "withings",
        "feature_enabled": True,
        "connection": _metadata_only_connection(row),
    }


@router.post("/api/health/devices/withings/resync")
async def withings_resync(
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_health_current_user),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.health_sync_enabled:
        status_code, payload = _feature_disabled_response()
        return JSONResponse(status_code=status_code, content=payload)
    row = await _fetch_connection(pool, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Withings connection not found")
    resource_types = _granted_resource_types(
        _selected_resource_types(None, settings),
        row.get("granted_scopes") or [],
    )
    repo = repository_for(pool)
    for resource_type in resource_types:
        await repo.mark_dirty(
            connection_id=row["id"],
            user_id=user_id,
            provider="withings",
            resource_type=resource_type,
            reason="manual",
        )
    return {
        "provider": "withings",
        "status": "accepted",
        "detail": "Resync queued for enabled categories.",
        "resource_types": [resource_type.value for resource_type in resource_types],
        "connection": _metadata_only_connection(row),
    }


@router.post("/api/health/devices/withings/disconnect")
async def withings_disconnect(
    request: Request,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_health_current_user),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.health_sync_enabled:
        status_code, payload = _feature_disabled_response()
        return JSONResponse(status_code=status_code, content=payload)
    current = await _fetch_connection(pool, user_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Withings connection not found")
    try:
        tokens = await load_connection_tokens(pool, connection_id=current["id"])
        if tokens.access_token:
            await _withings_provider(request, settings).revoke(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
            )
    except Exception:
        pass
    row = await pool.fetchrow(
        """
        UPDATE mediator.health_connections
        SET status = 'disconnected',
            access_token_encrypted = NULL,
            refresh_token_encrypted = NULL,
            access_token_expires_at = NULL,
            refresh_token_expires_at = NULL,
            disconnected_at = COALESCE(disconnected_at, now()),
            revoked_at = COALESCE(revoked_at, now()),
            updated_at = now()
        WHERE id = $1 AND deleted_at IS NULL
        RETURNING status, granted_scopes, granted_at, last_success_at, last_error_at,
                  last_error_code, disconnected_at, revoked_at, deleted_at, updated_at
        """,
        current["id"],
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Withings connection not found")
    return {
        "provider": "withings",
        "status": "disconnected",
        "connection": _metadata_only_connection(row),
    }


@router.delete("/api/health/devices/withings")
async def withings_delete(
    request: Request,
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_health_current_user),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.health_sync_enabled:
        status_code, payload = _feature_disabled_response()
        return JSONResponse(status_code=status_code, content=payload)
    current = await _fetch_connection(pool, user_id)
    if current is None:
        raise HTTPException(status_code=404, detail="Withings connection not found")
    # Best-effort revoke/unsubscribe before local tear-down,
    # mirroring the disconnect intent.
    try:
        tokens = await load_connection_tokens(pool, connection_id=current["id"])
        if tokens.access_token:
            await _withings_provider(request, settings).revoke(
                access_token=tokens.access_token,
                refresh_token=tokens.refresh_token,
            )
    except Exception:
        pass
    try:
        repo = repository_for(pool)
        record = await repo.delete_connection_data(
            connection_id=current["id"],
            user_id=user_id,
        )
    except LookupError:
        raise HTTPException(status_code=404, detail="Withings connection not found")
    return {
        "provider": "withings",
        "status": "deleted",
        "connection": _metadata_only_connection(
            {
                "status": record.status,
                "granted_scopes": sorted(record.granted_scopes),
                "granted_at": record.updated_at,
                "last_success_at": None,
                "last_error_at": None,
                "last_error_code": None,
                "disconnected_at": None,
                "revoked_at": record.updated_at,
                "deleted_at": record.updated_at,
                "updated_at": record.updated_at,
            }
        ),
    }


@router.get("/api/health/devices/withings/export")
async def withings_export(
    pool: Any = Depends(get_pool),
    user_id: UUID = Depends(get_health_current_user),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if not settings.health_sync_enabled:
        status_code, payload = _feature_disabled_response()
        return JSONResponse(status_code=status_code, content=payload)
    row = await _fetch_connection(pool, user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Withings connection not found")
    return await export_withings_data(pool, user_id=user_id)
