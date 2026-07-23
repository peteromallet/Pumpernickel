"""Periodic health-sync reconciliation helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from app.services.health_sync.models import (
    HealthProviderCapabilities,
    HealthResourceType,
    HealthSyncCursor,
    HealthSyncOutcome,
)
from app.services.health_sync.provider import HealthSyncProvider
from app.services.health_sync.repository import HealthSyncRepository
from app.services.health_sync.sync import (
    DEFAULT_SYNC_MAX_ATTEMPTS,
    DEFAULT_SYNC_RETRY_AFTER_CAP_SECONDS,
    sync_connection_resource_safely,
    sync_dirty_categories,
)
from app.services.health_sync.tokens import (
    HealthTokenStoreError,
    load_connection_tokens,
    refresh_connection_tokens,
)


DEFAULT_RECONCILIATION_BACKFILL_WINDOW = timedelta(days=30)


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _requested_resource_types(
    *,
    capabilities: HealthProviderCapabilities,
    resource_types: Iterable[HealthResourceType | str] | None,
) -> tuple[HealthResourceType, ...]:
    if resource_types is None:
        return tuple(category.resource_type for category in capabilities.categories)
    normalized = []
    seen: set[HealthResourceType] = set()
    for resource_type in resource_types:
        candidate = HealthResourceType(resource_type)
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return tuple(normalized)


def _eligible_resource_types(
    *,
    provider: HealthSyncProvider,
    granted_scopes: frozenset[str],
    resource_types: tuple[HealthResourceType, ...],
) -> tuple[HealthResourceType, ...]:
    if not granted_scopes:
        return resource_types
    eligible = []
    for resource_type in resource_types:
        required_scopes = provider.capabilities.category_for(resource_type).required_scopes
        if required_scopes.issubset(granted_scopes):
            eligible.append(resource_type)
    return tuple(eligible)


@dataclass(frozen=True, slots=True)
class HealthReconciliationSummary:
    outcomes: tuple[HealthSyncOutcome, ...]
    skipped_connection_ids: tuple[UUID, ...]
    scanned_connection_count: int


async def _load_active_access_token(pool: Any, *, connection_id: UUID) -> str:
    loaded = await load_connection_tokens(pool, connection_id=connection_id)
    if loaded.status != "active":
        raise HealthTokenStoreError(
            "Health connection is not active.",
            code="connection_inactive",
        )
    if not loaded.access_token:
        raise HealthTokenStoreError(
            "Health connection has no access token.",
            code="missing_access_token",
        )
    return loaded.access_token


async def reconcile_connections(
    *,
    pool: Any,
    repository: HealthSyncRepository,
    provider: HealthSyncProvider,
    claimed_by: str,
    connection_limit: int,
    dirty_limit: int | None = None,
    resource_types: Iterable[HealthResourceType | str] | None = None,
    backfill_window: timedelta = DEFAULT_RECONCILIATION_BACKFILL_WINDOW,
    max_attempts: int = DEFAULT_SYNC_MAX_ATTEMPTS,
    retry_after_cap_seconds: int = DEFAULT_SYNC_RETRY_AFTER_CAP_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: datetime | None = None,
) -> HealthReconciliationSummary:
    timestamp = _normalize_datetime(now or _utc_now()) or _utc_now()
    outcomes: list[HealthSyncOutcome] = []
    if dirty_limit is not None and dirty_limit > 0:
        outcomes.extend(
            await sync_dirty_categories(
                repository=repository,
                provider=provider,
                claimed_by=claimed_by,
                limit=dirty_limit,
                access_token_loader=lambda connection_id: _load_active_access_token(
                    pool,
                    connection_id=connection_id,
                ),
                max_attempts=max_attempts,
                retry_after_cap_seconds=retry_after_cap_seconds,
                sleep=sleep,
                now=timestamp,
            )
        )

    requested_resources = _requested_resource_types(
        capabilities=provider.capabilities,
        resource_types=resource_types,
    )
    skipped_connection_ids: list[UUID] = []
    connections = await repository.list_connections(
        provider=provider.capabilities.provider,
        limit=connection_limit,
    )
    for connection in connections:
        if connection.status != "active":
            skipped_connection_ids.append(connection.connection_id)
            continue
        try:
            tokens = await load_connection_tokens(pool, connection_id=connection.connection_id)
        except HealthTokenStoreError:
            skipped_connection_ids.append(connection.connection_id)
            continue
        if tokens.status != "active":
            skipped_connection_ids.append(connection.connection_id)
            continue
        # Refresh short-lived Withings access tokens before syncing.  Without
        # this, the periodic reconciliation path fetches with the expired
        # stored token and fails authentication — the failure that left recent
        # nights unsynced once the access token aged out and no webhook had
        # created dirty work for the worker's refresh-aware dirty path.
        access_token = tokens.access_token
        if not access_token or (
            tokens.access_token_expires_at is not None
            and tokens.access_token_expires_at <= timestamp
        ):
            try:
                refreshed = await refresh_connection_tokens(
                    pool,
                    connection_id=connection.connection_id,
                    provider=provider,
                    now=timestamp,
                )
            except HealthTokenStoreError:
                skipped_connection_ids.append(connection.connection_id)
                continue
            if refreshed.status != "active" or not refreshed.access_token:
                skipped_connection_ids.append(connection.connection_id)
                continue
            access_token = refreshed.access_token
        for resource_type in _eligible_resource_types(
            provider=provider,
            granted_scopes=tokens.granted_scopes,
            resource_types=requested_resources,
        ):
            stored_cursor = await repository.load_cursor(
                connection_id=connection.connection_id,
                resource_type=resource_type,
            )
            cursor_seed = None
            if stored_cursor is None:
                cursor_seed = HealthSyncCursor(
                    resource_type=resource_type,
                    last_modified=timestamp - backfill_window,
                )
            outcomes.append(
                await sync_connection_resource_safely(
                    repository=repository,
                    provider=provider,
                    connection_id=connection.connection_id,
                    user_id=connection.user_id,
                    access_token=access_token,
                    resource_type=resource_type,
                    cursor_seed=cursor_seed,
                    max_attempts=max_attempts,
                    retry_after_cap_seconds=retry_after_cap_seconds,
                    sleep=sleep,
                    now=timestamp,
                )
            )

    return HealthReconciliationSummary(
        outcomes=tuple(outcomes),
        skipped_connection_ids=tuple(skipped_connection_ids),
        scanned_connection_count=len(connections),
    )


__all__ = [
    "DEFAULT_RECONCILIATION_BACKFILL_WINDOW",
    "HealthReconciliationSummary",
    "reconcile_connections",
]
