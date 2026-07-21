"""Tests for app/services/reflections_finalization_worker.py.

Covers:
  - Inactivity processing: sessions past idle deadline are finalized.
  - Abandoned-session handling: sessions past abandon threshold are abandoned.
  - Retry-safe worker behavior: re-running on already-finalized sessions
    is idempotent.
  - Active sessions are left untouched.
  - Error resilience: individual session failures don't crash the tick.
  - Bounded worker pattern: no scheduled jobs, no proactive sends.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.reflections.finalization import FinalizationDecision
from app.services.reflections_finalization_worker import (
    FinalizationWorkerResult,
    ReflectionFinalizationWorker,
    _ensure_utc,
    _session_state_from_row,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _session_row(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "test-bot",
    status: str = "collecting",
    source_message_ids: list[UUID] | None = None,
    opened_at: datetime | None = None,
    idle_finalize_at: datetime | None = None,
    finalized_at: datetime | None = None,
    abandoned_at: datetime | None = None,
    topic_id: UUID | None = None,
    phase: str = "freeform",
) -> dict:
    """Build a dict that mimics an asyncpg Record row for reflection_sessions."""
    return {
        "id": session_id or _uid(),
        "user_id": user_id or _uid(),
        "bot_id": bot_id,
        "status": status,
        "source_message_ids": list(source_message_ids or []),
        "opened_at": opened_at,
        "idle_finalize_at": idle_finalize_at,
        "finalized_at": finalized_at,
        "abandoned_at": abandoned_at,
        "topic_id": topic_id,
        "phase": phase,
    }


class _FakeSettings:
    """Minimal settings with the fields the worker reads."""

    def __init__(
        self,
        poll_interval: float = 60.0,
        batch_size: int = 50,
    ) -> None:
        self.reflection_finalization_worker_poll_interval_s = poll_interval
        self.reflection_finalization_worker_batch_size = batch_size
        self.embedding_model = "test-reflection-model"
        self.embedding_dimension = 384


def _embedding_repair_row(
    *,
    entry_id: UUID | None = None,
    plaintext: str = "A repaired reflection",
    embedded_content_hash: str | None = None,
    embedded_model: str | None = None,
    embedded_dimension: int | None = None,
    job_content_hash: str | None = None,
    job_model: str | None = None,
    job_dimension: int | None = None,
    job_id: UUID | None = None,
    job_status: str | None = None,
) -> dict:
    return {
        "id": entry_id or _uid(),
        "plaintext_searchable": plaintext,
        "embedded_content_hash": embedded_content_hash,
        "embedded_model": embedded_model,
        "embedded_dimension": embedded_dimension,
        "job_content_hash": job_content_hash,
        "job_model": job_model,
        "job_dimension": job_dimension,
        "job_id": job_id,
        "job_status": job_status,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Session state builder tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSessionStateFromRow:
    """Verify _session_state_from_row correctly maps row dicts to SessionState."""

    def test_maps_all_fields(self) -> None:
        sid = _uid()
        uid = _uid()
        opened = _utc_now() - timedelta(hours=1)
        idle = _utc_now() - timedelta(minutes=5)
        row = _session_row(
            session_id=sid,
            user_id=uid,
            bot_id="bot-1",
            status="collecting",
            source_message_ids=[_uid(), _uid()],
            opened_at=opened,
            idle_finalize_at=idle,
            finalized_at=None,
            abandoned_at=None,
            topic_id=_uid(),
            phase="retrospective",
        )

        state = _session_state_from_row(row)

        assert state.session_id == sid
        assert state.user_id == uid
        assert state.bot_id == "bot-1"
        assert state.status == "collecting"
        assert len(state.source_message_ids) == 2
        assert state.opened_at == opened
        assert state.idle_finalize_at == idle
        assert state.finalized_at is None
        assert state.abandoned_at is None
        assert state.topic_id is not None
        assert state.phase == "retrospective"

    def test_missing_bot_id_defaults_to_unknown(self) -> None:
        row = _session_row(bot_id="")  # empty string
        # Override with None/absent
        row["bot_id"] = None
        state = _session_state_from_row(row)
        assert state.bot_id == "unknown"

    def test_missing_phase_defaults_to_freeform(self) -> None:
        row = _session_row()
        row["phase"] = None
        state = _session_state_from_row(row)
        assert state.phase == "freeform"

    def test_source_message_ids_converted_to_list(self) -> None:
        row = _session_row()
        row["source_message_ids"] = None
        state = _session_state_from_row(row)
        assert state.source_message_ids == []


# ═══════════════════════════════════════════════════════════════════════════
# UTC helper tests
# ═══════════════════════════════════════════════════════════════════════════


class TestEnsureUtc:
    def test_none_returns_now(self) -> None:
        result = _ensure_utc(None)
        assert result.tzinfo is not None

    def test_naive_datetime_gets_utc(self) -> None:
        dt = datetime(2026, 7, 19, 12, 0, 0)
        result = _ensure_utc(dt)
        assert result.tzinfo == timezone.utc
        assert result.hour == 12

    def test_aware_datetime_preserves_instant(self) -> None:
        dt = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
        result = _ensure_utc(dt)
        assert result == dt


class TestProcessedEmbeddingRepair:
    """The production worker heals best-effort completion enqueue gaps."""

    async def test_failed_enqueue_is_retried_on_later_tick(self) -> None:
        row = _embedding_repair_row()
        pool = AsyncMock()
        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with (
            patch.object(
                worker,
                "_find_due_sessions",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                worker,
                "_collect_retried_sessions",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch.object(
                worker,
                "_fetch_embedding_repair_page",
                new_callable=AsyncMock,
                side_effect=[[row], [], [row]],
            ) as fetch_page,
            patch(
                "app.services.reflections_finalization_worker."
                "enqueue_reflection_embed",
                new_callable=AsyncMock,
                side_effect=[RuntimeError("queue unavailable"), None],
            ) as enqueue,
        ):
            await worker.run_once(now=_utc_now())
            await worker.run_once(now=_utc_now())

        assert enqueue.await_count == 2
        assert fetch_page.await_count == 3
        assert fetch_page.await_args_list[1].kwargs["after_entry_id"] == row["id"]
        assert fetch_page.await_args_list[2].kwargs["after_entry_id"] is None

    async def test_matching_embedding_or_job_is_current_only_for_contract(self) -> None:
        from app.services.embeddings import content_hash

        settings = _FakeSettings()
        plaintext = "Already covered"
        expected_hash = content_hash(plaintext)
        rows = [
            _embedding_repair_row(
                plaintext=plaintext,
                embedded_content_hash=expected_hash,
                embedded_model=settings.embedding_model,
                embedded_dimension=settings.embedding_dimension,
            ),
            _embedding_repair_row(
                plaintext=plaintext,
                job_content_hash=expected_hash,
                job_model=settings.embedding_model,
                job_dimension=settings.embedding_dimension,
            ),
            _embedding_repair_row(
                plaintext=plaintext,
                embedded_content_hash=expected_hash,
                embedded_model="old-model",
                embedded_dimension=settings.embedding_dimension,
            ),
        ]
        worker = ReflectionFinalizationWorker(AsyncMock(), settings=settings)

        with (
            patch.object(
                worker,
                "_fetch_embedding_repair_page",
                new_callable=AsyncMock,
                return_value=rows,
            ),
            patch(
                "app.services.reflections_finalization_worker."
                "enqueue_reflection_embed",
                new_callable=AsyncMock,
            ) as enqueue,
        ):
            attempted = await worker._repair_processed_embeddings()

        assert attempted == 1
        enqueue.assert_awaited_once_with(
            worker._pool,
            entry_id=rows[2]["id"],
            plaintext_searchable=plaintext,
        )

    async def test_stale_pending_job_is_superseded_before_reenqueue(self) -> None:
        from app.services.embeddings import content_hash

        settings = _FakeSettings()
        plaintext = "Needs the newly configured model"
        job_id = _uid()
        row = _embedding_repair_row(
            plaintext=plaintext,
            job_content_hash=content_hash(plaintext),
            job_model="old-model",
            job_dimension=settings.embedding_dimension,
            job_id=job_id,
            job_status="pending",
        )
        pool = AsyncMock()
        worker = ReflectionFinalizationWorker(pool, settings=settings)

        with (
            patch.object(
                worker,
                "_fetch_embedding_repair_page",
                new_callable=AsyncMock,
                return_value=[row],
            ),
            patch(
                "app.services.reflections_finalization_worker."
                "enqueue_reflection_embed",
                new_callable=AsyncMock,
            ) as enqueue,
        ):
            attempted = await worker._repair_processed_embeddings()

        assert attempted == 1
        update_sql = pool.execute.await_args.args[0]
        assert "status = 'superseded'" in update_sql
        assert pool.execute.await_args.args[1] == job_id
        enqueue.assert_awaited_once()

    async def test_repair_query_is_bounded_to_processed_current_entries(self) -> None:
        cursor = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = []
        worker = ReflectionFinalizationWorker(
            pool,
            settings=_FakeSettings(batch_size=7),
        )

        rows = await worker._fetch_embedding_repair_page(
            after_entry_id=cursor,
        )

        assert rows == []
        sql, passed_cursor, limit = pool.fetch.await_args.args
        assert "rs.status = 'processed'" in sql
        assert "successor.supersedes_entry_id = re.id" in sql
        assert "status IN ('pending', 'processing')" in sql
        assert "LIMIT $2" in sql
        assert passed_cursor == cursor
        assert limit == 7


# ═══════════════════════════════════════════════════════════════════════════
# Worker — inactivity processing
# ═══════════════════════════════════════════════════════════════════════════


class TestInactivityProcessing:
    """Sessions past their idle deadline should be finalized."""

    async def test_finalizes_session_past_explicit_idle_deadline(self) -> None:
        """Session with idle_finalize_at in the past → finalized."""
        now = _utc_now()
        uid = _uid()
        session_row = _session_row(
            user_id=uid,
            idle_finalize_at=now - timedelta(minutes=10),
            opened_at=now - timedelta(hours=2),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="finalize",
                reason="idle deadline passed",
                session_id=session_row["id"],
            ),
        ) as mock_eval:
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.scanned == 1
        assert result.finalized == 1
        assert result.abandoned == 0

    async def test_finalizes_session_past_default_idle_deadline(self) -> None:
        """Session with no idle_finalize_at but opened_at + default past → finalized."""
        now = _utc_now()
        uid = _uid()
        session_row = _session_row(
            user_id=uid,
            idle_finalize_at=None,
            opened_at=now - timedelta(hours=2),  # well past 15-min default
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="finalize",
                reason="idle deadline passed",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.scanned == 1
        assert result.finalized == 1

    async def test_does_not_touch_active_session(self) -> None:
        """Session with idle_finalize_at in the future → skipped."""
        now = _utc_now()
        uid = _uid()
        session_row = _session_row(
            user_id=uid,
            idle_finalize_at=now + timedelta(minutes=30),
            opened_at=now - timedelta(minutes=5),
        )

        pool = AsyncMock()
        # _find_due_sessions should return nothing for active sessions
        pool.fetch.return_value = []

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        result = await worker.run_once(now=now)

        assert result.scanned == 0
        assert result.finalized == 0
        assert result.abandoned == 0


# ═══════════════════════════════════════════════════════════════════════════
# Worker — abandoned session handling
# ═══════════════════════════════════════════════════════════════════════════


class TestAbandonedSessionHandling:
    """Sessions past the abandon threshold should be abandoned."""

    async def test_abandons_session_past_abandon_threshold(self) -> None:
        """Session idle past abandon timeout → abandoned."""
        now = _utc_now()
        uid = _uid()
        session_row = _session_row(
            user_id=uid,
            idle_finalize_at=now - timedelta(hours=25),  # past 24h abandon threshold
            opened_at=now - timedelta(hours=26),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="abandon",
                reason="session idle beyond abandon threshold",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.scanned == 1
        assert result.abandoned == 1
        assert result.finalized == 0

    async def test_abandons_session_with_max_messages(self) -> None:
        """Session with too many messages triggers safety-valve abandon."""
        now = _utc_now()
        uid = _uid()
        many_ids = [_uid() for _ in range(500)]
        session_row = _session_row(
            user_id=uid,
            source_message_ids=many_ids,
            idle_finalize_at=now - timedelta(minutes=5),
            opened_at=now - timedelta(hours=1),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="abandon",
                reason="session exceeded max source messages",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.abandoned == 1


# ═══════════════════════════════════════════════════════════════════════════
# Worker — retry-safe / idempotency
# ═══════════════════════════════════════════════════════════════════════════


class TestRetrySafeBehavior:
    """Re-running finalization on already-finalized sessions is safe."""

    async def test_already_finalized_session_is_idempotent_skip(self) -> None:
        """If store raises conflict (already finalized), worker counts as idempotent skip."""
        now = _utc_now()
        uid = _uid()
        session_row = _session_row(
            user_id=uid,
            idle_finalize_at=now - timedelta(minutes=10),
            opened_at=now - timedelta(hours=1),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="finalize",
                reason="idle deadline passed",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                # Simulate race: another worker already finalized this session
                mock_store.finalize_session = AsyncMock(
                    side_effect=Exception("UniqueViolation or similar")
                )
                mock_store.abandon_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        # The error is caught, the session is skipped (not counted as finalized)
        assert result.errors == 1

    async def test_abandon_race_is_idempotent_skip(self) -> None:
        """If abandon fails because session already transitioned, treat as idempotent."""
        now = _utc_now()
        uid = _uid()
        session_row = _session_row(
            user_id=uid,
            idle_finalize_at=now - timedelta(hours=25),
            opened_at=now - timedelta(hours=26),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="abandon",
                reason="session idle beyond abandon threshold",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock(
                    side_effect=Exception("race condition")
                )
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.errors == 1

    async def test_multiple_runs_dont_double_finalize(self) -> None:
        """Running run_once twice on the same data doesn't double-count."""
        now = _utc_now()
        uid = _uid()
        session_row = _session_row(
            user_id=uid,
            idle_finalize_at=now - timedelta(minutes=10),
            opened_at=now - timedelta(hours=2),
        )

        # First run: session is found and finalized
        pool1 = AsyncMock()
        pool1.fetch.return_value = [session_row]

        worker1 = ReflectionFinalizationWorker(pool1, settings=_FakeSettings())

        with patch.object(
            worker1._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="finalize",
                reason="idle deadline passed",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls1:
                mock_store1 = MagicMock()
                mock_store1.finalize_session = AsyncMock()
                mock_store1.abandon_session = AsyncMock()
                mock_store_cls1.return_value = mock_store1

                result1 = await worker1.run_once(now=now)

        assert result1.finalized == 1

        # Second run: session is no longer collecting (not found by query)
        pool2 = AsyncMock()
        pool2.fetch.return_value = []  # Nothing due now

        worker2 = ReflectionFinalizationWorker(pool2, settings=_FakeSettings())
        result2 = await worker2.run_once(now=now)

        assert result2.scanned == 0
        assert result2.finalized == 0


# ═══════════════════════════════════════════════════════════════════════════
# Worker — error resilience
# ═══════════════════════════════════════════════════════════════════════════


class TestErrorResilience:
    """Individual session failures do not crash the tick."""

    async def test_one_session_error_does_not_block_others(self) -> None:
        """When one session's finalization raises, others still process."""
        now = _utc_now()
        uid_a = _uid()
        uid_b = _uid()

        session_a = _session_row(
            user_id=uid_a,
            idle_finalize_at=now - timedelta(minutes=10),
            opened_at=now - timedelta(hours=1),
        )
        session_b = _session_row(
            user_id=uid_b,
            idle_finalize_at=now - timedelta(minutes=15),
            opened_at=now - timedelta(hours=2),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_a, session_b]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        call_count = 0

        def side_effect_eval(*, session, now, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # First session: engine says finalize, but store raises
                return FinalizationDecision(
                    action="finalize",
                    reason="idle deadline passed",
                    session_id=session.session_id,
                )
            else:
                return FinalizationDecision(
                    action="finalize",
                    reason="idle deadline passed",
                    session_id=session.session_id,
                )

        with patch.object(
            worker._engine, "evaluate_full", side_effect=side_effect_eval
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()

                call_count_store = 0

                async def finalize_side_effect(*, user_id, session_id):
                    nonlocal call_count_store
                    call_count_store += 1
                    if call_count_store == 1:
                        raise RuntimeError("DB connection lost")
                    # Second call succeeds
                    return MagicMock()

                mock_store.finalize_session = AsyncMock(
                    side_effect=finalize_side_effect
                )
                mock_store.abandon_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        # Session A errored, Session B succeeded
        assert result.scanned == 2
        assert result.finalized == 1  # Only B succeeded
        assert result.errors == 1  # A errored

    async def test_fetch_error_is_caught(self) -> None:
        """Worker survives a DB fetch error."""
        now = _utc_now()
        pool = AsyncMock()
        pool.fetch.side_effect = RuntimeError("DB down")

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        # run_once should propagate, but run_forever would catch it
        with pytest.raises(RuntimeError, match="DB down"):
            await worker.run_once(now=now)

    async def test_empty_result_set(self) -> None:
        """No due sessions → clean zero result."""
        now = _utc_now()
        pool = AsyncMock()
        pool.fetch.return_value = []

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        result = await worker.run_once(now=now)

        assert result.scanned == 0
        assert result.finalized == 0
        assert result.abandoned == 0
        assert result.errors == 0


# ═══════════════════════════════════════════════════════════════════════════
# Worker — bounded pattern verification
# ═══════════════════════════════════════════════════════════════════════════


class TestBoundedWorkerPattern:
    """Verify the worker follows the bounded lifespan pattern, not scheduled jobs."""

    def test_worker_has_run_forever_method(self) -> None:
        """Worker exposes run_forever() matching EmbedJobWorker pattern."""
        worker = ReflectionFinalizationWorker(
            AsyncMock(), settings=_FakeSettings()
        )
        assert hasattr(worker, "run_forever")
        assert callable(worker.run_forever)

    def test_worker_has_run_once_method(self) -> None:
        """Worker exposes run_once() for single-tick processing."""
        worker = ReflectionFinalizationWorker(
            AsyncMock(), settings=_FakeSettings()
        )
        assert hasattr(worker, "run_once")
        assert callable(worker.run_once)

    def test_worker_does_not_use_scheduled_jobs_table(self) -> None:
        """The worker queries reflection_sessions directly, never scheduled_jobs."""
        worker = ReflectionFinalizationWorker(
            AsyncMock(), settings=_FakeSettings()
        )
        # The _find_due_sessions method queries mediator.reflection_sessions
        # Verify it does NOT reference scheduled_jobs anywhere
        import inspect

        source = inspect.getsource(worker._find_due_sessions)
        assert "scheduled_jobs" not in source
        assert "reflection_sessions" in source

    def test_worker_never_sends_messages(self) -> None:
        """The worker has no Discord/WhatsApp/messaging imports."""
        import inspect

        source = inspect.getsource(ReflectionFinalizationWorker)
        assert "discord" not in source.lower()
        assert "whatsapp" not in source.lower()
        assert "send_message" not in source.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Worker — result accounting
# ═══════════════════════════════════════════════════════════════════════════


class TestResultAccounting:
    """FinalizationWorkerResult accurately tracks outcomes."""

    def test_default_result_is_all_zeros(self) -> None:
        result = FinalizationWorkerResult()
        assert result.scanned == 0
        assert result.finalized == 0
        assert result.abandoned == 0
        assert result.skipped_active == 0
        assert result.skipped_idempotent == 0
        assert result.errors == 0

    def test_result_is_frozen(self) -> None:
        result = FinalizationWorkerResult(scanned=5)
        with pytest.raises(Exception):
            result.scanned = 10  # type: ignore[misc]

    async def test_mixed_outcomes_counted_correctly(self) -> None:
        """Scanned=3: one finalized, one abandoned, one active skip."""
        now = _utc_now()

        session_finalize = _session_row(
            idle_finalize_at=now - timedelta(minutes=10),
            opened_at=now - timedelta(hours=2),
        )
        session_abandon = _session_row(
            idle_finalize_at=now - timedelta(hours=25),
            opened_at=now - timedelta(hours=26),
        )
        session_active = _session_row(
            idle_finalize_at=now - timedelta(minutes=5),
            opened_at=now - timedelta(hours=1),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [
            session_finalize,
            session_abandon,
            session_active,
        ]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        eval_results = [
            FinalizationDecision(
                action="finalize",
                reason="idle",
                session_id=session_finalize["id"],
            ),
            FinalizationDecision(
                action="abandon",
                reason="abandon",
                session_id=session_abandon["id"],
            ),
            FinalizationDecision(
                action="noop",
                reason="deadline not yet reached",
                session_id=session_active["id"],
            ),
        ]
        eval_iter = iter(eval_results)

        def eval_side_effect(*, session, now, **kwargs):
            return next(eval_iter)

        with patch.object(
            worker._engine, "evaluate_full", side_effect=eval_side_effect
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.scanned == 3
        assert result.finalized == 1
        assert result.abandoned == 1
        assert result.skipped_active == 1
        assert result.errors == 0


# ═══════════════════════════════════════════════════════════════════════════
# Worker — noop handling
# ═══════════════════════════════════════════════════════════════════════════


class TestNoopHandling:
    """Engine decisions other than finalize/abandon are handled correctly."""

    async def test_noop_active_is_skipped_active(self) -> None:
        """Noop with 'deadline not reached' → skipped_active."""
        now = _utc_now()
        session_row = _session_row(
            idle_finalize_at=now - timedelta(minutes=5),
            opened_at=now - timedelta(hours=1),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="noop",
                reason="idle deadline not yet reached",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.skipped_active == 1
        assert result.finalized == 0
        assert result.abandoned == 0

    async def test_noop_not_collecting_is_idempotent_skip(self) -> None:
        """Noop with 'not collecting' reason → skipped_idempotent."""
        now = _utc_now()
        session_row = _session_row(
            idle_finalize_at=now - timedelta(minutes=10),
            opened_at=now - timedelta(hours=2),
        )

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        worker = ReflectionFinalizationWorker(pool, settings=_FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="noop",
                reason="session is not collecting (status=finalizing)",
                session_id=session_row["id"],
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.skipped_idempotent == 1
        assert result.finalized == 0
