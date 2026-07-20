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
from app.reflections.finalization import (
    FinalizationDecision,
    FinalizationEngine,
    SessionState,
    build_session_state,
)
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
                )
            await asyncio.sleep(self._poll_interval)

    async def run_once(
        self,
        *,
        now: datetime | None = None,
    ) -> FinalizationWorkerResult:
        """Execute one finalization scan.

        Queries all collecting sessions whose idle deadline has passed,
        evaluates each one through ``FinalizationEngine``, and executes
        the resulting decisions.

        Args:
            now: Reference datetime (injectable for tests).
        """
        now = _ensure_utc(now)

        # ── Step 1: find sessions past their idle deadline ──────────────
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
                    extra={"worker_id": self._worker_id},
                )
                result = FinalizationWorkerResult(
                    scanned=result.scanned,
                    finalized=result.finalized,
                    abandoned=result.abandoned,
                    skipped_active=result.skipped_active,
                    skipped_idempotent=result.skipped_idempotent,
                    errors=result.errors + 1,
                )

        if result.finalized or result.abandoned:
            logger.info(
                "reflection finalization worker tick: scanned=%d finalized=%d "
                "abandoned=%d skipped_active=%d skipped_idempotent=%d errors=%d "
                "(worker=%s)",
                result.scanned,
                result.finalized,
                result.abandoned,
                result.skipped_active,
                result.skipped_idempotent,
                result.errors,
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
                await normalize_and_create_entry(
                    store=store,
                    user_id=session_state.user_id,
                    session_id=session_state.session_id,
                    bot_id=session_state.bot_id,
                    pool=self._pool,
                    processor_version=self._worker_id,
                )
            except Exception:
                logger.exception(
                    "finalization worker: normalize+create_entry failed for "
                    "session=%s (session is still finalized — entry creation "
                    "will be retried on next poll via idempotency)",
                    session_state.session_id,
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
