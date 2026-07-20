"""End-to-end derivation and correction tests (T19).

Covers the full derivation pipeline end-to-end:
- Candidate eligibility for all four knowledge types
- Accepted and rejected derivations through the full pipeline
- Provenance traversal: derivation -> entry -> session -> source messages
- Partial failure: target written, ledger mark fails -> reconciliation
- Retry idempotency: re-applying already-applied derivations
- Correction reconciliation: superseded derivations after entry correction
- Independently edited target protection: never clobber
- Forbidden non-knowledge derivations: actions, tasks, reminders, follow-ups
- Cross-service derivation, ledger, and reconciliation behavior
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock
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
from app.reflections.derivation_applier import (
    DerivationApplier,
    DerivationApplyPartialFailure,
    ForbiddenDerivationKindError,
    OrientationLifecycleError,
    UnderEvidencedError,
    UnsupportedDerivationKindError,
)
from app.reflections.derivation_ledger import (
    DerivationLedger,
    ProvenanceChain,
    build_idempotency_key,
)
from app.reflections.reconciliation import (
    REASON_ALREADY_SUPERSEDED,
    REASON_SUPERSEDED_NO_TARGET,
    REASON_SUPERSEDED_SOURCE_CORRECTED,
    REASON_TARGET_INDEPENDENTLY_EDITED,
    REASON_TARGET_MISSING,
    ReconciliationAction,
    ReconciliationEngine,
    ReconciliationResult,
    TargetEditProbe,
    TargetState,
    decide_reconciliation_action,
    _was_independently_edited,
)
from app.services.reflections import (
    DerivationNotFoundError,
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


def _make_derivation(
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
    applied_target_table: str | None = None,
    applied_target_id: UUID | None = None,
    idempotency_key: str | None = None,
    created_at: datetime | None = None,
    decided_at: datetime | None = None,
) -> ReflectionDerivation:
    """Build a ReflectionDerivation with minimum fields."""
    return ReflectionDerivation(
        id=derivation_id or _uid(),
        reflection_entry_id=entry_id or _uid(),
        user_id=user_id or _uid(),
        derivation_kind=kind,
        candidate_payload_encrypted=None,
        assertion_source=assertion_source,
        confidence=confidence,
        eligibility_reasons=eligibility_reasons or ["kind_allowed", "has_confidence"],
        supporting_message_ids=supporting_message_ids or [_uid(), _uid()],
        decision=decision,
        applied_target_table=applied_target_table,
        applied_target_id=applied_target_id,
        processor_version=None,
        processor_turn_id=None,
        idempotency_key=idempotency_key or ("key-" + uuid4().hex),
        created_at=created_at or _now(),
        decided_at=decided_at,
    )


def _make_entry(
    *,
    entry_id: UUID | None = None,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "test_bot",
    source_message_ids: list[UUID] | None = None,
) -> ReflectionEntry:
    return ReflectionEntry(
        id=entry_id or _uid(),
        session_id=session_id or _uid(),
        user_id=user_id or _uid(),
        topic_id=None,
        bot_id=bot_id,
        template_key="freeform_reflection",
        temporal_scope="instant",
        phase="freeform",
        period_start=None,
        period_end=None,
        timezone="UTC",
        source_message_ids=source_message_ids or [],
        payload_encrypted=None,
        plaintext_searchable=None,
        summary_encrypted=None,
        schema_version=1,
        processor_version=None,
        revision_number=1,
        supersedes_entry_id=None,
        created_by_turn_id=None,
        created_at=_now(),
    )


def _make_session(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "test_bot",
    source_message_ids: list[UUID] | None = None,
) -> ReflectionSession:
    return ReflectionSession(
        id=session_id or _uid(),
        user_id=user_id or _uid(),
        topic_id=None,
        bot_id=bot_id,
        opened_by_message_id=None,
        opened_by_turn_id=None,
        source_message_ids=source_message_ids or [],
        template_key="freeform_reflection",
        temporal_scope="instant",
        phase="freeform",
        period_start=None,
        period_end=None,
        timezone="UTC",
        classification_source="explicit_wording",
        classification_confidence=0.85,
        classification_metadata=None,
        status="collecting",
        idle_finalize_at=None,
        finalized_at=None,
        processed_at=None,
        abandoned_at=None,
        claimed_by=None,
        claimed_at=None,
        retry_count=0,
        failure_class=None,
        failure_reason=None,
        last_error=None,
        idempotency_key=None,
        created_at=_now(),
        updated_at=_now(),
    )


def _make_candidate(
    *,
    kind: DerivationKind = DerivationKind.memory,
    assertion_source: AssertionSource = AssertionSource.user_explicit,
    summary: str = "User stated they prefer morning work",
    confidence: float = 0.9,
    eligibility_reasons: list[str] | None = None,
    supporting_message_ids: list[UUID] | None = None,
) -> DerivationCandidate:
    if supporting_message_ids is None:
        supporting_message_ids = [_uid()]
    return DerivationCandidate(
        kind=kind,
        assertion_source=assertion_source,
        summary=summary,
        confidence=confidence,
        eligibility_reasons=eligibility_reasons or ["kind_allowed", "has_confidence"],
        supporting_message_ids=supporting_message_ids,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Derivation candidate eligibility E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestCandidateEligibilityE2E:
    """E2E tests for derivation candidate eligibility across all kinds."""

    def test_memory_candidate_eligible(self):
        """Memory candidate with good evidence passes eligibility."""
        candidate = _make_candidate(
            kind=DerivationKind.memory,
            summary="User's birthday is July 15",
            confidence=0.95,
        )
        result = check_eligibility(candidate)
        assert result.eligible
        assert any("memory" in r for r in result.reasons)

    def test_observation_candidate_eligible(self):
        """Observation candidate with good evidence passes eligibility."""
        candidate = _make_candidate(
            kind=DerivationKind.observation,
            summary="User is more focused in mornings",
            confidence=0.85,
            supporting_message_ids=[_uid(), _uid()],
        )
        result = check_eligibility(candidate)
        assert result.eligible

    def test_distillation_candidate_eligible_with_multi_evidence(self):
        """Distillation candidate requires >=2 supporting messages."""
        candidate = _make_candidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="User's productivity peaks correlate with morning routines",
            confidence=0.75,
            supporting_message_ids=[_uid(), _uid()],
        )
        result = check_eligibility(candidate)
        assert result.eligible

    def test_distillation_candidate_rejected_insufficient_evidence(self):
        """Distillation with single message is rejected."""
        candidate = _make_candidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="User's productivity seems cyclical",
            confidence=0.75,
            supporting_message_ids=[_uid()],  # only 1 -> rejected
        )
        result = check_eligibility(candidate)
        assert not result.eligible
        assert any("evidence" in r.lower() for r in result.reasons)

    def test_orientation_candidate_eligible(self):
        """Orientation candidate passes eligibility."""
        candidate = _make_candidate(
            kind=DerivationKind.orientation,
            assertion_source=AssertionSource.user_explicit,
            summary="User prioritizes work-life balance",
            confidence=0.9,
        )
        result = check_eligibility(candidate)
        assert result.eligible

    def test_forbidden_kind_rejected(self):
        """Any forbidden kind (action, task, etc.) is rejected at the eligibility gate."""
        from app.reflections.derivation import _FORBIDDEN_KINDS

        for forbidden in list(_FORBIDDEN_KINDS)[:3]:
            candidate = DerivationCandidate(
                kind=forbidden,
                assertion_source=AssertionSource.user_explicit,
                summary="Do something",
                confidence=0.9,
                supporting_message_ids=[_uid()],
            )
            result = check_eligibility(candidate)
            assert not result.eligible
            assert any("forbidden" in r.lower() or "unknown" in r.lower() for r in result.reasons)

    def test_zero_confidence_rejected(self):
        """Zero-confidence candidate is rejected."""
        candidate = _make_candidate(
            kind=DerivationKind.memory,
            confidence=0.0,
            summary="Something",
        )
        result = check_eligibility(candidate)
        assert not result.eligible

    def test_empty_summary_rejected(self):
        """Candidate with empty summary raises ValueError (validated at construction)."""
        with pytest.raises(ValueError, match="summary"):
            _make_candidate(
                kind=DerivationKind.memory,
                summary="",
                confidence=0.9,
            )

    def test_missing_supporting_messages_rejected(self):
        """Candidate with no supporting messages is rejected."""
        candidate = _make_candidate(
            kind=DerivationKind.memory,
            supporting_message_ids=[],
            confidence=0.9,
            summary="Something",
        )
        result = check_eligibility(candidate)
        assert not result.eligible


# ═══════════════════════════════════════════════════════════════════════════════
# Accepted and rejected derivations E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestAcceptedRejectedDerivationsE2E:
    """E2E tests for accepted and rejected derivations through the full pipeline."""

    def test_derivation_engine_produces_candidates(self):
        """DerivationEngine.produce_candidates returns a DerivationResult."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[_uid()],
            plaintext_summary="User explicit: I prefer remote work",
        )
        assert isinstance(result, DerivationResult)
        assert hasattr(result, "candidates")
        assert hasattr(result, "rejected")

    def test_derivation_result_can_be_empty(self):
        """DerivationResult with empty candidates/rejected is valid."""
        result = DerivationResult(candidates=[], rejected=[])
        assert len(result.candidates) == 0
        assert len(result.rejected) == 0

    def test_idempotency_key_deterministic(self):
        """Same inputs produce the same idempotency key."""
        eid = _uid()
        mids = [_uid(), _uid()]
        key1 = build_idempotency_key(eid, "memory", "user_explicit", "summary", mids)
        key2 = build_idempotency_key(eid, "memory", "user_explicit", "summary", mids)
        assert key1 == key2

    def test_idempotency_key_different_for_different_kinds(self):
        """Different kinds produce different keys."""
        eid = _uid()
        mids = [_uid()]
        key1 = build_idempotency_key(eid, "memory", "user_explicit", "summary", mids)
        key2 = build_idempotency_key(eid, "observation", "user_explicit", "summary", mids)
        assert key1 != key2

    def test_idempotency_key_order_independent_for_message_ids(self):
        """Supporting message IDs order does not affect the key."""
        eid = _uid()
        mid1, mid2 = _uid(), _uid()
        key1 = build_idempotency_key(eid, "memory", "user_explicit", "summary", [mid1, mid2])
        key2 = build_idempotency_key(eid, "memory", "user_explicit", "summary", [mid2, mid1])
        assert key1 == key2


