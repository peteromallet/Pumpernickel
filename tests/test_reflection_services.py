"""Tests for the reflection session storage APIs.

Covers:
- Module-level constructs (exceptions, validators, enums)
- ReflectionSession / ReflectionEntry / ReflectionDerivation from_row
- Validation helpers
- Store instantiation
- Live-DB tests: privacy boundaries, concurrency, claim/retry/recovery
  idempotency, immutable correction revisions, derivation traceability
  and idempotency (skipped when DATABASE_URL / EVAL_DATABASE_URL not set).
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.services.reflections import (
    ReflectionSession,
    ReflectionEntry,
    ReflectionDerivation,
    ReflectionStore,
    SessionNotFoundError,
    SessionNotCollectingError,
    SessionNotFinalizingError,
    SessionClaimConflictError,
    SessionFinalizeConflictError,
    EntryNotFoundError,
    EntryRevisionConflictError,
    EntryCorrectionError,
    DerivationNotFoundError,
    DerivationIdempotencyConflictError,
    DerivationDecisionError,
    VALID_STATUSES,
    VALID_TEMPORAL_SCOPES,
    VALID_PHASES,
    VALID_FAILURE_CLASSES,
    VALID_DERIVATION_KINDS,
    VALID_ASSERTION_SOURCES,
    VALID_DECISIONS,
)


# ── Module-level constructs ────────────────────────────────────────────


class TestModuleExports:
    """Verify all expected public names are importable."""

    def test_exceptions_are_distinct(self) -> None:
        assert issubclass(SessionNotFoundError, LookupError)
        assert issubclass(SessionNotCollectingError, ValueError)
        assert issubclass(SessionNotFinalizingError, ValueError)
        assert issubclass(SessionClaimConflictError, RuntimeError)
        assert issubclass(SessionFinalizeConflictError, RuntimeError)

    def test_status_enums(self) -> None:
        assert VALID_STATUSES == {
            "collecting", "finalizing", "processed", "abandoned", "processing_failed"
        }

    def test_temporal_scope_enums(self) -> None:
        assert VALID_TEMPORAL_SCOPES == {
            "instant", "day", "week", "month", "custom", "none"
        }

    def test_phase_enums(self) -> None:
        assert VALID_PHASES == {
            "opening", "closing", "checkpoint", "prospective", "retrospective", "freeform"
        }

    def test_failure_class_enums(self) -> None:
        assert VALID_FAILURE_CLASSES == {
            "retryable_processor", "terminal_input", "terminal_internal", "stale_claim"
        }


# ── ReflectionSession.from_row ─────────────────────────────────────────


def _session_dict(**overrides) -> dict:
    now = datetime.now(timezone.utc)
    uid = uuid4()
    d = {
        "id": uid,
        "user_id": uid,
        "topic_id": None,
        "bot_id": "test_bot",
        "opened_by_message_id": None,
        "opened_by_turn_id": None,
        "source_message_ids": [],
        "template_key": "freeform",
        "temporal_scope": "instant",
        "phase": "freeform",
        "period_start": None,
        "period_end": None,
        "timezone": "UTC",
        "classification_source": None,
        "classification_confidence": None,
        "classification_metadata": None,
        "status": "collecting",
        "idle_finalize_at": None,
        "finalized_at": None,
        "processed_at": None,
        "abandoned_at": None,
        "claimed_by": None,
        "claimed_at": None,
        "retry_count": 0,
        "failure_class": None,
        "failure_reason": None,
        "last_error": None,
        "idempotency_key": None,
        "created_at": now,
        "updated_at": now,
    }
    d.update(overrides)
    return d


class _RecordLike:
    """Minimal asyncpg-Record-like object supporting both key and attribute access."""

    def __init__(self, d: dict) -> None:
        self._d = d

    def __getitem__(self, key: str):
        return self._d[key]

    def get(self, key: str, default=None):
        return self._d.get(key, default)


class TestFromRow:
    """ReflectionSession.from_row with dict and record-like inputs."""

    def test_from_dict_basic(self) -> None:
        d = _session_dict()
        s = ReflectionSession.from_row(d)
        assert s.id == d["id"]
        assert s.user_id == d["user_id"]
        assert s.bot_id == "test_bot"
        assert s.status == "collecting"
        assert s.template_key == "freeform"
        assert s.retry_count == 0

    def test_from_record_like(self) -> None:
        d = _session_dict()
        rec = _RecordLike(d)
        s = ReflectionSession.from_row(rec)
        assert s.id == d["id"]
        assert s.status == "collecting"

    def test_source_message_ids_list(self) -> None:
        msg_ids = [uuid4(), uuid4()]
        d = _session_dict(source_message_ids=msg_ids)
        s = ReflectionSession.from_row(d)
        assert s.source_message_ids == msg_ids

    def test_source_message_ids_none_becomes_empty(self) -> None:
        d = _session_dict(source_message_ids=None)
        s = ReflectionSession.from_row(d)
        assert s.source_message_ids == []

    def test_classification_metadata_json_string(self) -> None:
        d = _session_dict(classification_metadata='{"key": "value"}')
        s = ReflectionSession.from_row(d)
        assert s.classification_metadata == {"key": "value"}

    def test_classification_metadata_invalid_json(self) -> None:
        d = _session_dict(classification_metadata="not-json")
        s = ReflectionSession.from_row(d)
        assert s.classification_metadata is None

    def test_retry_count_none_becomes_zero(self) -> None:
        d = _session_dict(retry_count=None)
        s = ReflectionSession.from_row(d)
        assert s.retry_count == 0

    def test_frozen_dataclass(self) -> None:
        d = _session_dict()
        s = ReflectionSession.from_row(d)
        with pytest.raises(Exception):
            s.status = "processed"  # type: ignore[misc]

    def test_all_statuses_accepted(self) -> None:
        for status in VALID_STATUSES:
            d = _session_dict(status=status)
            s = ReflectionSession.from_row(d)
            assert s.status == status


# ── Store instantiation ────────────────────────────────────────────────


class TestStoreInstantiation:
    """ReflectionStore requires a pool object."""

    def test_instantiate_with_pool(self) -> None:
        store = ReflectionStore(pool=object())
        assert store._pool is not None


# ── Validation helpers (indirectly via public API) ─────────────────────


class TestValidation:
    """Validation is tested indirectly through the store API contract.

    These tests verify the validation helper functions behave as expected
    when exercised at the module boundary.
    """

    def test_failure_class_valid_values(self) -> None:
        from app.services.reflections import _validate_failure_class

        for fc in VALID_FAILURE_CLASSES:
            assert _validate_failure_class(fc) == fc

    def test_failure_class_none_ok(self) -> None:
        from app.services.reflections import _validate_failure_class

        assert _validate_failure_class(None) is None

    def test_failure_class_invalid_raises(self) -> None:
        from app.services.reflections import _validate_failure_class

        with pytest.raises(ValueError, match="invalid failure_class"):
            _validate_failure_class("bogus")

    def test_status_valid_values(self) -> None:
        from app.services.reflections import _validate_status

        for s in VALID_STATUSES:
            assert _validate_status(s) == s

    def test_status_invalid_raises(self) -> None:
        from app.services.reflections import _validate_status

        with pytest.raises(ValueError, match="invalid status"):
            _validate_status("bogus")

    def test_temporal_scope_valid_values(self) -> None:
        from app.services.reflections import _validate_temporal_scope

        for s in VALID_TEMPORAL_SCOPES:
            assert _validate_temporal_scope(s) == s

    def test_temporal_scope_invalid_raises(self) -> None:
        from app.services.reflections import _validate_temporal_scope

        with pytest.raises(ValueError, match="invalid temporal_scope"):
            _validate_temporal_scope("bogus")

    def test_phase_valid_values(self) -> None:
        from app.services.reflections import _validate_phase

        for p in VALID_PHASES:
            assert _validate_phase(p) == p

    def test_phase_invalid_raises(self) -> None:
        from app.services.reflections import _validate_phase

        with pytest.raises(ValueError, match="invalid phase"):
            _validate_phase("bogus")

    def test_require_user_id_rejects_none(self) -> None:
        from app.services.reflections import _require_user_id

        with pytest.raises(ValueError, match="user_id is required"):
            _require_user_id(None)

    def test_require_bot_id_rejects_empty(self) -> None:
        from app.services.reflections import _require_bot_id

        with pytest.raises(ValueError, match="bot_id must be"):
            _require_bot_id("")

    def test_require_bot_id_rejects_whitespace(self) -> None:
        from app.services.reflections import _require_bot_id

        with pytest.raises(ValueError, match="bot_id must be"):
            _require_bot_id("   ")


# ═════════════════════════════════════════════════════════════════════════
# Live-DB tests — privacy, concurrency, claim/retry/recovery idempotency,
# correction immutability, and derivation traceability/idempotency.
# Skipped when DATABASE_URL / EVAL_DATABASE_URL not set.
# ═════════════════════════════════════════════════════════════════════════

# ── Helpers ────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _future(minutes: int = 10) -> datetime:
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


async def _setup_session(
    store: ReflectionStore,
    *,
    user_id: UUID,
    bot_id: str = "test_bot",
    status: str = "collecting",
) -> ReflectionSession:
    """Create a session in the given status via open or direct manipulation."""
    session = await store.open_or_attach_session(
        user_id=user_id,
        bot_id=bot_id,
        template_key="freeform",
        temporal_scope="instant",
        phase="freeform",
    )
    if status == "collecting":
        return session
    if status == "finalizing":
        return await store.finalize_session(user_id=user_id, session_id=session.id)
    if status == "processing_failed":
        finalized = await store.finalize_session(user_id=user_id, session_id=session.id)
        claimed = await store.claim_session(claimed_by="test_worker")
        assert claimed is not None
        result = await store.mark_session_failed(
            session_id=claimed.id,
            claimed_by="test_worker",
            failure_class="retryable_processor",
            failure_reason="test failure",
        )
        assert result is not None
        return result
    raise ValueError(f"Unsupported status: {status}")


async def _create_entry_via_claim(
    store: ReflectionStore,
    *,
    user_id: UUID,
    session_id: UUID,
    bot_id: str = "test_bot",
    payload: dict | None = None,
) -> ReflectionEntry:
    """Finalize, claim-and-create-entry in one atomic step."""
    await store.finalize_session(user_id=user_id, session_id=session_id)
    entry, _sess = await store.create_entry_for_claim(
        user_id=user_id,
        session_id=session_id,
        claimed_by="test_worker",
        bot_id=bot_id,
        payload=payload,
    )
    return entry


# ── Privacy boundary tests ─────────────────────────────────────────────


class TestPrivacyBoundaries:
    """Cross-user, cross-bot rejection at service level.

    These tests verify that user A cannot access, modify, or list user B's
    sessions, entries, or derivations.  They also cover scoped list
    operations with bot_id and topic_id filters.
    """

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
            pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")

    # ── Session privacy ──────────────────────────────────────────────

    @pytest.mark.anyio
    async def test_user_a_cannot_get_user_b_session(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await store.open_or_attach_session(
                    user_id=user_a, bot_id="bot_x",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                # User B tries to get User A's session
                result = await store.get_session(user_id=user_b, session_id=session_a.id)
                assert result is None, "User B should not see User A's session"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_finalize_user_b_session(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await store.open_or_attach_session(
                    user_id=user_a, bot_id="bot_x",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                with pytest.raises(SessionNotFoundError, match="not found for user"):
                    await store.finalize_session(user_id=user_b, session_id=session_a.id)
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_list_sessions_scoped_to_user(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                await store.open_or_attach_session(
                    user_id=user_a, bot_id="bot_x",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                await store.open_or_attach_session(
                    user_id=user_b, bot_id="bot_x",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                sessions_a = await store.list_sessions(user_id=user_a)
                sessions_b = await store.list_sessions(user_id=user_b)
                assert len(sessions_a) == 1, "User A should see only their session"
                assert len(sessions_b) == 1, "User B should see only their session"
                assert sessions_a[0].user_id == user_a
                assert sessions_b[0].user_id == user_b
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_abandon_user_b_session(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await store.open_or_attach_session(
                    user_id=user_a, bot_id="bot_x",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                with pytest.raises(SessionNotFoundError, match="not found for user"):
                    await store.abandon_session(user_id=user_b, session_id=session_a.id)
        finally:
            await pool.close()

    # ── Entry privacy ─────────────────────────────────────────────────

    @pytest.mark.anyio
    async def test_user_a_cannot_get_user_b_entry(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                result = await store.get_entry(user_id=user_b, entry_id=entry.id)
                assert result is None, "User B should not see User A's entry"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_list_user_b_entries(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                entries = await store.list_entries(user_id=user_b, session_id=session_a.id)
                assert len(entries) == 0, "User B should not see entries for User A's session"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_get_current_entry_user_b(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                result = await store.get_current_entry(user_id=user_b, session_id=session_a.id)
                assert result is None, "User B should not see current entry for User A's session"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_correct_user_b_entry(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                with pytest.raises(EntryNotFoundError, match="not found for user"):
                    await store.correct_entry(
                        user_id=user_b,
                        supersedes_entry_id=entry.id,
                        bot_id="bot_x",
                        summary="attempted correction",
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_get_revision_history_user_b(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                history = await store.get_entry_revision_history(
                    user_id=user_b, session_id=session_a.id,
                )
                assert len(history) == 0, "User B should not see revision history for User A"
        finally:
            await pool.close()

    # ── Derivation privacy ────────────────────────────────────────────

    @pytest.mark.anyio
    async def test_user_a_cannot_get_user_b_derivation(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                derivation = await store.create_derivation(
                    user_id=user_a,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="agent_inferred",
                    idempotency_key=f"ik_{uuid4().hex[:12]}",
                )
                result = await store.get_derivation(user_id=user_b, derivation_id=derivation.id)
                assert result is None, "User B should not see User A's derivation"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_list_derivations_for_user_b_entry(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                await store.create_derivation(
                    user_id=user_a,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="agent_inferred",
                )
                derivations = await store.list_derivations_for_entry(
                    user_id=user_b, reflection_entry_id=entry.id,
                )
                assert len(derivations) == 0, "User B should not see derivations for User A's entry"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_user_a_cannot_update_user_b_derivation_decision(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user_a, user_b = uuid4(), uuid4()

                session_a = await _setup_session(store, user_id=user_a, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user_a, session_id=session_a.id,
                    bot_id="bot_x",
                )
                derivation = await store.create_derivation(
                    user_id=user_a,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="agent_inferred",
                )
                with pytest.raises(DerivationNotFoundError, match="not found for user"):
                    await store.update_derivation_decision(
                        user_id=user_b,
                        derivation_id=derivation.id,
                        decision="applied",
                        applied_target_table="memories",
                        applied_target_id=uuid4(),
                    )
        finally:
            await pool.close()

    # ── Cross-bot scoping ─────────────────────────────────────────────

    @pytest.mark.anyio
    async def test_entries_scoped_by_bot_id_in_list(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session_a = await _setup_session(store, user_id=user, bot_id="bot_alpha", status="finalizing")
                session_b = await _setup_session(store, user_id=user, bot_id="bot_beta", status="finalizing")
                await _create_entry_via_claim(store, user_id=user, session_id=session_a.id, bot_id="bot_alpha")
                await _create_entry_via_claim(store, user_id=user, session_id=session_b.id, bot_id="bot_beta")

                entries_alpha = await store.list_entries(user_id=user, bot_id="bot_alpha")
                entries_beta = await store.list_entries(user_id=user, bot_id="bot_beta")
                assert len(entries_alpha) == 1
                assert entries_alpha[0].bot_id == "bot_alpha"
                assert len(entries_beta) == 1
                assert entries_beta[0].bot_id == "bot_beta"
        finally:
            await pool.close()

    # ── Cross-topic scoping ───────────────────────────────────────────

    @pytest.mark.anyio
    async def test_entries_scoped_by_topic_id_in_list(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                topic_a, topic_b = uuid4(), uuid4()

                session_a = await store.open_or_attach_session(
                    user_id=user, bot_id="bot_x", topic_id=topic_a,
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                session_b = await store.open_or_attach_session(
                    user_id=user, bot_id="bot_x", topic_id=topic_b,
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                await _create_entry_via_claim(
                    store, user_id=user, session_id=session_a.id, bot_id="bot_x",
                )
                await _create_entry_via_claim(
                    store, user_id=user, session_id=session_b.id, bot_id="bot_x",
                )

                entries_a = await store.list_entries(user_id=user, topic_id=topic_a)
                entries_b = await store.list_entries(user_id=user, topic_id=topic_b)
                assert len(entries_a) == 1
                assert entries_a[0].topic_id == topic_a
                assert len(entries_b) == 1
                assert entries_b[0].topic_id == topic_b
        finally:
            await pool.close()


# ── Concurrency tests ──────────────────────────────────────────────────


class TestConcurrentSessionOpen:
    """Concurrent open_or_attach_session must produce exactly one collecting session."""

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
            pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")

    @pytest.mark.anyio
    async def test_concurrent_opens_create_one_collecting_session(self) -> None:
        """Two callers racing to open the same (user_id, bot_id) => exactly 1 collecting."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                bot = "concurrent_bot"

                async def _open() -> ReflectionSession:
                    return await store.open_or_attach_session(
                        user_id=user,
                        bot_id=bot,
                        template_key="freeform",
                        temporal_scope="instant",
                        phase="freeform",
                        idempotency_key=f"ik_{uuid4().hex[:12]}",
                    )

                results = await asyncio.gather(_open(), _open(), _open())
                session_ids = {s.id for s in results}

                # All three should return the same session
                assert len(session_ids) == 1, (
                    f"Expected exactly 1 collecting session, got {len(session_ids)}: {session_ids}"
                )

                # Verify only one collecting session exists in DB
                sessions = await store.list_sessions(user_id=user, statuses=["collecting"])
                assert len(sessions) == 1
                assert sessions[0].id == next(iter(session_ids))
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_concurrent_opens_deduplicate_source_message_ids(self) -> None:
        """Concurrent attaches should merge source_message_ids without duplicates."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                bot = "dedup_bot"
                msg_a, msg_b, msg_c = uuid4(), uuid4(), uuid4()

                # First open creates the session with msg_a
                session = await store.open_or_attach_session(
                    user_id=user, bot_id=bot,
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                    source_message_ids=[msg_a],
                )
                assert msg_a in session.source_message_ids

                # Concurrent attaches with overlapping and new messages
                async def _attach(msgs: list[UUID]) -> ReflectionSession:
                    return await store.open_or_attach_session(
                        user_id=user, bot_id=bot,
                        template_key="freeform", temporal_scope="instant",
                        phase="freeform",
                        source_message_ids=msgs,
                    )

                results = await asyncio.gather(
                    _attach([msg_a, msg_b]),  # overlaps with original
                    _attach([msg_b, msg_c]),  # overlaps with both
                )

                # Both should return the same session
                assert results[0].id == session.id
                assert results[1].id == session.id

                # Final session should have all three messages, no duplicates
                final = await store.get_session(user_id=user, session_id=session.id)
                assert final is not None
                assert set(final.source_message_ids) == {msg_a, msg_b, msg_c}
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_different_bots_create_different_collecting_sessions(self) -> None:
        """Same user with different bots => separate collecting sessions."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                s_x = await store.open_or_attach_session(
                    user_id=user, bot_id="bot_x",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                s_y = await store.open_or_attach_session(
                    user_id=user, bot_id="bot_y",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                assert s_x.id != s_y.id, "Different bots should get different collecting sessions"

                sessions = await store.list_sessions(user_id=user, statuses=["collecting"])
                assert len(sessions) == 2
        finally:
            await pool.close()


# ── Claim / retry / recovery idempotency tests ────────────────────────


class TestClaimRetryRecovery:
    """Finalized session claim, retry, and recovery idempotency."""

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
            pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")

    @pytest.mark.anyio
    async def test_claim_session_returns_oldest_finalized(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                s1 = await _setup_session(store, user_id=user, status="finalizing")
                await asyncio.sleep(0.01)  # ensure different finalized_at
                s2 = await _setup_session(store, user_id=user, bot_id="bot_2", status="finalizing")

                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is not None
                # Should claim the oldest (s1)
                assert claimed.id == s1.id
                assert claimed.claimed_by == "worker_1"
                assert claimed.retry_count == 0
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_claim_session_returns_none_when_none_available(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                # Create only collecting session — not claimable
                await store.open_or_attach_session(
                    user_id=user, bot_id="bot_x",
                    template_key="freeform", temporal_scope="instant",
                    phase="freeform",
                )
                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is None
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_two_workers_cannot_claim_same_session(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                await _setup_session(store, user_id=user, status="finalizing")

                async def _claim(worker: str) -> ReflectionSession | None:
                    return await store.claim_session(claimed_by=worker)

                results = await asyncio.gather(_claim("worker_1"), _claim("worker_2"))
                claimed = [r for r in results if r is not None]
                assert len(claimed) == 1, (
                    f"Only one worker should succeed, got {len(claimed)}"
                )
                assert claimed[0].claimed_by in ("worker_1", "worker_2")
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_mark_session_processed_transitions_correctly(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is not None

                processed = await store.mark_session_processed(
                    session_id=claimed.id, claimed_by="worker_1",
                )
                assert processed is not None
                assert processed.status == "processed"
                assert processed.processed_at is not None
                assert processed.claimed_by is None  # claim cleared
                assert processed.failure_class is None
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_mark_session_processed_wrong_claimant_fails(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is not None

                result = await store.mark_session_processed(
                    session_id=claimed.id, claimed_by="worker_2",
                )
                assert result is None, "Wrong claimant should not be able to mark processed"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_mark_session_failed_transitions_correctly(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is not None

                failed = await store.mark_session_failed(
                    session_id=claimed.id,
                    claimed_by="worker_1",
                    failure_class="retryable_processor",
                    failure_reason="transient error",
                    last_error="Connection reset",
                )
                assert failed is not None
                assert failed.status == "processing_failed"
                assert failed.failure_class == "retryable_processor"
                assert failed.failure_reason == "transient error"
                assert failed.last_error == "Connection reset"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_retry_session_transitions_back_to_finalizing(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="processing_failed")
                assert session.retry_count == 0

                retried = await store.retry_session(user_id=user, session_id=session.id)
                assert retried.status == "finalizing"
                assert retried.retry_count == 1
                assert retried.claimed_by is None
                assert retried.failure_class is None
                assert retried.failure_reason is None
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_retry_session_idempotent_twice_fails(self) -> None:
        """Retrying a session that's already back in finalizing should fail."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="processing_failed")
                await store.retry_session(user_id=user, session_id=session.id)

                with pytest.raises(ValueError, match="expected 'processing_failed'"):
                    await store.retry_session(user_id=user, session_id=session.id)
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_recover_stale_claims_clears_stale(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is not None

                # Manually backdate claimed_at to simulate staleness
                await pool.execute(
                    """UPDATE mediator.reflection_sessions
                       SET claimed_at = $1
                       WHERE id = $2""",
                    datetime.now(timezone.utc) - timedelta(seconds=600),
                    claimed.id,
                )

                recovered = await store.recover_stale_claims(stale_claim_seconds=300)
                assert len(recovered) >= 1
                recovered_session = [s for s in recovered if s.id == claimed.id][0]
                assert recovered_session.failure_class == "stale_claim"
                assert recovered_session.claimed_by is None
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_release_claim_gracefully_returns_to_finalizing(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is not None

                released = await store.release_claim(
                    session_id=claimed.id, claimed_by="worker_1",
                )
                assert released is not None
                assert released.status == "finalizing"
                assert released.claimed_by is None
                assert released.claimed_at is None
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_release_claim_wrong_claimant_fails(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                claimed = await store.claim_session(claimed_by="worker_1")
                assert claimed is not None

                result = await store.release_claim(
                    session_id=claimed.id, claimed_by="worker_2",
                )
                assert result is None
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_atomic_claim_and_entry_creation(self) -> None:
        """create_entry_for_claim atomically claims session and creates first entry."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry, claimed_session = await store.create_entry_for_claim(
                    user_id=user,
                    session_id=session.id,
                    claimed_by="worker_1",
                    bot_id="test_bot",
                    payload={"summary": "first entry"},
                )
                assert entry.revision_number == 1
                assert entry.session_id == session.id
                assert claimed_session.claimed_by == "worker_1"
                assert claimed_session.status == "finalizing"

                # Verify session is now claimed
                stored = await store.get_session(user_id=user, session_id=session.id)
                assert stored is not None
                assert stored.claimed_by == "worker_1"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_claim_nonexistent_session_raises(self) -> None:
        """create_entry_for_claim on nonexistent session raises SessionClaimConflictError."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                fake_id = uuid4()

                with pytest.raises(SessionClaimConflictError, match="Cannot claim"):
                    await store.create_entry_for_claim(
                        user_id=user,
                        session_id=fake_id,
                        claimed_by="worker_1",
                        bot_id="test_bot",
                    )
        finally:
            await pool.close()


# ── Correction immutability tests ──────────────────────────────────────


class TestCorrectionImmutability:
    """Corrections must preserve old immutable revisions while creating a new current revision."""

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
            pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")

    @pytest.mark.anyio
    async def test_correction_creates_new_revision(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                original = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                    payload={"summary": "original"},
                )
                assert original.revision_number == 1
                assert original.supersedes_entry_id is None

                corrected = await store.correct_entry(
                    user_id=user,
                    supersedes_entry_id=original.id,
                    bot_id="test_bot",
                    summary="corrected version",
                    payload={"summary": "corrected"},
                )
                assert corrected.id != original.id
                assert corrected.revision_number == 2
                assert corrected.supersedes_entry_id == original.id
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_original_entry_unchanged_after_correction(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                original = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                    payload={"summary": "original"},
                    plaintext_searchable="original plaintext",
                )

                await store.correct_entry(
                    user_id=user,
                    supersedes_entry_id=original.id,
                    bot_id="test_bot",
                    summary="corrected",
                    payload={"summary": "corrected"},
                    plaintext_searchable="corrected plaintext",
                )

                # Re-read the original from DB
                original_re_read = await store.get_entry(user_id=user, entry_id=original.id)
                assert original_re_read is not None
                assert original_re_read.revision_number == 1
                assert original_re_read.supersedes_entry_id is None  # never mutated
                assert original_re_read.plaintext_searchable == "original plaintext"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_get_current_entry_returns_newest_unsuperseded(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                original = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                corrected = await store.correct_entry(
                    user_id=user,
                    supersedes_entry_id=original.id,
                    bot_id="test_bot",
                    summary="corrected",
                )

                current = await store.get_current_entry(user_id=user, session_id=session.id)
                assert current is not None
                assert current.id == corrected.id
                assert current.revision_number == 2
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_revision_history_includes_all_revisions(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                original = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                corrected = await store.correct_entry(
                    user_id=user,
                    supersedes_entry_id=original.id,
                    bot_id="test_bot",
                    summary="corrected",
                )
                # Second correction
                re_corrected = await store.correct_entry(
                    user_id=user,
                    supersedes_entry_id=corrected.id,
                    bot_id="test_bot",
                    summary="re-corrected",
                )

                history = await store.get_entry_revision_history(
                    user_id=user, session_id=session.id,
                )
                assert len(history) == 3
                assert history[0].id == original.id
                assert history[0].revision_number == 1
                assert history[1].id == corrected.id
                assert history[1].revision_number == 2
                assert history[1].supersedes_entry_id == original.id
                assert history[2].id == re_corrected.id
                assert history[2].revision_number == 3
                assert history[2].supersedes_entry_id == corrected.id
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_correction_on_nonexistent_entry_raises(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                fake_id = uuid4()

                with pytest.raises(EntryNotFoundError, match="not found for user"):
                    await store.correct_entry(
                        user_id=user,
                        supersedes_entry_id=fake_id,
                        bot_id="test_bot",
                        summary="attempt",
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_correction_with_none_supersedes_entry_id_raises(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                with pytest.raises(EntryCorrectionError, match="supersedes_entry_id is required"):
                    await store.correct_entry(
                        user_id=user,
                        supersedes_entry_id=None,  # type: ignore[arg-type]
                        bot_id="test_bot",
                        summary="attempt",
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_list_entries_current_only_excludes_superseded(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                original = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                await store.correct_entry(
                    user_id=user,
                    supersedes_entry_id=original.id,
                    bot_id="test_bot",
                    summary="corrected",
                )

                entries = await store.list_entries(
                    user_id=user, session_id=session.id, current_only=True,
                )
                assert len(entries) == 1
                assert entries[0].supersedes_entry_id is None
                assert entries[0].revision_number == 2

                # Full history includes both
                full = await store.list_entries(
                    user_id=user, session_id=session.id, current_only=False,
                )
                assert len(full) == 2
        finally:
            await pool.close()


# ── Derivation traceability and idempotency tests ──────────────────────


class TestDerivationTraceability:
    """Derivations must be traceable to entries and idempotent on retry."""

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
            pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")

    @pytest.mark.anyio
    async def test_create_derivation_is_traceable_to_entry(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )

                derivation = await store.create_derivation(
                    user_id=user,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="user_explicit",
                    confidence=0.95,
                    eligibility_reasons=["rule_001", "rule_002"],
                    supporting_message_ids=[uuid4()],
                    decision="deferred",
                )
                assert derivation.reflection_entry_id == entry.id
                assert derivation.user_id == user
                assert derivation.derivation_kind == "memory"
                assert derivation.assertion_source == "user_explicit"
                assert derivation.confidence == 0.95
                assert derivation.eligibility_reasons == ["rule_001", "rule_002"]
                assert derivation.decision == "deferred"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_derivation_idempotency_key_prevents_duplicates(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                ik = f"ik_derivation_{uuid4().hex[:12]}"

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )

                d1 = await store.create_derivation(
                    user_id=user,
                    reflection_entry_id=entry.id,
                    derivation_kind="observation",
                    assertion_source="agent_inferred",
                    idempotency_key=ik,
                    decision="deferred",
                )
                # Second submission with same idempotency_key
                d2 = await store.create_derivation(
                    user_id=user,
                    reflection_entry_id=entry.id,
                    derivation_kind="observation",
                    assertion_source="agent_inferred",
                    idempotency_key=ik,
                    decision="applied",  # different decision, should be ignored
                )
                assert d2.id == d1.id, "Idempotent retry should return existing derivation"
                assert d2.decision == "deferred", "Original decision should be preserved"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_derivation_without_idempotency_key_creates_new_each_time(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )

                d1 = await store.create_derivation(
                    user_id=user,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="user_explicit",
                )
                d2 = await store.create_derivation(
                    user_id=user,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="user_explicit",
                )
                assert d1.id != d2.id, "Without idempotency_key, each call creates new derivation"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_update_derivation_decision_transitions_correctly(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )

                derivation = await store.create_derivation(
                    user_id=user,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="agent_inferred",
                    decision="deferred",
                )
                target_id = uuid4()
                updated = await store.update_derivation_decision(
                    user_id=user,
                    derivation_id=derivation.id,
                    decision="applied",
                    applied_target_table="memories",
                    applied_target_id=target_id,
                    processor_version="v1.2.3",
                )
                assert updated.decision == "applied"
                assert updated.decided_at is not None
                assert updated.applied_target_table == "memories"
                assert updated.applied_target_id == target_id
                assert updated.processor_version == "v1.2.3"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_update_derivation_decision_applied_requires_targets(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                derivation = await store.create_derivation(
                    user_id=user,
                    reflection_entry_id=entry.id,
                    derivation_kind="memory",
                    assertion_source="agent_inferred",
                )

                with pytest.raises(DerivationDecisionError, match="requires both"):
                    await store.update_derivation_decision(
                        user_id=user,
                        derivation_id=derivation.id,
                        decision="applied",
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_list_derivations_for_entry_filters_by_kind(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="memory", assertion_source="user_explicit",
                )
                await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="observation", assertion_source="agent_inferred",
                )
                await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="distillation", assertion_source="agent_inferred",
                )

                memories = await store.list_derivations_for_entry(
                    user_id=user, reflection_entry_id=entry.id, derivation_kind="memory",
                )
                assert len(memories) == 1
                assert memories[0].derivation_kind == "memory"

                all_derivs = await store.list_derivations_for_entry(
                    user_id=user, reflection_entry_id=entry.id,
                )
                assert len(all_derivs) == 3
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_list_derivations_for_entry_filters_by_decision(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                d1 = await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="memory", assertion_source="user_explicit",
                    decision="deferred",
                )
                d2 = await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="observation", assertion_source="agent_inferred",
                    decision="deferred",
                )
                await store.update_derivation_decision(
                    user_id=user, derivation_id=d2.id, decision="rejected",
                )

                deferred = await store.list_derivations_for_entry(
                    user_id=user, reflection_entry_id=entry.id, decision="deferred",
                )
                rejected = await store.list_derivations_for_entry(
                    user_id=user, reflection_entry_id=entry.id, decision="rejected",
                )
                assert len(deferred) == 1
                assert deferred[0].id == d1.id
                assert len(rejected) == 1
                assert rejected[0].id == d2.id
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_list_derivations_for_session_joins_correctly(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="memory", assertion_source="user_explicit",
                )
                await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="observation", assertion_source="agent_inferred",
                )

                derivations = await store.list_derivations_for_session(
                    user_id=user, session_id=session.id,
                )
                assert len(derivations) == 2
                assert all(d.user_id == user for d in derivations)
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_get_derivation_by_idempotency_key(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                ik = f"ik_lookup_{uuid4().hex[:12]}"

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                created = await store.create_derivation(
                    user_id=user, reflection_entry_id=entry.id,
                    derivation_kind="memory", assertion_source="user_explicit",
                    idempotency_key=ik,
                )

                found = await store.get_derivation_by_idempotency_key(
                    user_id=user, idempotency_key=ik,
                )
                assert found is not None
                assert found.id == created.id

                not_found = await store.get_derivation_by_idempotency_key(
                    user_id=user, idempotency_key="nonexistent_key",
                )
                assert not_found is None
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_create_derivation_on_nonexistent_entry_raises(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()
                fake_entry_id = uuid4()

                with pytest.raises(EntryNotFoundError, match="not found for user"):
                    await store.create_derivation(
                        user_id=user,
                        reflection_entry_id=fake_entry_id,
                        derivation_kind="memory",
                        assertion_source="user_explicit",
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_create_derivation_applied_requires_targets(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                with pytest.raises(DerivationDecisionError, match="requires both"):
                    await store.create_derivation(
                        user_id=user,
                        reflection_entry_id=entry.id,
                        derivation_kind="memory",
                        assertion_source="user_explicit",
                        decision="applied",
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_create_derivation_invalid_kind_raises(self) -> None:
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool) as scratch:
                store = ReflectionStore(pool)
                user = uuid4()

                session = await _setup_session(store, user_id=user, status="finalizing")
                entry = await _create_entry_via_claim(
                    store, user_id=user, session_id=session.id,
                    bot_id="test_bot",
                )
                with pytest.raises(ValueError, match="invalid derivation_kind"):
                    await store.create_derivation(
                        user_id=user,
                        reflection_entry_id=entry.id,
                        derivation_kind="bogus_kind",
                        assertion_source="user_explicit",
                    )
        finally:
            await pool.close()
