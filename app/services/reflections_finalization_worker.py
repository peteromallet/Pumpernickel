"""Background worker for reflection session finalization.

Follows the same bounded worker pattern as ``EmbedJobWorker`` (see
``app/services/embed_worker.py``): a class with ``run_forever()`` that
loops on ``run_once()`` + ``asyncio.sleep()``, registered in the FastAPI
lifespan as ``asyncio.create_task(worker.run_forever())``.

This worker does NOT create or mutate scheduled jobs, and it never sends
proactive reflection invitations or messages.  It only evaluates existing
collecting sessions and transitions them to ``finalizing`` or ``abandoned``
when their idle deadline has passed — exactly the bounded inactivity
finalization contract described in T8.

Design contract
---------------
* Follows the same ``run_forever()`` / ``run_once()`` pattern as
  ``EmbedJobWorker`` — one poll loop, one atomic batch per tick.
* Uses ``FinalizationEngine`` (pure business logic from
  ``app/reflections/finalization.py``) for all decisions.
* Executes decisions through ``ReflectionStore.finalize_session()`` and
  ``ReflectionStore.abandon_session()``, which are both race-safe via
  ``WHERE status = 'collecting'``.
* Idempotent: re-running finalization on an already-finalized session
  is a no-op (the store-level filter rejects it).
* Does NOT use ``mediator.scheduled_jobs`` — this is a polling worker
  that queries ``mediator.reflection_sessions`` directly.
* Gated behind ``reflection_finalization_worker_enabled`` setting
  (default ``False`` to avoid surprising existing deploys).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.config import Settings, get_settings
from app.services.embeddings import (
    DEFAULT_OPENAI_EMBEDDING_DIMENSION,
    DEFAULT_OPENAI_EMBEDDING_MODEL,
    content_hash,
)
from app.services.message_embedding_lifecycle import enqueue_reflection_embed
from app.reflections.finalization import (
    FinalizationDecision,
    FinalizationEngine,
    SessionState,
    build_session_state,
)
from app.services.reflection_redaction import redact_for_log_extra
from app.services.reflections import ReflectionStore
from app.services.reflections_normalization_bridge import (
    normalize_and_create_entry,
)

logger = logging.getLogger(__name__)

# ── Default configuration (overridable via settings) ────────────────────────


@dataclass(frozen=True)
class FinalizationWorkerResult:
    """Outcome of one ``run_once()`` tick."""

    scanned: int = 0
    finalized: int = 0
    abandoned: int = 0
    skipped_active: int = 0
    skipped_idempotent: int = 0
    errors: int = 0


# ── Worker ──────────────────────────────────────────────────────────────────


class ReflectionFinalizationWorker:
    """Polling worker that finalizes reflection sessions past their idle deadline.

    This is a *bounded* worker: it only acts on sessions that are already
    ``collecting`` and whose idle deadline has passed.  It never creates
    sessions, never sends messages, and never schedules proactive work.

    Usage::

        worker = ReflectionFinalizationWorker(pool, settings=settings)
        task = asyncio.create_task(worker.run_forever())
        app.state.background_tasks.add(task)
    """

    def __init__(
        self,
        pool: Any,
        *,
        settings: Settings | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._pool = pool
        self._settings = settings or get_settings()
        self._worker_id = worker_id or f"refl-finalize-{uuid4()}"
        self._engine = FinalizationEngine()
        self._embedding_repair_cursor: Any | None = None

    # ── Public API ──────────────────────────────────────────────────────

    async def run_forever(self) -> None:
        """Run the finalization loop until cancelled.

        Same pattern as ``EmbedJobWorker.run_forever()`` and
        ``ScheduledJobWorker.run_forever()``.
        """
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "reflection finalization worker tick failed (worker=%s)",
                    self._worker_id,
                    extra=redact_for_log_extra(
                        {"worker_id": self._worker_id}
                    ),
                )
            await asyncio.sleep(self._poll_interval)

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> FinalizationWorkerResult:
        """Execute one finalization scan.

        Phase 1: Queries all collecting sessions whose idle deadline has
        passed, evaluates each one through ``FinalizationEngine``, and
        executes the resulting decisions.

        Phase 2: Recovers ``finalizing`` sessions that lack entries (from
        retry, worker interruption, or partial failure).  These are
        re-normalized through ``normalize_and_create_entry`` whose
        idempotency check prevents duplicate entries.

        Args:
            now: Reference datetime (injectable for tests).
        """
        now = _ensure_utc(now)

        # ── Phase 1: find sessions past their idle deadline ────────────
        sessions = await self._find_due_sessions(now=now)

        result = FinalizationWorkerResult(scanned=len(sessions))

        for session in sessions:
            try:
                decision = await self._evaluate_and_act(session, now=now)
                if decision.action == "finalize":
                    result = FinalizationWorkerResult(
                        scanned=result.scanned,
                        finalized=result.finalized + 1,
                        abandoned=result.abandoned,
                        skipped_active=result.skipped_active,
                        skipped_idempotent=result.skipped_idempotent,
                        errors=result.errors,
                    )
                elif decision.action == "abandon":
                    result = FinalizationWorkerResult(
                        scanned=result.scanned,
                        finalized=result.finalized,
                        abandoned=result.abandoned + 1,
                        skipped_active=result.skipped_active,
                        skipped_idempotent=result.skipped_idempotent,
                        errors=result.errors,
                    )
                elif decision.action == "noop":
                    reason = decision.reason.lower()
                    if "not collecting" in reason or "terminal" in reason:
                        result = FinalizationWorkerResult(
                            scanned=result.scanned,
                            finalized=result.finalized,
                            abandoned=result.abandoned,
                            skipped_active=result.skipped_active,
                            skipped_idempotent=result.skipped_idempotent + 1,
                            errors=result.errors,
                        )
                    else:
                        result = FinalizationWorkerResult(
                            scanned=result.scanned,
                            finalized=result.finalized,
                            abandoned=result.abandoned,
                            skipped_active=result.skipped_active + 1,
                            skipped_idempotent=result.skipped_idempotent,
                            errors=result.errors,
                        )
            except Exception:
                logger.exception(
                    "reflection finalization worker: error processing session %s",
                    getattr(session, "id", "unknown"),
                    extra=redact_for_log_extra(
                        {
                            "worker_id": self._worker_id,
                            "session_id": str(getattr(session, "id", "unknown")),
                        }
                    ),
                )
                result = FinalizationWorkerResult(
                    scanned=result.scanned,
                    finalized=result.finalized,
                    abandoned=result.abandoned,
                    skipped_active=result.skipped_active,
                    skipped_idempotent=result.skipped_idempotent,
                    errors=result.errors + 1,
                )

        # ── Phase 2: recover finalizing sessions without entries ─────────
        # Sessions that were retried (processing_failed → finalizing) or
        # interrupted after finalize_session but before entry creation
        # are stuck in finalizing with no entry.  We recover them here.
        retried = await self._collect_retried_sessions()

        for session_row in retried:
            try:
                await self._retry_finalizing_session(session_row, now=now)
            except Exception:
                logger.exception(
                    "reflection finalization worker: error recovering "
                    "retried session %s",
                    getattr(session_row, "id", getattr(session_row, "get", lambda _: None)("id")),
                    extra=redact_for_log_extra(
                        {
                            "worker_id": self._worker_id,
                            "session_id": str(
                                getattr(session_row, "id", getattr(session_row, "get", lambda _: None)("id"))
                            ),
                        }
                    ),
                )

        # ── Phase 3: repair swallowed reflection embedding enqueues ─────
        # Completion enqueue is deliberately best-effort.  This bounded,
        # cursor-driven sweep makes that failure mode eventually consistent.
        embedding_repairs = 0
        try:
            embedding_repairs = await self._repair_processed_embeddings()
        except Exception:
            logger.exception(
                "reflection finalization worker: embedding repair sweep failed",
                extra=redact_for_log_extra({"worker_id": self._worker_id}),
            )

        if result.finalized or result.abandoned or retried or embedding_repairs:
            logger.info(
                "reflection finalization worker tick: scanned=%d finalized=%d "
                "abandoned=%d skipped_active=%d skipped_idempotent=%d errors=%d "
                "retried_recovered=%d embedding_repairs=%d (worker=%s)",
                result.scanned,
                result.finalized,
                result.abandoned,
                result.skipped_active,
                result.skipped_idempotent,
                result.errors,
                len(retried),
                embedding_repairs,
                self._worker_id,
            )

        return result

    # ── Internal ────────────────────────────────────────────────────────

    @property
    def _poll_interval(self) -> float:
        """Poll interval in seconds.

        Defaults to 60 s; override via settings if needed.
        """
        return getattr(
            self._settings,
            "reflection_finalization_worker_poll_interval_s",
            60.0,
        )

    async def _find_due_sessions(self, *, now: datetime) -> list[Any]:
        """Find collecting sessions whose idle deadline has passed.

        A session is due when EITHER:
        - ``idle_finalize_at`` is set AND ``idle_finalize_at <= now``, OR
        - ``idle_finalize_at`` is NULL AND ``opened_at + default_timeout <= now``.

        Returns ORM rows (asyncpg Records) that have at least:
        id, user_id, bot_id, status, source_message_ids, opened_at,
        idle_finalize_at, finalized_at, abandoned_at, topic_id, phase.
        """
        default_timeout_seconds = self._engine.DEFAULT_IDLE_TIMEOUT_SECONDS
        abandon_timeout_seconds = self._engine.DEFAULT_ABANDON_TIMEOUT_SECONDS

        rows = await self._pool.fetch(
            """
            SELECT id, user_id, bot_id, status,
                   source_message_ids, opened_at, idle_finalize_at,
                   finalized_at, abandoned_at, topic_id, phase
            FROM mediator.reflection_sessions
            WHERE status = 'collecting'
              AND (
                  -- Explicit idle deadline has passed
                  idle_finalize_at <= $1
                  OR
                  -- No explicit deadline; use opened_at + default timeout.
                  -- Also include "very stale" sessions past abandon threshold
                  -- so the abandon path can be exercised.
                  (
                      idle_finalize_at IS NULL
                      AND opened_at IS NOT NULL
                      AND opened_at <= ($1::timestamptz - make_interval(secs => $2))
                  )
              )
            ORDER BY opened_at ASC NULLS LAST
            LIMIT $3
            """,
            now,
            default_timeout_seconds,
            self._batch_size,
        )
        return list(rows)

    @property
    def _batch_size(self) -> int:
        """Max sessions to process per tick."""
        return getattr(
            self._settings,
            "reflection_finalization_worker_batch_size",
            50,
        )

    async def _evaluate_and_act(
        self,
        row: Any,
        *,
        now: datetime,
    ) -> FinalizationDecision:
        """Evaluate one session row and execute the finalization decision.

        Returns the decision that was made (for result accounting).

        Store-level exceptions (e.g. race conditions, DB errors) are
        NOT caught here — they propagate to ``run_once()`` where they
        are counted as errors.  The store's ``WHERE status = 'collecting'``
        filter is the race-safety mechanism: if another worker already
        transitioned the session, the UPDATE affects 0 rows and the store
        raises ``SessionFinalizeConflictError`` / ``SessionNotCollectingError``.
        The worker treats these as errors (counted in ``result.errors``)
        rather than silently swallowing them, because the next poll cycle
        will simply not find the session (it's no longer collecting).
        """
        # ── Build SessionState from the row ────────────────────────────
        session_state = _session_state_from_row(row)

        store = ReflectionStore(self._pool)

        # ── Evaluate ───────────────────────────────────────────────────
        decision = self._engine.evaluate_full(
            session=session_state,
            now=now,
        )

        # ── Execute ────────────────────────────────────────────────────
        if decision.action == "finalize":
            await store.finalize_session(
                user_id=session_state.user_id,
                session_id=session_state.session_id,
            )
            logger.debug(
                "finalization worker: finalized session=%s user=%s",
                session_state.session_id,
                session_state.user_id,
            )
            # ── Normalize and create the immutable entry ──────────────
            # This is placed synchronously after finalize because
            # normalization is cheap (pure computation + message-text
            # fetch).  Retry safety is provided by the idempotency
            # check inside normalize_and_create_entry.
            try:
                entry = await normalize_and_create_entry(
                    store=store,
                    user_id=session_state.user_id,
                    session_id=session_state.session_id,
                    bot_id=session_state.bot_id,
                    pool=self._pool,
                    processor_version=self._worker_id,
                )
                # ── Mark processed to prevent re-processing ──────────
                # Transition session to processed so that subsequent polls
                # and retry paths treat it as terminal.  Uses complete_session
                # (direct status update) rather than mark_session_processed
                # because the worker does not use the claim mechanism.
                if entry is not None:
                    await store.complete_session(
                        session_id=session_state.session_id,
                        user_id=session_state.user_id,
                    )
            except Exception:
                logger.exception(
                    "finalization worker: normalize+create_entry failed for "
                    "session=%s (session is still finalized — entry creation "
                    "will be retried on next poll via idempotency)",
                    session_state.session_id,
                    extra=redact_for_log_extra(
                        {
                            "session_id": str(session_state.session_id),
                            "user_id": str(session_state.user_id),
                            "bot_id": session_state.bot_id,
                            "worker_id": self._worker_id,
                        }
                    ),
                )
            return decision

        elif decision.action == "abandon":
            await store.abandon_session(
                user_id=session_state.user_id,
                session_id=session_state.session_id,
            )
            logger.debug(
                "finalization worker: abandoned session=%s user=%s reason=%s",
                session_state.session_id,
                session_state.user_id,
                decision.reason,
            )
            return decision

        else:
            # noop / skip_late / open_new_for_late — nothing to do here.
            # The worker only handles inactivity finalization; explicit
            # completion and topic transitions are handled synchronously
            # in the inbound path (via capture_burst_for_reflection).
            return decision

    # ── Retry recovery ──────────────────────────────────────────────────

    async def _collect_retried_sessions(self) -> list[Any]:
        """Find ``finalizing`` sessions that lack entries and need recovery.

        These sessions arrived in ``finalizing`` via ``retry_session``
        (processing_failed → finalizing) or were interrupted after
        ``finalize_session`` but before ``normalize_and_create_entry``.

        Returns rows with at least: id, user_id, bot_id, status,
        source_message_ids, opened_at, idle_finalize_at, finalized_at,
        abandoned_at, topic_id, phase.
        """
        rows = await self._pool.fetch(
            """
            SELECT s.id, s.user_id, s.bot_id, s.status,
                   s.source_message_ids, s.opened_at, s.idle_finalize_at,
                   s.finalized_at, s.abandoned_at, s.topic_id, s.phase
            FROM mediator.reflection_sessions s
            WHERE s.status = 'finalizing'
              AND NOT EXISTS (
                  SELECT 1
                  FROM mediator.reflection_entries e
                  WHERE e.session_id = s.id
              )
            ORDER BY s.finalized_at ASC NULLS LAST
            LIMIT $1
            """,
            self._batch_size,
        )
        return list(rows)

    async def _retry_finalizing_session(
        self,
        row: Any,
        *,
        now: datetime,
    ) -> None:
        """Recover a single ``finalizing`` session that lacks an entry.

        Calls ``normalize_and_create_entry`` which is idempotent — if an
        entry already exists (race), it returns the existing one.  On
        success transitions the session to ``processed`` via
        ``complete_session``.
        """
        get = row.get if isinstance(row, dict) else lambda k: row[k]
        session_id = get("id")
        user_id = get("user_id")
        bot_id = get("bot_id") or "unknown"

        store = ReflectionStore(self._pool)

        entry = await normalize_and_create_entry(
            store=store,
            user_id=user_id,
            session_id=session_id,
            bot_id=bot_id,
            pool=self._pool,
            processor_version=self._worker_id,
        )

        if entry is not None:
            await store.complete_session(
                session_id=session_id,
                user_id=user_id,
            )
            logger.info(
                "finalization worker: recovered retried session=%s entry=%s",
                session_id,
                entry.id,
            )

    # ── Embedding repair ───────────────────────────────────────────────

    async def _repair_processed_embeddings(self) -> int:
        """Re-enqueue bounded missing/stale processed reflection embeddings.

        A matching persisted embedding or active embed/reembed job is current
        only when hash, configured model, and configured dimension all match.
        The in-process cursor prevents healthy early rows from starving later
        gaps; reaching the end wraps to the beginning on the next query.
        """
        rows = await self._fetch_embedding_repair_page(
            after_entry_id=self._embedding_repair_cursor,
        )
        if not rows and self._embedding_repair_cursor is not None:
            self._embedding_repair_cursor = None
            rows = await self._fetch_embedding_repair_page(after_entry_id=None)
        if not rows:
            return 0

        model = getattr(
            self._settings,
            "embedding_model",
            DEFAULT_OPENAI_EMBEDDING_MODEL,
        )
        dimension = getattr(
            self._settings,
            "embedding_dimension",
            DEFAULT_OPENAI_EMBEDDING_DIMENSION,
        )
        attempted = 0
        for row in rows:
            get = row.get if isinstance(row, dict) else lambda key: row[key]
            entry_id = get("id")
            plaintext = get("plaintext_searchable")
            expected_hash = content_hash(plaintext)
            embedding_current = (
                get("embedded_content_hash") == expected_hash
                and get("embedded_model") == model
                and get("embedded_dimension") == dimension
            )
            job_current = (
                get("job_content_hash") == expected_hash
                and get("job_model") == model
                and get("job_dimension") == dimension
            )
            if not embedding_current and not job_current:
                attempted += 1
                try:
                    if get("job_id") is not None and get("job_status") == "pending":
                        await self._supersede_stale_pending_job(get("job_id"))
                    await enqueue_reflection_embed(
                        self._pool,
                        entry_id=entry_id,
                        plaintext_searchable=plaintext,
                    )
                except Exception:
                    logger.warning(
                        "reflection embedding repair enqueue failed for entry=%s",
                        entry_id,
                        exc_info=True,
                        extra=redact_for_log_extra(
                            {"entry_id": str(entry_id)}
                        ),
                    )

        last = rows[-1]
        last_get = last.get if isinstance(last, dict) else lambda key: last[key]
        self._embedding_repair_cursor = last_get("id")
        return attempted

    async def _supersede_stale_pending_job(self, job_id: Any) -> None:
        """Clear a stale pending job so model/dimension drift can re-enqueue."""
        await self._pool.execute(
            """
            UPDATE mediator.embed_jobs
            SET status = 'superseded',
                last_error = 'superseded by reflection embedding repair',
                locked_at = NULL,
                locked_by = NULL,
                completed_at = NOW(),
                updated_at = NOW()
            WHERE id = $1
              AND source_type = 'reflection'
              AND status = 'pending'
            """,
            job_id,
        )

    async def _fetch_embedding_repair_page(
        self,
        *,
        after_entry_id: Any | None,
    ) -> list[Any]:
        rows = await self._pool.fetch(
            """
            SELECT re.id,
                   re.plaintext_searchable,
                   ce.content_hash AS embedded_content_hash,
                   ce.model AS embedded_model,
                   ce.dimension AS embedded_dimension,
                   ej.content_hash AS job_content_hash,
                   ej.model AS job_model,
                   ej.dimension AS job_dimension,
                   ej.id AS job_id,
                   ej.status AS job_status
            FROM mediator.reflection_entries re
            JOIN mediator.reflection_sessions rs ON rs.id = re.session_id
            LEFT JOIN mediator.content_embeddings ce
              ON ce.source_type = 'reflection'
             AND ce.source_id = re.id
            LEFT JOIN LATERAL (
                SELECT id, status, content_hash, model, dimension
                FROM mediator.embed_jobs
                WHERE source_type = 'reflection'
                  AND source_id = re.id
                  AND job_kind IN ('embed', 'reembed')
                  AND status IN ('pending', 'processing')
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
            ) ej ON TRUE
            WHERE rs.status = 'processed'
              AND re.plaintext_searchable IS NOT NULL
              AND btrim(re.plaintext_searchable) <> ''
              AND NOT EXISTS (
                  SELECT 1
                  FROM mediator.reflection_entries successor
                  WHERE successor.supersedes_entry_id = re.id
              )
              AND ($1::uuid IS NULL OR re.id > $1)
            ORDER BY re.id
            LIMIT $2
            """,
            after_entry_id,
            self._batch_size,
        )
        return list(rows)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _ensure_utc(dt: datetime | None) -> datetime:
    """Return a timezone-aware UTC datetime from *dt* or now."""
    if dt is None:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _session_state_from_row(row: Any) -> SessionState:
    """Build a ``SessionState`` from an asyncpg Record or dict row.

    The row must have the columns selected by ``_find_due_sessions``:
    id, user_id, bot_id, status, source_message_ids, opened_at,
    idle_finalize_at, finalized_at, abandoned_at, topic_id, phase.
    """
    get = row.get if isinstance(row, dict) else lambda k: row[k]

    source_ids = get("source_message_ids") or []
    if not isinstance(source_ids, list):
        source_ids = list(source_ids)

    return build_session_state(
        session_id=get("id"),
        user_id=get("user_id"),
        bot_id=get("bot_id") or "unknown",
        status=get("status") or "collecting",
        source_message_ids=source_ids,
        opened_at=get("opened_at"),
        idle_finalize_at=get("idle_finalize_at"),
        finalized_at=get("finalized_at"),
        abandoned_at=get("abandoned_at"),
        topic_id=get("topic_id"),
        phase=get("phase") or "freeform",
    )