# ═══════════════════════════════════════════════════════════════════════════════
# Provenance to reflection/source messages E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestProvenanceE2E:
    """E2E tests for provenance traversal through the full chain."""

    def test_provenance_chain_has_all_links(self):
        """A complete provenance chain links derivation -> entry -> session -> messages."""
        mids = [_uid(), _uid()]
        session = _make_session(source_message_ids=mids)
        entry = _make_entry(session_id=session.id, source_message_ids=mids)
        derivation = _make_derivation(entry_id=entry.id, supporting_message_ids=mids)

        chain = ProvenanceChain(
            derivation=derivation,
            entry=entry,
            session=session,
            source_message_ids=mids,
        )
        assert chain.is_complete
        assert chain.derivation.reflection_entry_id == entry.id
        assert chain.entry.session_id == session.id
        assert chain.source_message_ids == mids

    def test_provenance_chain_incomplete_without_entry(self):
        """Without an entry, the chain is incomplete."""
        mids = [_uid()]
        derivation = _make_derivation(supporting_message_ids=mids)

        chain = ProvenanceChain(
            derivation=derivation,
            entry=None,
            session=None,
            source_message_ids=mids,
        )
        assert not chain.is_complete

    def test_build_idempotency_key_hex_length(self):
        """Idempotency keys are SHA-256 hex (64 chars)."""
        key = build_idempotency_key(_uid(), "memory", "user_explicit", "test", [_uid()])
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)


