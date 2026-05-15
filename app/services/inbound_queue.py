"""
Provider-agnostic inbound queue transition helpers.

Queue states and legal transitions
-----------------------------------
States (as enforced by the messages_processing_state_check constraint):

  raw         Stored, not yet claimed by any worker.
  deferred    Intentionally waiting for coalescing or pacing.
  processing  Claimed by a worker/turn; actively being handled.
  processed   Successfully handled by a completed turn.
  expired     Intentionally no longer needs direct handling.
  failed      Attempted and failed; retryable or inspectable.
  withheld    Legacy state retained for existing rows (no new rows enter this).

Legal transitions:

  raw ──────────────────> processing   (claim_messages_for_turn)
  raw ──────────────────> deferred     (defer_messages)
  raw ──────────────────> expired      (expire_messages — past retention)
  deferred ─────────────> processing   (claim_messages_for_turn)
  processing ───────────> processed    (complete_messages)
  processing ───────────> failed       (fail_messages)
  processing ───────────> raw          (recover_stale_processing)
  failed ───────────────> raw          (recover_retryable_failed)
  failed ───────────────> expired      (past retention or retries exhausted,
                                        handled separately by sweeper expiry logic)

Terminal states: processed, expired, withheld.
Once a row reaches a terminal state it MUST NOT be retried or re-enqueued.

All helpers in this module include ``direction='inbound'`` guards.
Outbound rows are never touched by these functions.

Design decisions
----------------
See SD-001 through SD-007 in the durable-inbound-queue-hardening brief.

- claim_messages_for_turn uses an atomic CTE (UPDATE ... WHERE ... RETURNING)
  to prevent two workers from claiming the same row.
- handled_by_turn_id serves double duty: set during claim (active processing
  turn) and during completion (terminal handled-by metadata).  The sweeper
  MUST check processing_state, not just handled_by_turn_id, to decide whether
  a row needs recovery (see DEBT-095).
- Silencing / reaction paths that never open a bot_turns row call
  complete_messages with handled_by_turn_id=None (see DEBT-097).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ── claim ────────────────────────────────────────────────────────────────────

async def claim_messages_for_turn(
    pool: Any,
    message_ids: list[UUID],
    *,
    bot_id: str,
    topic_id: UUID,
) -> list[UUID]:
    """Atomically claim eligible inbound messages for a turn.

    Only rows matching ALL of these conditions are claimed:
    - ``direction = 'inbound'``
    - ``processing_state IN ('raw', 'deferred')``
    - ``bot_id`` and ``topic_id`` match the caller's scope
    - ``processing_started_at IS NULL`` or older than 5 minutes (stale claim)

    Claimed rows are immediately:
    - Set to ``processing_state = 'processing'``
    - Stamped with ``processing_started_at = now()``
    - Have ``processing_attempts`` incremented
    - Have ``processing_error`` cleared

    Returns only the ids that were *actually* claimed (race-resistant:
    rows that did not match the WHERE at COMMIT time are silently excluded).
    """
    if not message_ids:
        return []

    rows = await pool.fetch(
        """
        WITH claimed AS (
            UPDATE messages
            SET processing_state      = 'processing',
                processing_started_at = now(),
                processing_attempts   = processing_attempts + 1,
                processing_error      = NULL
            WHERE id = ANY($1::uuid[])
              AND direction = 'inbound'
              AND processing_state IN ('raw', 'deferred')
              AND (processing_started_at IS NULL
                   OR processing_started_at < now() - interval '5 minutes')
              AND bot_id = $2
              AND topic_id = $3
            RETURNING id
        )
        SELECT id FROM claimed
        """,
        message_ids,
        bot_id,
        topic_id,
    )
    claimed = [row["id"] for row in rows]
    unclaimed = len(message_ids) - len(claimed)
    if unclaimed:
        logger.debug(
            "claim_messages_for_turn: claimed=%d unclaimed=%d bot_id=%s topic_id=%s",
            len(claimed),
            unclaimed,
            bot_id,
            str(topic_id),
        )
    return claimed


# ── terminal completion ──────────────────────────────────────────────────────

async def complete_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    handling_result: str,
    handled_by_turn_id: UUID | None = None,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Mark inbound messages as successfully handled.

    Parameters
    ----------
    handling_result : str
        One of 'replied', 'silent', 'withheld_newer_inbound', 'no_action'.
    handled_by_turn_id : UUID | None
        The bot_turns row that handled these messages.  May be ``None`` for
        pacer/debouncer silence/react paths that never open a turn.
    """
    if not message_ids:
        return 0

    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state   = 'processed',
            handling_result    = $4,
            handled_at         = now(),
            handled_by_turn_id = $5
        WHERE id = ANY($1::uuid[])
          AND direction = 'inbound'
          AND bot_id = $2
          AND topic_id = $3
        """,
        message_ids,
        bot_id,
        topic_id,
        handling_result,
        handled_by_turn_id,
    )
    # asyncpg execute returns a string like "UPDATE N" — parse the count
    updated = _parse_update_count(result)
    if updated:
        logger.debug(
            "complete_messages: updated=%d result=%s bot_id=%s topic_id=%s",
            updated,
            handling_result,
            bot_id,
            str(topic_id),
        )
    return updated


# ── failure ──────────────────────────────────────────────────────────────────

async def fail_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    processing_error: str,
    handled_by_turn_id: UUID | None = None,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Mark inbound messages as failed with an error description.

    Sets ``handling_result = 'failed'`` and stamps ``handled_at`` only when
    a turn_id is provided (i.e. the turn existed and completed enough to
    record the failure).  Otherwise the row stays in 'failed' for future
    sweeper recovery.
    """
    if not message_ids:
        return 0

    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state   = 'failed',
            processing_error   = $4,
            handling_result    = 'failed',
            handled_by_turn_id = COALESCE($5, handled_by_turn_id),
            handled_at         = CASE WHEN $5 IS NOT NULL THEN now()
                                      ELSE handled_at END
        WHERE id = ANY($1::uuid[])
          AND direction = 'inbound'
          AND bot_id = $2
          AND topic_id = $3
        """,
        message_ids,
        bot_id,
        topic_id,
        processing_error,
        handled_by_turn_id,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.debug(
            "fail_messages: updated=%d error=%s bot_id=%s topic_id=%s",
            updated,
            processing_error[:120],
            bot_id,
            str(topic_id),
        )
    return updated


# ── defer ────────────────────────────────────────────────────────────────────

async def defer_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Move inbound messages to ``deferred`` state (e.g. for spend-cap deferral).

    Rows in 'deferred' are eligible for later re-claim by claim_messages_for_turn.
    """
    if not message_ids:
        return 0

    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state = 'deferred'
        WHERE id = ANY($1::uuid[])
          AND direction = 'inbound'
          AND bot_id = $2
          AND topic_id = $3
        """,
        message_ids,
        bot_id,
        topic_id,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.debug(
            "defer_messages: updated=%d bot_id=%s topic_id=%s",
            updated,
            bot_id,
            str(topic_id),
        )
    return updated


# ── expire ───────────────────────────────────────────────────────────────────

async def expire_messages(
    pool: Any,
    message_ids: list[UUID],
    *,
    bot_id: str,
    topic_id: UUID,
) -> int:
    """Mark inbound messages as ``expired`` (outside retention, no longer needed).

    Terminal state — rows are not eligible for recovery or retry.
    """
    if not message_ids:
        return 0

    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state = 'expired',
            handling_result  = 'expired',
            handled_at       = now()
        WHERE id = ANY($1::uuid[])
          AND direction = 'inbound'
          AND bot_id = $2
          AND topic_id = $3
        """,
        message_ids,
        bot_id,
        topic_id,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.debug(
            "expire_messages: updated=%d bot_id=%s topic_id=%s",
            updated,
            bot_id,
            str(topic_id),
        )
    return updated


