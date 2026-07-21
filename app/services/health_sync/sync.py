"""Cursor-safe health ingestion helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
import inspect
from typing import Any
from uuid import UUID

from app.services.health_sync.models import (
    HealthResourceType,
    HealthSyncCursor,
    HealthSyncError,
    HealthSyncErrorKind,
    HealthSyncOutcome,
    HealthSyncStatus,
)
from app.services.health_sync.normalization import (
    normalize_measure_group,
    normalize_sleep_summary,
)
from app.services.health_sync.provider import HealthSyncProvider
from app.services.health_sync.repository import HealthDirtyCategory, HealthSyncRepository


DEFAULT_SYNC_MAX_ATTEMPTS = 3
DEFAULT_SYNC_RETRY_AFTER_CAP_SECONDS = 30


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class HealthSyncCursorError(RuntimeError):
    """Raised when stored cursor state cannot be used safely."""

    def __init__(self, detail: str, *, code: str = "invalid_cursor_state") -> None:
        super().__init__(detail)
        self.error = HealthSyncError.permanent_error(
            kind=HealthSyncErrorKind.INVALID_CURSOR_STATE,
            code=code,
            detail=detail,
        )


def apply_cursor_overlap(cursor: HealthSyncCursor | None) -> HealthSyncCursor | None:
    if cursor is None:
        return None
    if cursor.page_offset is not None:
        raise HealthSyncCursorError("persisted health cursor cannot retain an in-flight page offset")
    if cursor.last_modified is None:
        return HealthSyncCursor(
            resource_type=cursor.resource_type,
            etag=cursor.etag,
        )
    return HealthSyncCursor(
        resource_type=cursor.resource_type,
        last_modified=cursor.last_modified - cursor.overlap_window,
        etag=cursor.etag,
    )


def _cursor_for_persistence(
    *,
    resource_type: HealthResourceType,
    cursor_before: HealthSyncCursor | None,
    candidate: HealthSyncCursor | None,
) -> HealthSyncCursor | None:
    if candidate is None:
        return cursor_before
    last_modified = candidate.last_modified
    if (
        cursor_before is not None
        and cursor_before.last_modified is not None
        and (last_modified is None or last_modified < cursor_before.last_modified)
    ):
        last_modified = cursor_before.last_modified
    return HealthSyncCursor(
        resource_type=resource_type,
        last_modified=last_modified,
        etag=candidate.etag or (cursor_before.etag if cursor_before is not None else None),
    )


async def _resolve_access_token(
    loader: Callable[[UUID], str | Awaitable[str]],
    *,
    connection_id: UUID,
) -> str:
    token = loader(connection_id)
    if inspect.isawaitable(token):
        token = await token
    text = str(token).strip()
    if not text:
        raise ValueError("health sync access token loader returned an empty token")
    return text


def _normalized_cursor_seed(
    *,
    resource_type: HealthResourceType,
    cursor_seed: HealthSyncCursor | None,
) -> HealthSyncCursor | None:
    if cursor_seed is None:
        return None
    if cursor_seed.resource_type is not resource_type:
        raise HealthSyncCursorError("health cursor seed resource type does not match the requested resource")
    if cursor_seed.page_offset is not None:
        raise HealthSyncCursorError("health cursor seed cannot include an in-flight page offset")
    return cursor_seed


def _error_from_exception(exc: Exception) -> HealthSyncError:
    candidate = getattr(exc, "error", None)
    if isinstance(candidate, HealthSyncError):
        return candidate
    return HealthSyncError.permanent_error(
        code="sync_failed",
        detail="health sync failed",
    )


def _retry_delay_seconds(
    *,
    error: HealthSyncError,
    attempt: int,
    retry_after_cap_seconds: int,
) -> float | None:
    if not error.retryable:
        return None
    retry_cap = max(0, int(retry_after_cap_seconds))
    if error.retry_after_seconds is not None:
        if error.retry_after_seconds > retry_cap:
            return None
        return float(error.retry_after_seconds)
    if retry_cap <= 0:
        return None
    return float(min(2 ** max(0, attempt - 1), retry_cap))


def _failed_outcome(
    *,
    resource_type: HealthResourceType,
    cursor_before: HealthSyncCursor | None,
    error: HealthSyncError,
) -> HealthSyncOutcome:
    return HealthSyncOutcome(
        resource_type=resource_type,
        status=HealthSyncStatus.FAILED,
        cursor_before=cursor_before,
        cursor_after=cursor_before,
        error=error,
    )


async def sync_connection_resource(
    *,
    repository: HealthSyncRepository,
    provider: HealthSyncProvider,
    connection_id: UUID,
    user_id: UUID,
    access_token: str,
    resource_type: HealthResourceType | str,
    dirty_id: UUID | None = None,
    cursor_seed: HealthSyncCursor | None = None,
    now: datetime | None = None,
) -> HealthSyncOutcome:
    normalized_resource = HealthResourceType(resource_type)
    normalized_seed = _normalized_cursor_seed(
        resource_type=normalized_resource,
        cursor_seed=cursor_seed,
    )
    try:
        cursor_before = await repository.load_cursor(
            connection_id=connection_id,
            resource_type=normalized_resource,
        )
        fetch_cursor = apply_cursor_overlap(cursor_before)
    except HealthSyncCursorError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise HealthSyncCursorError("stored health cursor is malformed") from exc
    if fetch_cursor is None:
        fetch_cursor = normalized_seed

    page_count = 0
    fetched_count = 0
    deleted_count = 0
    records_to_store = []
    tombstones_to_store = []
    current_cursor = fetch_cursor
    cursor_candidate = cursor_before

    while True:
        result = await provider.fetch_changes(
            access_token=access_token.strip(),
            resource_type=normalized_resource,
            cursor=current_cursor,
        )
        page_count += 1
        fetched_count += len(result.records) + len(result.tombstones)
        deleted_count += len(result.tombstones)
        records_to_store.extend(result.records)
        tombstones_to_store.extend(result.tombstones)
        if result.next_cursor is not None:
            cursor_candidate = result.next_cursor
        if not result.has_more:
            break
        if result.next_cursor is None:
            raise RuntimeError("health provider reported pagination without a continuation cursor")
        current_cursor = result.next_cursor

    timestamp = _normalize_datetime(now or _utc_now())
    assert timestamp is not None
    cursor_after = _cursor_for_persistence(
        resource_type=normalized_resource,
        cursor_before=cursor_before,
        candidate=cursor_candidate,
    )

    normalized_inserted = 0
    async with repository.transaction() as connection:
        for record in records_to_store:
            stored = await repository.upsert_source_record(
                connection_id=connection_id,
                user_id=user_id,
                record=record,
                now=timestamp,
                executor=connection,
            )
            if (
                record.resource_type == HealthResourceType.MEASUREMENT
                and not record.is_deleted
            ):
                measures = record.source_metadata.get("measures")
                if measures:
                    date_epoch = record.source_metadata.get("date")
                    measured_at = (
                        datetime.fromtimestamp(int(date_epoch), tz=UTC)
                        if date_epoch is not None
                        else (record.observed_at or timestamp)
                    )
                    normalized_rows = normalize_measure_group(
                        measures,
                        measured_at=measured_at,
                        source_timezone=record.source_timezone,
                        source_device_id=record.source_device_id,
                        source_device_model=record.source_device_model,
                        attribution=dict(record.attribution),
                    )
                    if normalized_rows:
                        await repository.replace_normalized_measurements(
                            source_record_id=stored.record_id,
                            connection_id=connection_id,
                            user_id=user_id,
                            measurements=normalized_rows,
                            executor=connection,
                        )
                        normalized_inserted += len(normalized_rows)
            elif (
                record.resource_type == HealthResourceType.SLEEP
                and not record.is_deleted
            ):
                normalized_sleep = normalize_sleep_summary(
                    record,
                    revision_count=stored.revision_count,
                )
                if normalized_sleep is not None:
                    await repository.replace_normalized_sleep(
                        source_record_id=stored.record_id,
                        connection_id=connection_id,
                        user_id=user_id,
                        started_at=normalized_sleep.started_at,
                        ended_at=normalized_sleep.ended_at,
                        local_sleep_date=normalized_sleep.local_sleep_date,
                        local_timezone=normalized_sleep.local_timezone,
                        local_offset_seconds=normalized_sleep.local_offset_seconds,
                        completeness_state=normalized_sleep.completeness_state,
                        total_in_bed_seconds=normalized_sleep.total_in_bed_seconds,
                        total_asleep_seconds=normalized_sleep.total_asleep_seconds,
                        awake_seconds=normalized_sleep.awake_seconds,
                        light_sleep_seconds=normalized_sleep.light_sleep_seconds,
                        deep_sleep_seconds=normalized_sleep.deep_sleep_seconds,
                        rem_sleep_seconds=normalized_sleep.rem_sleep_seconds,
                        sleep_latency_seconds=normalized_sleep.sleep_latency_seconds,
                        wake_after_sleep_onset_seconds=normalized_sleep.wake_after_sleep_onset_seconds,
                        wakeups=normalized_sleep.wakeups,
                        sleep_score=normalized_sleep.sleep_score,
                        source_device_id=normalized_sleep.source_device_id,
                        source_device_model=normalized_sleep.source_device_model,
                        attribution=normalized_sleep.attribution,
                        executor=connection,
                    )
        for tombstone in tombstones_to_store:
            stored = await repository.tombstone_source_record(
                connection_id=connection_id,
                user_id=user_id,
                tombstone=tombstone,
                now=timestamp,
                executor=connection,
            )
            if tombstone.resource_type == HealthResourceType.MEASUREMENT:
                await repository.delete_normalized_measurements(
                    source_record_id=stored.record_id,
                    connection_id=connection_id,
                    user_id=user_id,
                    executor=connection,
                )
            elif tombstone.resource_type == HealthResourceType.SLEEP:
                await repository.delete_normalized_sleep(
                    source_record_id=stored.record_id,
                    connection_id=connection_id,
                    user_id=user_id,
                    executor=connection,
                )
            elif tombstone.resource_type == HealthResourceType.WORKOUT:
                pass
        if cursor_after is not None:
            await repository.store_cursor(
                connection_id=connection_id,
                cursor=cursor_after,
                now=timestamp,
                executor=connection,
            )
        if dirty_id is not None:
            await repository.clear_dirty_category(
                dirty_id=dirty_id,
                cleared_at=timestamp,
                executor=connection,
            )

    return HealthSyncOutcome(
        resource_type=normalized_resource,
        status=HealthSyncStatus.COMPLETED,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        page_count=page_count,
        fetched_count=fetched_count,
        inserted_count=len(records_to_store),
        deleted_count=deleted_count,
        tombstones=tuple(tombstones_to_store),
    )


async def sync_connection_resource_safely(
    *,
    repository: HealthSyncRepository,
    provider: HealthSyncProvider,
    connection_id: UUID,
    user_id: UUID,
    access_token: str,
    resource_type: HealthResourceType | str,
    dirty_id: UUID | None = None,
    cursor_seed: HealthSyncCursor | None = None,
    max_attempts: int = DEFAULT_SYNC_MAX_ATTEMPTS,
    retry_after_cap_seconds: int = DEFAULT_SYNC_RETRY_AFTER_CAP_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: datetime | None = None,
) -> HealthSyncOutcome:
    normalized_resource = HealthResourceType(resource_type)
    timestamp = _normalize_datetime(now or _utc_now()) or _utc_now()
    initial_cursor_before: HealthSyncCursor | None = None

    for attempt in range(1, max(1, int(max_attempts)) + 1):
        try:
            outcome = await sync_connection_resource(
                repository=repository,
                provider=provider,
                connection_id=connection_id,
                user_id=user_id,
                access_token=access_token,
                resource_type=normalized_resource,
                dirty_id=dirty_id,
                cursor_seed=cursor_seed,
                now=timestamp,
            )
            await repository.record_sync_success(
                connection_id=connection_id,
                synced_at=timestamp,
            )
            return outcome
        except HealthSyncCursorError as exc:
            if initial_cursor_before is None:
                try:
                    initial_cursor_before = await repository.load_cursor(
                        connection_id=connection_id,
                        resource_type=normalized_resource,
                    )
                except (KeyError, TypeError, ValueError):
                    initial_cursor_before = None
            await repository.record_sync_error(
                connection_id=connection_id,
                error=exc.error,
                errored_at=timestamp,
            )
            return _failed_outcome(
                resource_type=normalized_resource,
                cursor_before=initial_cursor_before,
                error=exc.error,
            )
        except Exception as exc:
            error = _error_from_exception(exc)
            if initial_cursor_before is None:
                try:
                    initial_cursor_before = await repository.load_cursor(
                        connection_id=connection_id,
                        resource_type=normalized_resource,
                    )
                except (KeyError, TypeError, ValueError):
                    initial_cursor_before = None
            delay_seconds = _retry_delay_seconds(
                error=error,
                attempt=attempt,
                retry_after_cap_seconds=retry_after_cap_seconds,
            )
            if delay_seconds is not None and attempt < max(1, int(max_attempts)):
                await sleep(delay_seconds)
                continue
            await repository.record_sync_error(
                connection_id=connection_id,
                error=error,
                errored_at=timestamp,
            )
            return _failed_outcome(
                resource_type=normalized_resource,
                cursor_before=initial_cursor_before,
                error=error,
            )


async def sync_claimed_dirty_category(
    *,
    repository: HealthSyncRepository,
    provider: HealthSyncProvider,
    dirty_category: HealthDirtyCategory,
    access_token: str,
    max_attempts: int = DEFAULT_SYNC_MAX_ATTEMPTS,
    retry_after_cap_seconds: int = DEFAULT_SYNC_RETRY_AFTER_CAP_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: datetime | None = None,
) -> HealthSyncOutcome:
    return await sync_connection_resource_safely(
        repository=repository,
        provider=provider,
        connection_id=dirty_category.connection_id,
        user_id=dirty_category.user_id,
        access_token=access_token,
        resource_type=dirty_category.resource_type,
        dirty_id=dirty_category.dirty_id,
        max_attempts=max_attempts,
        retry_after_cap_seconds=retry_after_cap_seconds,
        sleep=sleep,
        now=now,
    )


async def sync_dirty_categories(
    *,
    repository: HealthSyncRepository,
    provider: HealthSyncProvider,
    claimed_by: str,
    limit: int,
    access_token_loader: Callable[[UUID], str | Awaitable[str]],
    max_attempts: int = DEFAULT_SYNC_MAX_ATTEMPTS,
    retry_after_cap_seconds: int = DEFAULT_SYNC_RETRY_AFTER_CAP_SECONDS,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    now: datetime | None = None,
) -> tuple[HealthSyncOutcome, ...]:
    claimed = await repository.claim_dirty_categories(
        claimed_by=claimed_by,
        limit=limit,
        now=now,
    )
    outcomes = []
    for dirty_category in claimed:
        try:
            access_token = await _resolve_access_token(
                access_token_loader,
                connection_id=dirty_category.connection_id,
            )
        except Exception as exc:
            error = _error_from_exception(exc if isinstance(exc, Exception) else RuntimeError("token loader failed"))
            await repository.record_sync_error(
                connection_id=dirty_category.connection_id,
                error=error,
                errored_at=_normalize_datetime(now or _utc_now()) or _utc_now(),
            )
            outcomes.append(
                _failed_outcome(
                    resource_type=dirty_category.resource_type,
                    cursor_before=None,
                    error=error,
                )
            )
            continue
        outcomes.append(
            await sync_claimed_dirty_category(
                repository=repository,
                provider=provider,
                dirty_category=dirty_category,
                access_token=access_token,
                max_attempts=max_attempts,
                retry_after_cap_seconds=retry_after_cap_seconds,
                sleep=sleep,
                now=now,
            )
        )
    return tuple(outcomes)


__all__ = [
    "HealthSyncCursorError",
    "DEFAULT_SYNC_MAX_ATTEMPTS",
    "DEFAULT_SYNC_RETRY_AFTER_CAP_SECONDS",
    "apply_cursor_overlap",
    "sync_claimed_dirty_category",
    "sync_connection_resource",
    "sync_connection_resource_safely",
    "sync_dirty_categories",
]