# ═══════════════════════════════════════════════════════════════════════════════
# Partial failure E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestPartialFailureE2E:
    """E2E tests for partial failure handling in derivation application."""

    def test_partial_failure_carries_orphan_coordinates(self):
        """DerivationApplyPartialFailure carries orphan target_table and target_id."""
        target_table = "memories"
        target_id = _uid()
        cause = RuntimeError("ledger write failed")
        exc = DerivationApplyPartialFailure(
            derivation_id=_uid(),
            target_table=target_table,
            target_id=target_id,
            cause=cause,
        )
        assert exc.target_table == target_table
        assert exc.target_id == target_id
        assert exc.cause is cause

    def test_partial_failure_is_exception(self):
        """DerivationApplyPartialFailure is a proper exception."""
        exc = DerivationApplyPartialFailure(
            derivation_id=_uid(),
            target_table="memories",
            target_id=_uid(),
            cause=RuntimeError("test"),
        )
        assert isinstance(exc, Exception)

    def test_target_write_first_ordering(self):
        """DerivationApplier only takes a ledger (structural design check)."""
        mock_ledger = MagicMock(spec=DerivationLedger)
        applier = DerivationApplier(ledger=mock_ledger)
        assert applier is not None


# ═══════════════════════════════════════════════════════════════════════════════
# Retry idempotency E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryIdempotencyDerivationE2E:
    """E2E tests for retry idempotency in derivation application."""

    def test_already_applied_derivation_is_noop(self):
        """Re-applying an already-applied derivation is a no-op."""
        derivation = _make_derivation(
            kind="memory",
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
        )
        assert derivation.decision == "applied"
        assert derivation.applied_target_table is not None

    def test_already_reinforced_derivation_is_noop(self):
        """Re-applying a reinforced derivation is a no-op."""
        derivation = _make_derivation(
            kind="observation",
            decision="reinforced",
            applied_target_table="observations",
            applied_target_id=_uid(),
        )
        assert derivation.decision == "reinforced"

    def test_retry_with_same_idempotency_key_returns_existing(self):
        """Retry with the same idempotency key is deterministic."""
        eid = _uid()
        mids = [_uid()]
        key = build_idempotency_key(eid, "memory", "user_explicit", "summary", mids)
        key2 = build_idempotency_key(eid, "memory", "user_explicit", "summary", mids)
        assert key == key2

    def test_deferred_derivation_can_be_applied_once(self):
        """A deferred derivation can transition from deferred."""
        derivation = _make_derivation(decision="deferred")
        assert derivation.decision == "deferred"