# ── recovery ─────────────────────────────────────────────────────────────────

async def recover_stale_processing(
    pool: Any,
    *,
    bot_id: str,
    topic_id: UUID,
    stale_seconds: int = 300,
    limit: int = 50,
) -> int:
    """Recover inbound messages stuck in ``processing`` state.

    Rows with ``processing_started_at`` older than *stale_seconds* are reset
    to ``raw`` so the sweeper or coalescer can re-process them.  This handles
    worker crashes that leave rows in ``processing`` indefinitely.
    """
    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state      = 'raw',
            processing_started_at = NULL
        WHERE id IN (
            SELECT id FROM messages
            WHERE direction = 'inbound'
              AND processing_state = 'processing'
              AND processing_started_at < now() - $3::interval
              AND bot_id = $1
              AND topic_id = $2
            LIMIT $4
        )
        """,
        bot_id,
        topic_id,
        timedelta(seconds=stale_seconds),
        limit,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.info(
            "recover_stale_processing: recovered=%d bot_id=%s topic_id=%s "
            "stale_seconds=%d",
            updated,
            bot_id,
            str(topic_id),
            stale_seconds,
        )
    return updated


async def recover_retryable_failed(
    pool: Any,
    *,
    bot_id: str,
    topic_id: UUID,
    max_retries: int = 3,
    limit: int = 50,
) -> int:
    """Recover inbound messages in ``failed`` state that are still retryable.

    Only rows with ``processing_attempts < max_retries`` are reset to ``raw``.
    Rows that have exceeded the retry cap are left in ``failed`` for manual
    inspection (they are terminal for automatic recovery).
    """
    result = await pool.execute(
        """
        UPDATE messages
        SET processing_state      = 'raw',
            processing_started_at = NULL
        WHERE id IN (
            SELECT id FROM messages
            WHERE direction = 'inbound'
              AND processing_state = 'failed'
              AND processing_attempts < $3
              AND bot_id = $1
              AND topic_id = $2
            LIMIT $4
        )
        """,
        bot_id,
        topic_id,
        max_retries,
        limit,
    )
    updated = _parse_update_count(result)
    if updated:
        logger.info(
            "recover_retryable_failed: recovered=%d bot_id=%s topic_id=%s "
            "max_retries=%d",
            updated,
            bot_id,
            str(topic_id),
            max_retries,
        )
    return updated


# ── internal helpers ─────────────────────────────────────────────────────────

def _parse_update_count(result: Any) -> int:
    """Parse the row count from an asyncpg ``execute`` result string.

    asyncpg returns a string like ``"UPDATE 5"`` for successful UPDATEs.
    Returns 0 for unrecognised formats or None.
    """
    if isinstance(result, str):
        parts = result.strip().split()
        if len(parts) >= 2 and parts[0].upper() == "UPDATE":
            try:
                return int(parts[1])
            except (ValueError, IndexError):
                pass
    if isinstance(result, int):
        return result
    return 0
