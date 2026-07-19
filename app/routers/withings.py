"""Withings integration router — fail-closed stubs.

Token exchange, state validation, and persistent ingestion are not
implemented yet.  Every data-path endpoint returns 503 so that Withings
does not silently accept a URL and start sending notifications into a
black hole.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

router = APIRouter()

_NOT_IMPLEMENTED_BODY = {
    "status": "unavailable",
    "detail": (
        "Withings integration is not implemented yet. "
        "The URL is registered but token exchange and notification "
        "ingestion are pending a future release."
    ),
}

# ── OAuth callback ──────────────────────────────────────────────────
# Withings calls this with ?code=<code>&state=<state> after the user
# authorises access.  We must never echo or log the authorization code
# or state, so the 503 response body is static.


@router.head("/api/health/devices/withings/oauth/callback")
async def oauth_callback_head() -> Response:
    """URL validation — Withings checks reachability before first use."""
    return Response(status_code=204)


@router.get("/api/health/devices/withings/oauth/callback")
async def oauth_callback_get() -> JSONResponse:
    """Fail-closed: token exchange / state validation not implemented."""
    return JSONResponse(status_code=503, content=_NOT_IMPLEMENTED_BODY)


# ── Notification webhook ─────────────────────────────────────────────
# Withings POSTs application/x-www-form-urlencoded payloads here when
# new device data is available.  We reject every request until the
# durable ingestion pipeline exists.


@router.head("/api/health/devices/withings/notifications")
async def notifications_head() -> Response:
    """URL validation — Withings checks reachability before first use."""
    return Response(status_code=204)


@router.post("/api/health/devices/withings/notifications")
async def notifications_post(request: Request) -> JSONResponse:
    """Fail-closed: persistent ingestion / queuing not implemented.

    The body is explicitly *not* read or echoed to avoid reflecting
    sensitive data in logs or response payloads.
    """
    return JSONResponse(status_code=503, content=_NOT_IMPLEMENTED_BODY)