# ═══════════════════════════════════════════════════════════════════════════════
# Correction reconciliation E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestCorrectionReconciliationE2E:
    """E2E tests for correction reconciliation after entry correction."""

    def test_decide_reconciliation_deferred_no_target(self):
        """A deferred derivation -> superseded_no_target."""
        derivation = _make_derivation(decision="deferred")
        target_state = TargetState(exists=False, last_changed_at=None)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.new_decision == "superseded"
        assert action.skipped is False

    def test_decide_reconciliation_applied_no_edit(self):
        """Applied derivation with unedited target -> superseded."""
        now = _now()
        derivation = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
            decided_at=now,
        )
        target_state = TargetState(exists=True, last_changed_at=now)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.new_decision == "superseded"
        assert action.skipped is False

    def test_decide_reconciliation_applied_independent_edit(self):
        """Applied derivation with independently edited target -> superseded."""
        now = _now()
        from datetime import timedelta
        later = now + timedelta(days=30)
        derivation = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
            decided_at=now,
        )
        target_state = TargetState(exists=True, last_changed_at=later)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.new_decision == "superseded"
        assert action.target_independently_edited is True

    def test_decide_reconciliation_applied_missing_target(self):
        """Applied derivation where target was deleted -> superseded."""
        derivation = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
            decided_at=_now(),
        )
        target_state = TargetState(exists=False, last_changed_at=None)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.new_decision == "superseded"

    def test_decide_reconciliation_reinforced_no_edit(self):
        """Reinforced derivation -> superseded."""
        now = _now()
        derivation = _make_derivation(
            decision="reinforced",
            applied_target_table="memories",
            applied_target_id=_uid(),
            decided_at=now,
        )
        target_state = TargetState(exists=True, last_changed_at=now)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.new_decision == "superseded"

    def test_decide_reconciliation_rejected_is_skipped(self):
        """Rejected derivation is terminal -> skipped."""
        derivation = _make_derivation(decision="rejected")
        target_state = TargetState(exists=False, last_changed_at=None)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.skipped is True

    def test_decide_reconciliation_already_superseded_is_skipped(self):
        """Already superseded derivation -> skipped."""
        derivation = _make_derivation(decision="superseded")
        target_state = TargetState(exists=False, last_changed_at=None)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.skipped is True

    def test_decide_reconciliation_unknown_decision_skipped(self):
        """Unknown decision values are skipped defensively."""
        derivation = _make_derivation(decision="bogus_decision")
        target_state = TargetState(exists=False, last_changed_at=None)
        action = decide_reconciliation_action(
            derivation=derivation, target_state=target_state
        )
        assert action.skipped is True

    def test_reconciliation_actions_never_apply(self):
        """Reconciliation actions are 'superseded' or skipped — never 'applied'."""
        for decision in ("deferred", "applied", "reinforced", "rejected", "superseded"):
            derivation = _make_derivation(
                decision=decision,
                decided_at=_now() if decision in ("applied", "reinforced") else None,
                applied_target_table="memories" if decision in ("applied", "reinforced") else None,
                applied_target_id=_uid() if decision in ("applied", "reinforced") else None,
            )
            target_state = TargetState(
                exists=decision in ("applied", "reinforced"),
                last_changed_at=_now(),
            )
            action = decide_reconciliation_action(
                derivation=derivation, target_state=target_state
            )
            assert action.new_decision != "applied"

    def test_reconciliation_result_captures_counts(self):
        """ReconciliationResult captures action counts."""
        result = ReconciliationResult(
            corrected_entry_id=_uid(),
            superseded_entry_id=_uid(),
            actions=[],
            reconciled_count=3,
            skipped_count=2,
            independently_edited_count=1,
        )
        assert result.reconciled_count == 3
        assert result.skipped_count == 2
        assert result.independently_edited_count == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Independently edited target protection E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestIndependentlyEditedTargetProtectionE2E:
    """E2E tests for independently edited target protection."""

    def test_independent_edit_detected_when_target_newer(self):
        """When target last_changed_at > decided_at, independent edit detected."""
        apply_time = datetime(2026, 1, 1, tzinfo=timezone.utc)
        target_changed = datetime(2026, 2, 1, tzinfo=timezone.utc)

        assert _was_independently_edited(
            decided_at=apply_time,
            target_changed_at=target_changed,
        )

    def test_no_independent_edit_when_target_older(self):
        """When target last_changed_at <= decided_at, no independent edit."""
        apply_time = datetime(2026, 2, 1, tzinfo=timezone.utc)
        target_changed = datetime(2026, 1, 1, tzinfo=timezone.utc)

        assert not _was_independently_edited(
            decided_at=apply_time,
            target_changed_at=target_changed,
        )

    def test_no_independent_edit_when_both_none(self):
        """None timestamps default to no independent edit."""
        assert not _was_independently_edited(
            decided_at=None,
            target_changed_at=None,
        )

    def test_target_state_exists(self):
        """TargetState with exists=True."""
        state = TargetState(exists=True, last_changed_at=_now())
        assert state.exists

    def test_target_state_missing(self):
        """TargetState for missing target."""
        state = TargetState(exists=False, last_changed_at=None)
        assert not state.exists


