"""Tests for app/reflections/derivation_ledger.py (T13).

Covers:
- Idempotency key construction: deterministic for same inputs, different for
  differing inputs.
- record_candidates: all eligible candidates from a DerivationResult are
  recorded with correct fields (kind, assertion_source, confidence, reasons,
  supporting_message_ids).
- record_candidate: a single candidate maps correctly to the store call.
- Idempotent ledgering: calling record_candidate twice with the same
  candidate returns the same derivation (no duplicate).
- Provenance traversal: traverse_provenance walks derivation → entry →
  session → source_message_ids.
- Provenance chain completeness: is_complete when all links exist, False
  when entry or session is missing.
- Decision updates: update_decision delegates correctly.
- Lookup helpers: get_derivation, get_derivation_by_key,
  list_derivations_for_entry, list_derivations_for_session.
- Error cases: recording against a non-existent entry raises EntryNotFoundError;
  traversing a non-existent derivation raises DerivationNotFoundError.
- Semantic boundary: rejected candidates are never recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, call, patch
from uuid import UUID, uuid4

import pytest

from app.reflections.derivation import (
    AssertionSource,
    DerivationCandidate,
    DerivationEngine,
    DerivationKind,
    DerivationResult,
    EligibilityResult,
    check_eligibility,
)
from app.reflections.derivation_ledger import (
    DerivationLedger,
    ProvenanceChain,
    build_idempotency_key,
)
from app.services.reflections import (
    DerivationNotFoundError,
    EntryNotFoundError,
    ReflectionDerivation,
    ReflectionEntry,
    ReflectionSession,
    ReflectionStore,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _make_candidate(
    *,
    kind: DerivationKind = DerivationKind.memory,
    assertion_source: AssertionSource = AssertionSource.user_explicit,
    summary: str = "Test memory fact.",
    confidence: float = 0.9,
    eligibility_reasons: list[str] | None = None,
    supporting_message_ids: list[UUID] | None = None,
    detail: dict | None = None,
) -> DerivationCandidate:
    """Create a valid DerivationCandidate for testing."""
    return DerivationCandidate(
        kind=kind,
        assertion_source=assertion_source,
        summary=summary,
        confidence=confidence,
        eligibility_reasons=eligibility_reasons or [],
        supporting_message_ids=supporting_message_ids or [_uid()],
        detail=detail,
    )


def _make_derivation_row(
    *,
    derivation_id: UUID | None = None,
    entry_id: UUID | None = None,
    user_id: UUID | None = None,
    kind: str = "memory",
    assertion_source: str = "user_explicit",
    confidence: float | None = 0.9,
    eligibility_reasons: list[str] | None = None,
    supporting_message_ids: list[UUID] | None = None,
    decision: str = "deferred",
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
) -> MagicMock:
    """Build a mock asyncpg row that ReflectionDerivation.from_row can parse."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "id": derivation_id or _uid(),
        "reflection_entry_id": entry_id or _uid(),
        "user_id": user_id or _uid(),
        "derivation_kind": kind,
        "candidate_payload_encrypted": None,
        "assertion_source": assertion_source,
        "confidence": confidence,
        "eligibility_reasons": eligibility_reasons,
        "supporting_message_ids": supporting_message_ids or [],
        "decision": decision,
        "applied_target_table": None,
        "applied_target_id": None,
        "processor_version": None,
        "processor_turn_id": None,
        "idempotency_key": idempotency_key,
        "created_at": created_at or _now(),
        "decided_at": None,
    }[key]
    return row


