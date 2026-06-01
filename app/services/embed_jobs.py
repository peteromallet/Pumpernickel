"""Idempotent enqueue helpers for async embedding work."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID


EmbedJobKind = Literal["embed", "reembed", "drop"]
EmbedJobStatus = Literal["pending", "processing", "succeeded", "failed", "skipped", "superseded", "cancelled"]
EmbedJobAction = Literal["created", "existing"]

_HASH_LENGTH = 64


@dataclass(frozen=True)
class EmbedJob:
    id: UUID
    message_id: UUID
    job_kind: EmbedJobKind
    status: EmbedJobStatus
    model: str | None
    dimension: int | None
    content_hash: str | None
    attempts: int
    next_attempt_at: datetime
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class EnqueueEmbedJobResult:
    job: EmbedJob
    action: EmbedJobAction
    superseded_pending: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _coerce_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _validate_content_hash(value: str) -> str:
    normalized = value.casefold()
    if len(normalized) != _HASH_LENGTH or any(char not in "0123456789abcdef" for char in normalized):
        raise ValueError("content_hash must be a lowercase 64-character SHA-256 hex digest")
    return normalized


def _validate_model_dimension(model: str | None, dimension: int | None, *, job_kind: EmbedJobKind) -> None:
    if job_kind == "drop":
        return
    if not model:
        raise ValueError("model is required for embed and reembed jobs")
    if dimension is None or dimension <= 0:
        raise ValueError("positive dimension is required for embed and reembed jobs")


def _row_to_job(row: Any) -> EmbedJob:
    return EmbedJob(
        id=row["id"],
        message_id=row["message_id"],
        job_kind=row["job_kind"],
        status=row["status"],
        model=row["model"],
        dimension=row["dimension"],
        content_hash=row["content_hash"],
        attempts=row["attempts"],
        next_attempt_at=row["next_attempt_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


@asynccontextmanager
async def _connection(pool_or_conn: Any) -> AsyncIterator[Any]:
    acquire = getattr(pool_or_conn, "acquire", None)
    if acquire is None:
        yield pool_or_conn
        return
    async with acquire() as conn:
        yield conn


async def enqueue_embed_job(
    pool_or_conn: Any,
    *,
    message_id: UUID,
    content_hash: str,
    model: str,
    dimension: int,
    now: datetime | None = None,
) -> EnqueueEmbedJobResult:
    """Ensure one active pending/processing embed job exists for this content hash."""

    return await _enqueue_job(
        pool_or_conn,
        message_id=message_id,
        job_kind="embed",
        content_hash=content_hash,
        model=model,
        dimension=dimension,
        now=now,
    )


async def enqueue_reembed_job(
    pool_or_conn: Any,
    *,
    message_id: UUID,
    content_hash: str,
    model: str,
    dimension: int,
    now: datetime | None = None,
) -> EnqueueEmbedJobResult:
    """Ensure one active pending/processing reembed job exists for this content hash."""

    return await _enqueue_job(
        pool_or_conn,
        message_id=message_id,
        job_kind="reembed",
        content_hash=content_hash,
        model=model,
        dimension=dimension,
        now=now,
    )


async def enqueue_drop_embedding_job(
    pool_or_conn: Any,
    *,
    message_id: UUID,
    now: datetime | None = None,
) -> EnqueueEmbedJobResult:
    """Ensure one active drop job exists and cancel obsolete pending embed work."""

    return await _enqueue_job(
        pool_or_conn,
        message_id=message_id,
        job_kind="drop",
        content_hash=None,
        model=None,
        dimension=None,
        now=now,
    )


async def _enqueue_job(
    pool_or_conn: Any,
    *,
    message_id: UUID,
    job_kind: EmbedJobKind,
    content_hash: str | None,
    model: str | None,
    dimension: int | None,
    now: datetime | None,
) -> EnqueueEmbedJobResult:
    timestamp = _coerce_aware_utc(now or _utc_now())
    if job_kind == "drop":
        normalized_hash = None
    elif content_hash is None:
        raise ValueError("content_hash is required for embed and reembed jobs")
    else:
        normalized_hash = _validate_content_hash(content_hash)
    _validate_model_dimension(model, dimension, job_kind=job_kind)

    async with _connection(pool_or_conn) as conn:
        superseded = await _supersede_obsolete_pending(
            conn,
            message_id=message_id,
            job_kind=job_kind,
            content_hash=normalized_hash,
            now=timestamp,
        )
        existing = await _fetch_active_job(
            conn,
            message_id=message_id,
            job_kind=job_kind,
            content_hash=normalized_hash,
        )
        if existing is not None:
            return EnqueueEmbedJobResult(
                job=_row_to_job(existing),
                action="existing",
                superseded_pending=superseded,
            )
        inserted = await _insert_job(
            conn,
            message_id=message_id,
            job_kind=job_kind,
            content_hash=normalized_hash,
            model=model,
            dimension=dimension,
            now=timestamp,
        )
        return EnqueueEmbedJobResult(
            job=_row_to_job(inserted),
            action="created",
            superseded_pending=superseded,
        )


async def _supersede_obsolete_pending(
    conn: Any,
    *,
    message_id: UUID,
    job_kind: EmbedJobKind,
    content_hash: str | None,
    now: datetime,
) -> int:
    if job_kind == "drop":
        status = await conn.execute(
            """
            UPDATE mediator.embed_jobs
            SET status = 'cancelled',
                last_error = 'superseded by drop job',
                locked_at = NULL,
                locked_by = NULL,
                updated_at = $2,
                completed_at = $2
            WHERE message_id = $1
              AND job_kind IN ('embed', 'reembed')
              AND status = 'pending'
            """,
            message_id,
            now,
        )
        return _rows_affected(status)

    status = await conn.execute(
        """
        UPDATE mediator.embed_jobs
        SET status = 'superseded',
            last_error = 'superseded by newer content hash',
            locked_at = NULL,
            locked_by = NULL,
            updated_at = $3,
            completed_at = $3
        WHERE message_id = $1
          AND job_kind IN ('embed', 'reembed')
          AND status = 'pending'
          AND content_hash IS DISTINCT FROM $2
        """,
        message_id,
        content_hash,
        now,
    )
    return _rows_affected(status)


async def _fetch_active_job(
    conn: Any,
    *,
    message_id: UUID,
    job_kind: EmbedJobKind,
    content_hash: str | None,
) -> Any | None:
    return await conn.fetchrow(
        """
        SELECT id, message_id, job_kind, status, model, dimension, content_hash,
               attempts, next_attempt_at, created_at, updated_at
        FROM mediator.embed_jobs
        WHERE message_id = $1
          AND job_kind = $2
          AND status IN ('pending', 'processing')
          AND content_hash IS NOT DISTINCT FROM $3
        ORDER BY created_at ASC, id ASC
        LIMIT 1
        """,
        message_id,
        job_kind,
        content_hash,
    )


async def _insert_job(
    conn: Any,
    *,
    message_id: UUID,
    job_kind: EmbedJobKind,
    content_hash: str | None,
    model: str | None,
    dimension: int | None,
    now: datetime,
) -> Any:
    return await conn.fetchrow(
        """
        INSERT INTO mediator.embed_jobs (
            message_id, job_kind, status, model, dimension, content_hash,
            attempts, last_error, next_attempt_at, locked_at, locked_by,
            created_at, updated_at, completed_at
        )
        VALUES ($1, $2, 'pending', $3, $4, $5, 0, NULL, $6, NULL, NULL, $6, $6, NULL)
        RETURNING id, message_id, job_kind, status, model, dimension, content_hash,
                  attempts, next_attempt_at, created_at, updated_at
        """,
        message_id,
        job_kind,
        model,
        dimension,
        content_hash,
        now,
    )


def _rows_affected(status: str) -> int:
    try:
        return int(status.rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0