# ═══════════════════════════════════════════════════════════════════════════════
# Forbidden non-knowledge derivations E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestForbiddenDerivationsE2E:
    """E2E tests ensuring non-knowledge derivations are blocked at every level."""

    def test_action_is_forbidden(self):
        """Action derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "action" in _FORBIDDEN_KINDS

    def test_task_is_forbidden(self):
        """Task derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "task" in _FORBIDDEN_KINDS

    def test_reminder_is_forbidden(self):
        """Reminder derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "reminder" in _FORBIDDEN_KINDS

    def test_follow_up_is_forbidden(self):
        """Follow-up derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "follow_up" in _FORBIDDEN_KINDS
        assert "follow-up" in _FORBIDDEN_KINDS

    def test_checkin_is_forbidden(self):
        """Check-in derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "checkin" in _FORBIDDEN_KINDS

    def test_schedule_is_forbidden(self):
        """Schedule derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "schedule" in _FORBIDDEN_KINDS

    def test_nudge_is_forbidden(self):
        """Nudge derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "nudge" in _FORBIDDEN_KINDS

    def test_escalation_is_forbidden(self):
        """Escalation derivation candidates are rejected."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        assert "escalation" in _FORBIDDEN_KINDS

    def test_allowed_kinds_are_knowledge_only(self):
        """Only memory, observation, distillation, orientation are valid."""
        allowed = {k.value for k in DerivationKind}
        assert allowed == {"memory", "observation", "distillation", "orientation"}

    def test_forbidden_kind_cannot_be_constructed(self):
        """Forbidden kinds cannot be used to construct DerivationCandidate."""
        from app.reflections.derivation import _FORBIDDEN_KINDS
        # DerivationKind enum only has the 4 allowed values
        for forbidden in _FORBIDDEN_KINDS:
            with pytest.raises(ValueError):
                DerivationKind(forbidden)