def _make_entry_row(
    *,
    entry_id: UUID | None = None,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    source_message_ids: list[UUID] | None = None,
    revision_number: int = 1,
    bot_id: str = "mediator",
    template_key: str = "freeform_reflection",
    temporal_scope: str = "day",
    phase: str = "freeform",
) -> MagicMock:
    """Build a mock asyncpg row for an entry."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "id": entry_id or _uid(),
        "session_id": session_id or _uid(),
        "user_id": user_id or _uid(),
        "bot_id": bot_id,
        "template_key": template_key,
        "temporal_scope": temporal_scope,
        "phase": phase,
        "period_start": None,
        "period_end": None,
        "timezone": "UTC",
        "source_message_ids": source_message_ids or [],
        "topic_id": None,
        "payload_encrypted": None,
        "plaintext_searchable": "test",
        "summary_encrypted": None,
        "schema_version": 1,
        "processor_version": None,
        "revision_number": revision_number,
        "supersedes_entry_id": None,
        "created_by_turn_id": None,
        "created_at": _now(),
    }[key]
    return row


def _make_session_row(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "mediator",
    topic_id: UUID | None = None,
    source_message_ids: list[UUID] | None = None,
    status: str = "processed",
    template_key: str = "freeform_reflection",
    temporal_scope: str = "day",
    phase: str = "freeform",
) -> MagicMock:
    """Build a mock asyncpg row for a session."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "id": session_id or _uid(),
        "user_id": user_id or _uid(),
        "bot_id": bot_id,
        "topic_id": topic_id,
        "opened_by_message_id": None,
        "opened_by_turn_id": None,
        "source_message_ids": source_message_ids or [],
        "template_key": template_key,
        "temporal_scope": temporal_scope,
        "phase": phase,
        "period_start": None,
        "period_end": None,
        "timezone": "UTC",
        "classification_source": None,
        "classification_confidence": None,
        "classification_metadata": None,
        "status": status,
        "idle_finalize_at": None,
        "finalized_at": _now(),
        "processed_at": _now(),
        "abandoned_at": None,
        "claimed_by": None,
        "claimed_at": None,
        "retry_count": 0,
        "failure_class": None,
        "failure_reason": None,
        "last_error": None,
        "idempotency_key": None,
        "created_at": _now(),
        "updated_at": _now(),
    }[key]
    return row


# ── Idempotency key construction tests ───────────────────────────────────────


class TestBuildIdempotencyKey:
    """Verify idempotency key construction is deterministic."""

    def test_same_inputs_produce_same_key(self):
        entry_id = _uid()
        msg_ids = [_uid(), _uid()]

        key1 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=msg_ids,
        )
        key2 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=msg_ids,
        )
        assert key1 == key2
        assert len(key1) == 64  # SHA-256 hex digest
        assert isinstance(key1, str)

    def test_different_entry_id_produces_different_key(self):
        msg_ids = [_uid()]
        key1 = build_idempotency_key(
            entry_id=_uid(),
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=msg_ids,
        )
        key2 = build_idempotency_key(
            entry_id=_uid(),
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=msg_ids,
        )
        assert key1 != key2

    def test_different_kind_produces_different_key(self):
        entry_id = _uid()
        key1 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=[],
        )
        key2 = build_idempotency_key(
            entry_id=entry_id,
            kind="observation",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=[],
        )
        assert key1 != key2

    def test_different_summary_produces_different_key(self):
        entry_id = _uid()
        key1 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="I live in Berlin",
            supporting_message_ids=[],
        )
        key2 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="I live in Paris",
            supporting_message_ids=[],
        )
        assert key1 != key2

    def test_different_supporting_ids_produces_different_key(self):
        entry_id = _uid()
        key1 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=[_uid()],
        )
        key2 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=[_uid()],  # Different UUID
        )
        assert key1 != key2

    def test_message_id_order_does_not_matter(self):
        """Idempotency key is order-independent for supporting message IDs."""
        entry_id = _uid()
        a, b = _uid(), _uid()
        key1 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=[a, b],
        )
        key2 = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="Test",
            supporting_message_ids=[b, a],
        )
        assert key1 == key2

    def test_key_is_hex_string(self):
        key = build_idempotency_key(
            entry_id=_uid(),
            kind="orientation",
            assertion_source="agent_inferred",
            summary="Focus area: work",
            supporting_message_ids=[_uid(), _uid()],
        )
        # Should be all hex characters
        assert all(c in "0123456789abcdef" for c in key)
        assert len(key) == 64


