"""Message write-path hooks for asynchronous embedding jobs."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.services.embed_jobs import (
    enqueue_drop_embedding_job,
    enqueue_embed_job,
    enqueue_reembed_job,
)
from app.services.embeddings import canonical_content_hash

logger = logging.getLogger(__name__)


async def enqueue_message_embed(
    pool: Any,
    *,
    message_id: UUID,
    content: str | None,
    media_analysis: Mapping[str, Any] | None = None,
) -> None:
    """Best-effort enqueue after a real message row is created."""

    settings = get_settings()
    try:
        await enqueue_embed_job(
            pool,
            message_id=message_id,
            content_hash=canonical_content_hash(content, media_analysis),
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
    except Exception:
        logger.exception("failed to enqueue embed job for message_id=%s", message_id)


async def enqueue_message_reembed(
    pool: Any,
    *,
    message_id: UUID,
    content: str | None,
    media_analysis: Mapping[str, Any] | None = None,
) -> None:
    """Best-effort enqueue after canonical searchable text changes."""

    settings = get_settings()
    try:
        await enqueue_reembed_job(
            pool,
            message_id=message_id,
            content_hash=canonical_content_hash(content, media_analysis),
            model=settings.embedding_model,
            dimension=settings.embedding_dimension,
        )
    except Exception:
        logger.exception("failed to enqueue reembed job for message_id=%s", message_id)


async def enqueue_message_embedding_drop(pool: Any, *, message_id: UUID) -> None:
    """Best-effort enqueue after a message leaves the searchable lifecycle."""

    try:
        await enqueue_drop_embedding_job(pool, message_id=message_id)
    except Exception:
        logger.exception("failed to enqueue drop embedding job for message_id=%s", message_id)