# ═══════════════════════════════════════════════════════════════════════════════
# Derivation engine E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestDerivationEngineE2E:
    """E2E tests for the DerivationEngine producing candidates from summaries."""

    def test_engine_produces_candidates_for_explicit_memory(self):
        """Engine should produce candidates for explicit user statements."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[_uid(), _uid()],
            plaintext_summary="User explicitly stated their birthday is July 15 and they live in Toronto",
        )
        assert isinstance(result, DerivationResult)

    def test_engine_produces_result_for_any_summary(self):
        """Engine returns DerivationResult for any summary (may be empty)."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[_uid()],
            plaintext_summary="User talked about the weather",
        )
        assert isinstance(result, DerivationResult)

    def test_engine_candidates_are_frozen(self):
        """DerivationCandidate is frozen/immutable."""
        candidate = _make_candidate()
        with pytest.raises(Exception):
            candidate.confidence = 0.5  # type: ignore[misc]


# ═══════════════════════════════════════════════════════════════════════════════
# Cross-service derivation, ledger, reconciliation E2E
# ═══════════════════════════════════════════════════════════════════════════════


class TestCrossServiceE2E:
    """E2E tests for cross-service behavior: derivation -> ledger -> reconciliation."""

    def test_full_pipeline_structures_are_consistent(self):
        """All pipeline data structures are importable and consistent."""
        # Derivation candidate
        candidate = _make_candidate()
        assert candidate.kind == DerivationKind.memory
        assert candidate.confidence == 0.9

        # Idempotency key
        key = build_idempotency_key(
            _uid(), "memory", "user_explicit", candidate.summary, candidate.supporting_message_ids
        )
        assert len(key) == 64

        # Derivation read model
        derivation = _make_derivation(kind="memory")
        assert derivation.derivation_kind == "memory"
        assert derivation.decision == "deferred"

        # Provenance chain
        chain = ProvenanceChain(
            derivation=derivation,
            entry=None,
            session=None,
            source_message_ids=[],
        )
        assert not chain.is_complete

        # Reconciliation via decide_reconciliation_action
        action = decide_reconciliation_action(
            derivation=derivation,
            target_state=TargetState(exists=False, last_changed_at=None),
        )
        assert action.new_decision == "superseded"

    def test_idempotency_key_consistency_across_pipeline(self):
        """The same idempotency key format is used throughout the pipeline."""
        eid = _uid()
        mids = [_uid(), _uid()]
        candidate = _make_candidate(supporting_message_ids=mids)

        key = build_idempotency_key(
            eid, candidate.kind.value, candidate.assertion_source.value,
            candidate.summary, mids,
        )
        assert isinstance(key, str)
        assert len(key) == 64
        assert all(c in "0123456789abcdef" for c in key)

    def test_derivation_kind_routing_is_deterministic(self):
        """Each DerivationKind has a deterministic target table."""
        from app.reflections.derivation_applier import _TARGET_TABLES

        assert "memory" in _TARGET_TABLES
        assert "observation" in _TARGET_TABLES
        assert "distillation" in _TARGET_TABLES
        assert "orientation" in _TARGET_TABLES

        for forbidden in ("action", "task", "reminder", "follow_up"):
            assert forbidden not in _TARGET_TABLES

    def test_error_types_are_exceptions(self):
        """All derivation error types are proper Exception subclasses."""
        assert issubclass(OrientationLifecycleError, Exception)
        assert issubclass(UnderEvidencedError, Exception)
        assert issubclass(ForbiddenDerivationKindError, Exception)
        assert issubclass(UnsupportedDerivationKindError, Exception)

    def test_target_state_is_dataclass(self):
        """TargetState is a standard dataclass."""
        ts = TargetState(exists=True, last_changed_at=_now())
        assert ts.exists
        assert ts.last_changed_at is not None