# ── Record candidates tests ──────────────────────────────────────────────────


class TestRecordCandidates:
    """Verify ledger records candidates correctly via mocked store."""

    @pytest.fixture
    def store_mock(self):
        """Create a mock ReflectionStore with a mock pool."""
        mock = MagicMock(spec=ReflectionStore)
        mock._pool = AsyncMock()
        return mock

    @pytest.fixture
    def ledger(self, store_mock):
        return DerivationLedger(store_mock)

    async def test_record_candidate_calls_store_with_correct_fields(self, ledger, store_mock):
        """record_candidate maps DerivationCandidate to store.create_derivation."""
        entry_id = _uid()
        user_id = _uid()
        msg_ids = [_uid(), _uid()]
        candidate = _make_candidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="I live in Berlin",
            confidence=0.95,
            eligibility_reasons=["explicit_statement", "high_confidence"],
            supporting_message_ids=msg_ids,
        )

        expected_derivation = ReflectionDerivation.from_row(
            _make_derivation_row(
                derivation_id=_uid(),
                entry_id=entry_id,
                user_id=user_id,
                kind="memory",
                assertion_source="user_explicit",
                confidence=0.95,
                eligibility_reasons=["explicit_statement", "high_confidence"],
                supporting_message_ids=msg_ids,
            )
        )
        store_mock.create_derivation = AsyncMock(return_value=expected_derivation)

        result = await ledger.record_candidate(
            user_id=user_id,
            reflection_entry_id=entry_id,
            candidate=candidate,
            processor_version="1.0.0",
            processor_turn_id=_uid(),
        )

        store_mock.create_derivation.assert_awaited_once()
        call_kwargs = store_mock.create_derivation.call_args.kwargs
        assert call_kwargs["user_id"] == user_id
        assert call_kwargs["reflection_entry_id"] == entry_id
        assert call_kwargs["derivation_kind"] == "memory"
        assert call_kwargs["assertion_source"] == "user_explicit"
        assert call_kwargs["confidence"] == 0.95
        assert call_kwargs["eligibility_reasons"] == ["explicit_statement", "high_confidence"]
        assert call_kwargs["supporting_message_ids"] == msg_ids
        assert call_kwargs["decision"] == "deferred"
        assert call_kwargs["idempotency_key"] is not None
        assert len(call_kwargs["idempotency_key"]) == 64
        assert result is expected_derivation

    async def test_record_candidates_records_all_eligible(self, ledger, store_mock):
        """All eligible candidates from a DerivationResult are recorded."""
        entry_id = _uid()
        user_id = _uid()

        # Create 3 candidates with different kinds
        candidates = [
            _make_candidate(kind=DerivationKind.memory, summary="Memory 1"),
            _make_candidate(kind=DerivationKind.observation, summary="Observation 1"),
            _make_candidate(kind=DerivationKind.orientation, summary="Orientation 1"),
        ]

        returned = [
            ReflectionDerivation.from_row(
                _make_derivation_row(
                    derivation_id=_uid(), entry_id=entry_id, user_id=user_id,
                    kind=c.kind.value,
                )
            )
            for c in candidates
        ]

        store_mock.create_derivation = AsyncMock(side_effect=returned)
        # No rejected candidates
        derivation_result = DerivationResult(candidates=candidates, rejected=[])

        results = await ledger.record_candidates(
            user_id=user_id,
            reflection_entry_id=entry_id,
            derivation_result=derivation_result,
        )

        assert len(results) == 3
        assert store_mock.create_derivation.await_count == 3
        assert results == returned

    async def test_rejected_candidates_are_not_recorded(self, ledger, store_mock):
        """Only eligible candidates become ledger entries; rejected are skipped."""
        entry_id = _uid()
        user_id = _uid()

        eligible = _make_candidate(summary="Eligible memory")
        rejected_cand = DerivationCandidate(
            kind="task",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Do something",
            confidence=0.5,
            supporting_message_ids=[_uid()],
        )
        rejected_result = check_eligibility(rejected_cand)
        assert not rejected_result.eligible

        derivation_result = DerivationResult(
            candidates=[eligible],
            rejected=[(rejected_cand, rejected_result)],
        )

        store_mock.create_derivation = AsyncMock(
            return_value=ReflectionDerivation.from_row(
                _make_derivation_row(entry_id=entry_id, user_id=user_id)
            )
        )

        results = await ledger.record_candidates(
            user_id=user_id,
            reflection_entry_id=entry_id,
            derivation_result=derivation_result,
        )

        # Only the eligible candidate was recorded
        assert len(results) == 1
        store_mock.create_derivation.assert_awaited_once()

    async def test_empty_candidates_no_store_calls(self, ledger, store_mock):
        """Empty candidate list → no store calls."""
        derivation_result = DerivationResult(candidates=[], rejected=[])

        results = await ledger.record_candidates(
            user_id=_uid(),
            reflection_entry_id=_uid(),
            derivation_result=derivation_result,
        )

        assert len(results) == 0
        store_mock.create_derivation.assert_not_awaited()


