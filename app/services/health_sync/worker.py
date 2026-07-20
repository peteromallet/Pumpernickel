"""Background poll worker for health-sync maintenance."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.config import Settings, get_settings
from app.services.health_sync.models import (
    HealthResourceType,
    HealthSyncError,
    HealthSyncErrorKind,
)
from app.services.health_sync.reconciliation import reconcile_connections
from app.services.health_sync.repository import HealthDirtyCategory, HealthSyncRepository, repository_for
from app.services.health_sync.sync import sync_claimed_dirty_category
from app.services.health_sync.tokens import (
    HealthTokenStoreError,
    load_connection_tokens,
    refresh_connection_tokens,
)
from app.services.health_sync.withings import WithingsProvider

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_datetime(value: datetime | None) -> datetime:
    if value is None:
        return _utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def health_sync_resource_types(settings: Settings) -> tuple[HealthResourceType, ...]:
    resource_types: list[HealthResourceType] = []
    if settings.health_sync_measurements_enabled:
        resource_types.append(HealthResourceType.MEASUREMENT)
    if settings.health_sync_workouts_enabled:
        resource_types.append(HealthResourceType.WORKOUT)
    if settings.health_sync_sleep_enabled:
        resource_types.append(HealthResourceType.SLEEP)
    return tuple(resource_types)


def _token_error_to_sync_error(exc: HealthTokenStoreError) -> HealthSyncError:
    if exc.reauth_required:
        return HealthSyncError.permanent_error(
            kind=HealthSyncErrorKind.AUTHENTICATION,
            code=exc.code,
            detail=str(exc),
        )
    if exc.retryable:
        return HealthSyncError.retryable_error(
            code=exc.code,
            detail=str(exc),
            kind=HealthSyncErrorKind.TRANSIENT,
        )
    return HealthSyncError.permanent_error(
        code=exc.code,
        detail=str(exc),
    )


@dataclass(frozen=True, slots=True)
class HealthSyncWorkerResult:
    claimed: int = 0
    synced: int = 0
    failed: int = 0
    skipped_disabled: int = 0
    reconciliation_outcomes: int = 0
    skipped_connections: int = 0
    scanned_connections: int = 0


class HealthSyncWorker:
    """Poll dirty health categories and periodic reconciliation."""

    def __init__(
        self,
        pool,
        *,
        settings: Settings | None = None,
        provider=None,
        repository: HealthSyncRepository | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.pool = pool
        self.settings = settings or get_settings()
        self.repository = repository or repository_for(pool)
        self.provider = provider or WithingsProvider(
            client_id=self.settings.withings_client_id.get_secret_value(),
            client_secret=self.settings.withings_client_secret.get_secret_value(),
            api_base_url=self.settings.withings_api_endpoint,
            request_timeout_seconds=self.settings.health_sync_request_timeout_s,
        )
        self.worker_id = worker_id or f"health-sync-{uuid4()}"
        self.resource_types = frozenset(health_sync_resource_types(self.settings))
        self._last_reconciliation_at: datetime | None = None

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("health sync worker tick failed")
            await asyncio.sleep(self.settings.health_sync_poll_interval_s)

    async def run_once(self, *, now: datetime | None = None) -> HealthSyncWorkerResult:
        timestamp = _normalize_datetime(now)
        if not self.resource_types:
            return HealthSyncWorkerResult()

        claimed = await self.repository.claim_dirty_categories(
            claimed_by=self.worker_id,
            limit=self.settings.health_sync_batch_size,
            now=timestamp,
        )
        synced = 0
        failed = 0
        skipped_disabled = 0
        for dirty_category in claimed:
            if dirty_category.resource_type not in self.resource_types:
                await self.repository.clear_dirty_category(
                    dirty_id=dirty_category.dirty_id,
                    cleared_at=timestamp,
                )
                skipped_disabled += 1
                continue
            outcome = await self._sync_dirty_category(
                dirty_category=dirty_category,
                now=timestamp,
            )
            if outcome:
                synced += 1
            else:
                failed += 1

        reconciliation_outcomes = 0
        skipped_connections = 0
        scanned_connections = 0
        if self._should_reconcile(timestamp):
            summary = await reconcile_connections(
                pool=self.pool,
                repository=self.repository,
                provider=self.provider,
                claimed_by=self.worker_id,
                connection_limit=self.settings.health_sync_batch_size,
                dirty_limit=0,
                resource_types=tuple(self.resource_types),
                max_attempts=self.settings.health_sync_max_attempts,
                retry_after_cap_seconds=self.settings.health_sync_retry_after_cap_seconds,
                now=timestamp,
            )
            self._last_reconciliation_at = timestamp
            reconciliation_outcomes = len(summary.outcomes)
            skipped_connections = len(summary.skipped_connection_ids)
            scanned_connections = summary.scanned_connection_count

        return HealthSyncWorkerResult(
            claimed=len(claimed),
            synced=synced,
            failed=failed,
            skipped_disabled=skipped_disabled,
            reconciliation_outcomes=reconciliation_outcomes,
            skipped_connections=skipped_connections,
            scanned_connections=scanned_connections,
        )

    async def _sync_dirty_category(
        self,
        *,
        dirty_category: HealthDirtyCategory,
        now: datetime,
    ) -> bool:
        try:
            access_token = await self._load_access_token(
                connection_id=dirty_category.connection_id,
                now=now,
            )
        except HealthTokenStoreError as exc:
            await self.repository.record_sync_error(
                connection_id=dirty_category.connection_id,
                error=_token_error_to_sync_error(exc),
                errored_at=now,
            )
            return False

        outcome = await sync_claimed_dirty_category(
            repository=self.repository,
            provider=self.provider,
            dirty_category=dirty_category,
            access_token=access_token,
            max_attempts=self.settings.health_sync_max_attempts,
            retry_after_cap_seconds=self.settings.health_sync_retry_after_cap_seconds,
            now=now,
        )
        return outcome.error is None

    async def _load_access_token(
        self,
        *,
        connection_id: UUID,
        now: datetime,
    ) -> str:
        tokens = await load_connection_tokens(self.pool, connection_id=connection_id)
        if tokens.status != "active":
            raise HealthTokenStoreError(
                "Health connection is not active.",
                code="connection_inactive",
            )
        if tokens.access_token and (
            tokens.access_token_expires_at is None or tokens.access_token_expires_at > now
        ):
            return tokens.access_token
        refreshed = await refresh_connection_tokens(
            self.pool,
            connection_id=connection_id,
            provider=self.provider,
            now=now,
        )
        if refreshed.status != "active" or not refreshed.access_token:
            raise HealthTokenStoreError(
                "Health connection has no access token.",
                code="missing_access_token",
                reauth_required=refreshed.status == "reauth_required",
            )
        return refreshed.access_token

    def _should_reconcile(self, now: datetime) -> bool:
        if self._last_reconciliation_at is None:
            return True
        return (
            now - self._last_reconciliation_at
        ).total_seconds() >= self.settings.health_sync_reconciliation_interval_s


__all__ = [
    "HealthSyncWorker",
    "HealthSyncWorkerResult",
    "health_sync_resource_types",
]
