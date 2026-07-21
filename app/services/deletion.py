"""Deletion grace-period purge job."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID

from app.services.crypto import encrypt_value


def _normalize_message_ids(message_ids: Iterable[UUID]) -> list[UUID]:
    seen: set[UUID] = set()
    normalized: list[UUID] = []
    for message_id in message_ids:
        if message_id in seen:
            continue
        seen.add(message_id)
        normalized.append(message_id)
    return normalized


async def cleanup_deleted_reflection_state(
    pool: Any,
    *,
    message_ids: Iterable[UUID],
) -> None:
    """Tombstone reflection state tied to deleted source messages.

    The cleanup is intentionally idempotent. It hides affected reflection
    entries immediately, retires any derived durable targets, and clears
    orphaned embedding/search state while preserving audit-safe reflection
    session / entry tombstones.
    """

    affected_message_ids = _normalize_message_ids(message_ids)
    if not affected_message_ids:
        return

    now = datetime.now(timezone.utc)

    await pool.execute(
        """
        UPDATE mediator.reflection_sessions rs
        SET status = CASE
                WHEN rs.status = 'processed' THEN rs.status
                ELSE 'abandoned'
            END,
            abandoned_at = CASE
                WHEN rs.status = 'processed' THEN rs.abandoned_at
                ELSE COALESCE(rs.abandoned_at, $2)
            END,
            claimed_by = NULL,
            claimed_at = NULL,
            idle_finalize_at = NULL,
            failure_class = CASE
                WHEN rs.status = 'processed' THEN rs.failure_class
                ELSE NULL
            END,
            failure_reason = CASE
                WHEN rs.status = 'processed' THEN rs.failure_reason
                ELSE COALESCE(rs.failure_reason, 'source_message_deleted')
            END,
            last_error = CASE
                WHEN rs.status = 'processed' THEN rs.last_error
                ELSE COALESCE(rs.last_error, 'reflection source message deleted')
            END,
            updated_at = $2
        WHERE rs.opened_by_message_id = ANY($1::uuid[])
           OR rs.source_message_ids && $1::uuid[]
        """,
        affected_message_ids,
        now,
    )

    await pool.execute(
        """
        UPDATE mediator.reflection_entries re
        SET payload_encrypted = NULL,
            plaintext_searchable = NULL,
            summary_encrypted = NULL
        WHERE re.session_id IN (
            SELECT rs.id
            FROM mediator.reflection_sessions rs
            WHERE rs.opened_by_message_id = ANY($1::uuid[])
               OR rs.source_message_ids && $1::uuid[]
        )
        """,
        affected_message_ids,
    )

    await pool.execute(
        """
        UPDATE mediator.reflection_derivations rd
        SET candidate_payload_encrypted = NULL,
            eligibility_reasons = '["source_message_deleted"]'::jsonb,
            supporting_message_ids = ARRAY[]::uuid[],
            decision = 'superseded',
            decided_at = COALESCE(rd.decided_at, $2)
        WHERE rd.reflection_entry_id IN (
            SELECT re.id
            FROM mediator.reflection_entries re
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rs.opened_by_message_id = ANY($1::uuid[])
               OR rs.source_message_ids && $1::uuid[]
        )
        """,
        affected_message_ids,
        now,
    )

    await pool.execute(
        """
        UPDATE memories m
        SET status = 'invalidated'
        WHERE m.id IN (
            SELECT rd.applied_target_id
            FROM mediator.reflection_derivations rd
            JOIN mediator.reflection_entries re
              ON re.id = rd.reflection_entry_id
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rd.applied_target_table = 'memories'
              AND rd.applied_target_id IS NOT NULL
              AND (
                  rs.opened_by_message_id = ANY($1::uuid[])
                  OR rs.source_message_ids && $1::uuid[]
              )
        )
          AND m.status = 'active'
        """,
        affected_message_ids,
    )

    await pool.execute(
        """
        UPDATE observations o
        SET status = 'stale'
        WHERE o.id IN (
            SELECT rd.applied_target_id
            FROM mediator.reflection_derivations rd
            JOIN mediator.reflection_entries re
              ON re.id = rd.reflection_entry_id
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rd.applied_target_table = 'observations'
              AND rd.applied_target_id IS NOT NULL
              AND (
                  rs.opened_by_message_id = ANY($1::uuid[])
                  OR rs.source_message_ids && $1::uuid[]
              )
        )
          AND o.status = 'active'
        """,
        affected_message_ids,
    )

    await pool.execute(
        """
        UPDATE distillations d
        SET status = 'invalidated',
            updated_at = $2
        WHERE d.id IN (
            SELECT rd.applied_target_id
            FROM mediator.reflection_derivations rd
            JOIN mediator.reflection_entries re
              ON re.id = rd.reflection_entry_id
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rd.applied_target_table = 'distillations'
              AND rd.applied_target_id IS NOT NULL
              AND (
                  rs.opened_by_message_id = ANY($1::uuid[])
                  OR rs.source_message_ids && $1::uuid[]
              )
        )
          AND d.status = 'active'
        """,
        affected_message_ids,
        now,
    )

    await pool.execute(
        """
        UPDATE mediator.user_orientation_items uoi
        SET status = 'retired',
            closed_reason = COALESCE(uoi.closed_reason, 'source_message_deleted'),
            updated_at = $2
        WHERE uoi.id IN (
            SELECT rd.applied_target_id
            FROM mediator.reflection_derivations rd
            JOIN mediator.reflection_entries re
              ON re.id = rd.reflection_entry_id
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rd.applied_target_table = 'user_orientation_items'
              AND rd.applied_target_id IS NOT NULL
              AND (
                  rs.opened_by_message_id = ANY($1::uuid[])
                  OR rs.source_message_ids && $1::uuid[]
              )
        )
          AND uoi.status IN ('pending', 'active', 'completed')
        """,
        affected_message_ids,
        now,
    )

    await pool.execute(
        """
        DELETE FROM mediator.content_embeddings ce
        USING (
            SELECT 'reflection'::text AS source_type, re.id AS source_id
            FROM mediator.reflection_entries re
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rs.opened_by_message_id = ANY($1::uuid[])
               OR rs.source_message_ids && $1::uuid[]

            UNION

            SELECT CASE rd.applied_target_table
                    WHEN 'memories' THEN 'memory'
                    WHEN 'observations' THEN 'observation'
                    WHEN 'distillations' THEN 'distillation'
                END AS source_type,
                rd.applied_target_id AS source_id
            FROM mediator.reflection_derivations rd
            JOIN mediator.reflection_entries re
              ON re.id = rd.reflection_entry_id
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rd.applied_target_table IN ('memories', 'observations', 'distillations')
              AND rd.applied_target_id IS NOT NULL
              AND (
                  rs.opened_by_message_id = ANY($1::uuid[])
                  OR rs.source_message_ids && $1::uuid[]
              )
        ) targets
        WHERE ce.source_type = targets.source_type
          AND ce.source_id = targets.source_id
        """,
        affected_message_ids,
    )

    await pool.execute(
        """
        UPDATE mediator.embed_jobs ej
        SET status = 'cancelled',
            last_error = 'source_message_deleted',
            updated_at = $2
        FROM (
            SELECT 'reflection'::text AS source_type, re.id AS source_id
            FROM mediator.reflection_entries re
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rs.opened_by_message_id = ANY($1::uuid[])
               OR rs.source_message_ids && $1::uuid[]

            UNION

            SELECT CASE rd.applied_target_table
                    WHEN 'memories' THEN 'memory'
                    WHEN 'observations' THEN 'observation'
                    WHEN 'distillations' THEN 'distillation'
                END AS source_type,
                rd.applied_target_id AS source_id
            FROM mediator.reflection_derivations rd
            JOIN mediator.reflection_entries re
              ON re.id = rd.reflection_entry_id
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE rd.applied_target_table IN ('memories', 'observations', 'distillations')
              AND rd.applied_target_id IS NOT NULL
              AND (
                  rs.opened_by_message_id = ANY($1::uuid[])
                  OR rs.source_message_ids && $1::uuid[]
              )
        ) targets
        WHERE ej.source_type = targets.source_type
          AND ej.source_id = targets.source_id
          AND ej.status IN ('pending', 'processing')
        """,
        affected_message_ids,
        now,
    )


async def purge_expired_deletions(pool: Any) -> str:
    rows = await pool.fetch(
        """
        SELECT id
        FROM messages
        WHERE deleted_at IS NOT NULL
          AND deleted_at < now() - interval '24 hours'
        """
    )
    if rows:
        await cleanup_deleted_reflection_state(
            pool,
            message_ids=[row["id"] for row in rows],
        )
    return await pool.execute(
        """
        UPDATE messages
        SET content='[deleted]',
            content_encrypted=$1
        WHERE deleted_at IS NOT NULL
          AND deleted_at < now() - interval '24 hours'
          AND content <> '[deleted]'
        """,
        encrypt_value("[deleted]"),
    )