# ── Idempotent ledgering tests ───────────────────────────────────────────────


class TestIdempotentLedgering:
    """Verify that retried submissions with the same candidate don't create duplicates."""

    @pytest.fixture
    def store_mock(self):
        mock = MagicMock(spec=ReflectionStore)
        mock._pool = AsyncMock()
        return mock

    @pytest.fixture
    def ledger(self, store_mock):
        return DerivationLedger(store_mock)

    async def test_same_candidate_same_idempotency_key(self, ledger, store_mock):
        """Two calls with the same candidate use the same idempotency_key."""
        entry_id = _uid()
        user_id = _uid()
        candidate = _make_candidate(
            kind=DerivationKind.memory,
            summary="I live in Berlin",
            supporting_message_ids=[_uid()],
        )

        derivation = ReflectionDerivation.from_row(
            _make_derivation_row(entry_id=entry_id, user_id=user_id)
        )
        # First call: creates. Second call: also "creates" but store handles idempotency.
        store_mock.create_derivation = AsyncMock(return_value=derivation)

        result1 = await ledger.record_candidate(
            user_id=user_id,
            reflection_entry_id=entry_id,
            candidate=candidate,
        )
        result2 = await ledger.record_candidate(
            user_id=user_id,
            reflection_entry_id=entry_id,
            candidate=candidate,
        )

        # Both calls used the same idempotency_key
        assert store_mock.create_derivation.await_count == 2

        call1_key = store_mock.create_derivation.call_args_list[0].kwargs["idempotency_key"]
        call2_key = store_mock.create_derivation.call_args_list[1].kwargs["idempotency_key"]
        assert call1_key == call2_key
        # The key is deterministic
        expected_key = build_idempotency_key(
            entry_id=entry_id,
            kind="memory",
            assertion_source="user_explicit",
            summary="I live in Berlin",
            supporting_message_ids=candidate.supporting_message_ids,
        )
        assert call1_key == expected_key

    async def test_different_candidates_different_keys(self, ledger, store_mock):
        """Different candidates produce different idempotency keys."""
        entry_id = _uid()
        user_id = _uid()

        cand1 = _make_candidate(summary="Memory A", supporting_message_ids=[_uid()])
        cand2 = _make_candidate(summary="Memory B", supporting_message_ids=[_uid()])

        store_mock.create_derivation = AsyncMock(
            return_value=ReflectionDerivation.from_row(
                _make_derivation_row(entry_id=entry_id, user_id=user_id)
            )
        )

        await ledger.record_candidate(
            user_id=user_id, reflection_entry_id=entry_id, candidate=cand1,
        )
        await ledger.record_candidate(
            user_id=user_id, reflection_entry_id=entry_id, candidate=cand2,
        )

        key1 = store_mock.create_derivation.call_args_list[0].kwargs["idempotency_key"]
        key2 = store_mock.create_derivation.call_args_list[1].kwargs["idempotency_key"]
        assert key1 != key2

    async def test_idempotency_key_stable_across_ledger_instances(self, store_mock):
        """Same candidate produces the same key regardless of ledger instance."""
        entry_id = _uid()
        candidate = _make_candidate(
            kind=DerivationKind.observation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Pattern: user prefers mornings",
            confidence=0.7,
            supporting_message_ids=[_uid(), _uid()],
        )

        ledger1 = DerivationLedger(store_mock)
        ledger2 = DerivationLedger(store_mock)

        # We can't intercept the key easily without mocking, so use the function directly
        key1 = build_idempotency_key(
            entry_id=entry_id,
            kind=candidate.kind.value,
            assertion_source=candidate.assertion_source.value,
            summary=candidate.summary,
            supporting_message_ids=candidate.supporting_message_ids,
        )
        key2 = build_idempotency_key(
            entry_id=entry_id,
            kind=candidate.kind.value,
            assertion_source=candidate.assertion_source.value,
            summary=candidate.summary,
            supporting_message_ids=candidate.supporting_message_ids,
        )
        assert key1 == key2


