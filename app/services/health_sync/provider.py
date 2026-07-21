"""Minimal provider interface for health-sync adapters."""

from __future__ import annotations

from typing import Protocol

from app.services.health_sync.models import (
    HealthFetchResult,
    HealthOAuthTokens,
    HealthProviderCapabilities,
    HealthResourceType,
    HealthSyncCursor,
)


class HealthSyncProvider(Protocol):
    name: str
    capabilities: HealthProviderCapabilities

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> HealthOAuthTokens: ...

    async def refresh_token(
        self,
        *,
        refresh_token: str,
    ) -> HealthOAuthTokens: ...

    async def fetch_changes(
        self,
        *,
        access_token: str,
        resource_type: HealthResourceType,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult: ...

    async def revoke(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
    ) -> None: ...


__all__ = ["HealthSyncProvider"]