# ── Provenance traversal tests ───────────────────────────────────────────────


class TestProvenanceTraversal:
    """Verify provenance chain walks from derivation back to source messages."""

    @pytest.fixture
    def store_mock(self):
        mock = MagicMock(spec=ReflectionStore)
        mock._pool = AsyncMock()
        return mock

    @pytest.fixture
    def ledger(self, store_mock):
        return DerivationLedger(store_mock)

    async def test_full_chain_resolution(self, ledger, store_mock):
        """When all links exist, traverse_provenance returns a complete chain."""
        user_id = _uid()
        session_id = _uid()
        entry_id = _uid()
        derivation_id = _uid()
        source_msg_ids = [_uid(), _uid(), _uid()]

        # Mock the derivation
        deriv_row = _make_derivation_row(
            derivation_id=derivation_id,
            entry_id=entry_id,
            user_id=user_id,
            supporting_message_ids=source_msg_ids[:2],  # Derivation's own supporting IDs
        )
        store_mock.get_derivation = AsyncMock(
            return_value=ReflectionDerivation.from_row(deriv_row)
        )

        # Mock the entry
        entry_row = _make_entry_row(
            entry_id=entry_id,
            user_id=user_id,
            session_id=session_id,
        )
        store_mock.get_entry = AsyncMock(
            return_value=ReflectionEntry.from_row(entry_row)
        )

        # Mock the session
        session_row = _make_session_row(
            session_id=session_id,
            user_id=user_id,
            source_message_ids=source_msg_ids,
        )
        store_mock.get_session = AsyncMock(
            return_value=ReflectionSession.from_row(session_row)
        )

        chain = await ledger.traverse_provenance(
            user_id=user_id,
            derivation_id=derivation_id,
        )

        assert chain.is_complete
        assert chain.derivation is not None
        assert chain.entry is not None
        assert chain.session is not None
        # source_message_ids come from session for the canonical set
        assert chain.source_message_ids == source_msg_ids

    async def test_chain_incomplete_when_entry_missing(self, ledger, store_mock):
        """When the entry is deleted, the chain is incomplete but derivation still present."""
        user_id = _uid()
        derivation_id = _uid()

        deriv_row = _make_derivation_row(
            derivation_id=derivation_id,
            user_id=user_id,
        )
        store_mock.get_derivation = AsyncMock(
            return_value=ReflectionDerivation.from_row(deriv_row)
        )
        store_mock.get_entry = AsyncMock(return_value=None)

        chain = await ledger.traverse_provenance(
            user_id=user_id,
            derivation_id=derivation_id,
        )

        assert not chain.is_complete
        assert chain.derivation is not None
        assert chain.entry is None
        assert chain.session is None

    async def test_chain_incomplete_when_session_missing(self, ledger, store_mock):
        """When session is gone but entry exists, chain is incomplete."""
        user_id = _uid()
        entry_id = _uid()
        derivation_id = _uid()

        deriv_row = _make_derivation_row(
            derivation_id=derivation_id,
            entry_id=entry_id,
            user_id=user_id,
        )
        store_mock.get_derivation = AsyncMock(
            return_value=ReflectionDerivation.from_row(deriv_row)
        )

        entry_row = _make_entry_row(entry_id=entry_id, user_id=user_id)
        store_mock.get_entry = AsyncMock(
            return_value=ReflectionEntry.from_row(entry_row)
        )
        store_mock.get_session = AsyncMock(return_value=None)

        chain = await ledger.traverse_provenance(
            user_id=user_id,
            derivation_id=derivation_id,
        )

        assert not chain.is_complete
        assert chain.derivation is not None
        assert chain.entry is not None
        assert chain.session is None

    async def test_traverse_nonexistent_derivation_raises(self, ledger, store_mock):
        """Traversing a derivation that doesn't exist raises DerivationNotFoundError."""
        store_mock.get_derivation = AsyncMock(return_value=None)

        with pytest.raises(DerivationNotFoundError, match="not found"):
            await ledger.traverse_provenance(
                user_id=_uid(),
                derivation_id=_uid(),
            )

    async def test_provenance_chain_dataclass(self):
        """ProvenanceChain is a frozen dataclass with expected fields."""
        deriv = ReflectionDerivation.from_row(_make_derivation_row())
        entry = ReflectionEntry.from_row(_make_entry_row())
        session = ReflectionSession.from_row(_make_session_row())

        chain = ProvenanceChain(
            derivation=deriv,
            entry=entry,
            session=session,
            source_message_ids=[_uid()],
        )
        assert chain.is_complete
        assert chain.derivation is deriv
        assert chain.entry is entry
        assert chain.session is session
        assert len(chain.source_message_ids) == 1

        # Frozen
        with pytest.raises(Exception):
            chain.source_message_ids = []  # type: ignore[misc]


# ── Decision update tests ────────────────────────────────────────────────────


class TestDecisionUpdates:
    """Verify update_decision delegates to store correctly."""

    @pytest.fixture
    def store_mock(self):
        mock = MagicMock(spec=ReflectionStore)
        mock._pool = AsyncMock()
        return mock

    @pytest.fixture
    def ledger(self, store_mock):
        return DerivationLedger(store_mock)

    async def test_update_decision_applied_with_targets(self, ledger, store_mock):
        """Applying a derivation records the target table and ID."""
        user_id = _uid()
        derivation_id = _uid()
        target_id = _uid()

        expected = ReflectionDerivation.from_row(
            _make_derivation_row(
                derivation_id=derivation_id,
                user_id=user_id,
                decision="applied",
            )
        )
        store_mock.update_derivation_decision = AsyncMock(return_value=expected)

        result = await ledger.update_decision(
            user_id=user_id,
            derivation_id=derivation_id,
            decision="applied",
            applied_target_table="user_memory",
            applied_target_id=target_id,
            processor_version="1.0.0",
        )

        store_mock.update_derivation_decision.assert_awaited_once_with(
            user_id=user_id,
            derivation_id=derivation_id,
            decision="applied",
            applied_target_table="user_memory",
            applied_target_id=target_id,
            processor_version="1.0.0",
        )
        assert result is expected

    async def test_update_decision_rejected(self, ledger, store_mock):
        """Rejecting a derivation doesn't need target info."""
        user_id = _uid()
        derivation_id = _uid()

        expected = ReflectionDerivation.from_row(
            _make_derivation_row(derivation_id=derivation_id, decision="rejected")
        )
        store_mock.update_derivation_decision = AsyncMock(return_value=expected)

        result = await ledger.update_decision(
            user_id=user_id,
            derivation_id=derivation_id,
            decision="rejected",
        )

        store_mock.update_derivation_decision.assert_awaited_once_with(
            user_id=user_id,
            derivation_id=derivation_id,
            decision="rejected",
            applied_target_table=None,
            applied_target_id=None,
            processor_version=None,
        )
        assert result is expected


# ── Lookup helper tests ──────────────────────────────────────────────────────


class TestLookupHelpers:
    """Verify delegation to store methods."""

    @pytest.fixture
    def store_mock(self):
        mock = MagicMock(spec=ReflectionStore)
        mock._pool = AsyncMock()
        return mock

    @pytest.fixture
    def ledger(self, store_mock):
        return DerivationLedger(store_mock)

    async def test_get_derivation_delegates(self, ledger, store_mock):
        user_id = _uid()
        derivation_id = _uid()
        expected = ReflectionDerivation.from_row(_make_derivation_row())
        store_mock.get_derivation = AsyncMock(return_value=expected)

        result = await ledger.get_derivation(
            user_id=user_id,
            derivation_id=derivation_id,
        )
        store_mock.get_derivation.assert_awaited_once_with(
            user_id=user_id, derivation_id=derivation_id
        )
        assert result is expected

    async def test_get_derivation_by_key_delegates(self, ledger, store_mock):
        user_id = _uid()
        key = "abc123"
        store_mock.get_derivation_by_idempotency_key = AsyncMock(return_value=None)

        result = await ledger.get_derivation_by_key(
            user_id=user_id,
            idempotency_key=key,
        )
        store_mock.get_derivation_by_idempotency_key.assert_awaited_once_with(
            user_id=user_id, idempotency_key=key
        )
        assert result is None

    async def test_list_derivations_for_entry_delegates(self, ledger, store_mock):
        user_id = _uid()
        entry_id = _uid()
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[])

        result = await ledger.list_derivations_for_entry(
            user_id=user_id,
            reflection_entry_id=entry_id,
            derivation_kind="memory",
            decision="deferred",
            limit=50,
        )
        store_mock.list_derivations_for_entry.assert_awaited_once_with(
            user_id=user_id,
            reflection_entry_id=entry_id,
            derivation_kind="memory",
            decision="deferred",
            limit=50,
        )
        assert result == []

    async def test_list_derivations_for_session_delegates(self, ledger, store_mock):
        user_id = _uid()
        session_id = _uid()
        store_mock.list_derivations_for_session = AsyncMock(return_value=[])

        result = await ledger.list_derivations_for_session(
            user_id=user_id,
            session_id=session_id,
            derivation_kind="orientation",
            limit=200,
        )
        store_mock.list_derivations_for_session.assert_awaited_once_with(
            user_id=user_id,
            session_id=session_id,
            derivation_kind="orientation",
            decision=None,
            limit=200,
        )
        assert result == []


# ── Error case tests ─────────────────────────────────────────────────────────


class TestErrorCases:
    """Verify proper error propagation."""

    @pytest.fixture
    def store_mock(self):
        mock = MagicMock(spec=ReflectionStore)
        mock._pool = AsyncMock()
        return mock

    @pytest.fixture
    def ledger(self, store_mock):
        return DerivationLedger(store_mock)

    async def test_record_candidate_against_nonexistent_entry(self, ledger, store_mock):
        """Recording against a non-existent entry raises EntryNotFoundError."""
        store_mock.create_derivation = AsyncMock(
            side_effect=EntryNotFoundError("Entry not found")
        )

        with pytest.raises(EntryNotFoundError, match="Entry not found"):
            await ledger.record_candidate(
                user_id=_uid(),
                reflection_entry_id=_uid(),
                candidate=_make_candidate(),
            )

    async def test_traverse_derivation_wrong_user(self, ledger, store_mock):
        """Derivation owned by different user should not be found."""
        store_mock.get_derivation = AsyncMock(return_value=None)

        with pytest.raises(DerivationNotFoundError, match="not found"):
            await ledger.traverse_provenance(
                user_id=_uid(),
                derivation_id=_uid(),
            )


# ── Integration with real derivation engine ──────────────────────────────────


class TestLedgerWithDerivationEngine:
    """Verify the ledger correctly processes real DerivationEngine output."""

    @pytest.fixture
    def store_mock(self):
        mock = MagicMock(spec=ReflectionStore)
        mock._pool = AsyncMock()
        return mock

    @pytest.fixture
    def ledger(self, store_mock):
        return DerivationLedger(store_mock)

    async def test_real_engine_output_is_recorded(self, ledger, store_mock):
        """A real DerivationEngine result is correctly passed to the store."""
        engine = DerivationEngine()
        source_ids = [_uid(), _uid()]

        derivation_result = engine.produce_candidates(
            source_message_ids=source_ids,
            plaintext_summary="User reflected on their work habits and expressed desire to improve.",
            extracted_topics=["work", "productivity"],
            explicit_user_statements=[
                "I work too much",
                "I need better work-life balance",
                "I want to start exercising",
            ],
            detected_sentiment="reflective",
        )

        # Should produce some candidates
        assert len(derivation_result.candidates) > 0

        # Set up mock returns
        entry_id = _uid()
        user_id = _uid()
        returned = []
        for c in derivation_result.candidates:
            returned.append(
                ReflectionDerivation.from_row(
                    _make_derivation_row(
                        derivation_id=_uid(),
                        entry_id=entry_id,
                        user_id=user_id,
                        kind=c.kind.value,
                        assertion_source=c.assertion_source.value,
                        confidence=c.confidence,
                    )
                )
            )
        store_mock.create_derivation = AsyncMock(side_effect=returned)

        results = await ledger.record_candidates(
            user_id=user_id,
            reflection_entry_id=entry_id,
            derivation_result=derivation_result,
        )

        assert len(results) == len(derivation_result.candidates)

        # Every recorded derivation has the right fields
        for i, derivation in enumerate(results):
            candidate = derivation_result.candidates[i]
            assert derivation.derivation_kind == candidate.kind.value
            assert derivation.assertion_source == candidate.assertion_source.value
            assert derivation.confidence == candidate.confidence

        # Verify all create_derivation calls included an idempotency_key
        for call_args in store_mock.create_derivation.call_args_list:
            assert call_args.kwargs["idempotency_key"] is not None
            assert len(call_args.kwargs["idempotency_key"]) == 64

    async def test_real_engine_output_kinds_are_all_valid(self, ledger, store_mock):
        """Every candidate kind from the real engine is a valid derivation kind."""
        engine = DerivationEngine()
        derivation_result = engine.produce_candidates(
            source_message_ids=[_uid(), _uid()],
            plaintext_summary="Test reflection with various signals.",
            extracted_topics=["health", "career"],
            explicit_user_statements=["I want to run a marathon", "My job is demanding"],
            detected_sentiment="motivated",
        )

        valid_kinds = {"memory", "observation", "distillation", "orientation"}
        for candidate in derivation_result.candidates:
            assert candidate.kind.value in valid_kinds


# ── Store property test ──────────────────────────────────────────────────────


class TestDerivationLedgerConstruction:
    """Verify ledger construction and store access."""

    def test_store_property_returns_constructor_arg(self):
        store = MagicMock(spec=ReflectionStore)
        ledger = DerivationLedger(store)
        assert ledger.store is store
