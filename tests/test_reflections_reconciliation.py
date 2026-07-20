"""Tests for app/reflections/reconciliation.py (T16).

Covers:
- Pure decision function (decide_reconciliation_action) for all decision states.
- Independent edit detection via timestamp comparison.
- Missing target / no-probe defense.
- Idempotent re-entry: repeated reconciliation skips already-superseded derivations.
- Engine-level reconciliation with mock ledger and probe.
- Structural guarantee: no target writes under any path.
- Provenance traversal after reconciliation.
- Best-effort failure: probe exceptions do not abort reconciliation.
- ReconciliationAction / ReconciliationResult / TargetState model integrity.
- Correction-service integration (best-effort _reconcile_after_correction).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.reflections.derivation_ledger import DerivationLedger
from app.reflections.reconciliation import (
    REASON_ALREADY_REJECTED,
    REASON_ALREADY_SUPERSEDED,
    REASON_NO_DERIVATIONS,
    REASON_SUPERSEDED_NO_TARGET,
    REASON_SUPERSEDED_SOURCE_CORRECTED,
    REASON_TARGET_INDEPENDENTLY_EDITED,
    REASON_TARGET_MISSING,
    REASON_UNKNOWN_DECISION,
    PoolingTargetEditProbe,
    ReconciliationAction,
    ReconciliationEngine,
    ReconciliationResult,
    TargetEditProbe,
    TargetState,
    _as_utc,
    _was_independently_edited,
    decide_reconciliation_action,
)
from app.services.reflections import (
    DerivationNotFoundError,
    ReflectionDerivation,
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
    processor_version: str | None = None,
    processor_turn_id: UUID | None = None,
) -> ReflectionDerivation:
    """Create a ReflectionDerivation for testing."""
    return ReflectionDerivation(
        id=derivation_id or _uid(),
        reflection_entry_id=entry_id or _uid(),
        user_id=user_id or _uid(),
        derivation_kind=kind,
        candidate_payload_encrypted=None,
        assertion_source=assertion_source,
        confidence=confidence,
        eligibility_reasons=eligibility_reasons or [],
        supporting_message_ids=supporting_message_ids or [_uid()],
        decision=decision,
        applied_target_table=applied_target_table,
        applied_target_id=applied_target_id,
        processor_version=processor_version,
        processor_turn_id=processor_turn_id,
        idempotency_key=idempotency_key,
        created_at=created_at or _now(),
        decided_at=decided_at,
    )


def _dt(hour: int = 0, minute: int = 0, tz: timezone | None = None) -> datetime:
    """Create a datetime with a fixed date and specified hour."""
    tz = tz or timezone.utc
    return datetime(2026, 7, 20, hour, minute, 0, 0, tzinfo=tz)


# ── _as_utc tests ───────────────────────────────────────────────────────────


class TestAsUtc:
    """UTC normalization for safe timestamp comparison."""

    def test_naive_gets_utc_tagged(self) -> None:
        naive = datetime(2026, 7, 20, 10, 0, 0)
        result = _as_utc(naive)
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc
        assert result.hour == 10  # naive is "as if" UTC

    def test_aware_converts_to_utc(self) -> None:
        import zoneinfo
        est = zoneinfo.ZoneInfo("America/New_York")
        aware = datetime(2026, 7, 20, 10, 0, 0, tzinfo=est)  # 10am EST = 2pm UTC
        result = _as_utc(aware)
        assert result.tzinfo == timezone.utc
        assert result.hour == 14  # converted

    def test_already_utc_unchanged(self) -> None:
        utc = datetime(2026, 7, 20, 10, 0, 0, tzinfo=timezone.utc)
        result = _as_utc(utc)
        assert result == utc


# ── _was_independently_edited tests ─────────────────────────────────────────


class TestWasIndependentlyEdited:
    """Independent edit detection via timestamp comparison."""

    def test_not_edited_when_target_changed_before_decided(self) -> None:
        decided = _dt(10, 0)
        changed = _dt(9, 59)  # before
        assert _was_independently_edited(decided_at=decided, target_changed_at=changed) is False

    def test_not_edited_when_same_instant(self) -> None:
        instant = _dt(10, 0)
        assert _was_independently_edited(decided_at=instant, target_changed_at=instant) is False

    def test_edited_when_target_changed_strictly_after(self) -> None:
        decided = _dt(10, 0)
        changed = _dt(10, 1)  # 1 minute after
        assert _was_independently_edited(decided_at=decided, target_changed_at=changed) is True

    def test_none_decided_at_is_not_edited(self) -> None:
        assert _was_independently_edited(decided_at=None, target_changed_at=_dt(10, 0)) is False

    def test_none_changed_at_is_not_edited(self) -> None:
        assert _was_independently_edited(decided_at=_dt(10, 0), target_changed_at=None) is False

    def test_both_none_is_not_edited(self) -> None:
        assert _was_independently_edited(decided_at=None, target_changed_at=None) is False

    def test_edited_across_days(self) -> None:
        decided = datetime(2026, 7, 19, 22, 0, 0, tzinfo=timezone.utc)
        changed = datetime(2026, 7, 20, 8, 0, 0, tzinfo=timezone.utc)
        assert _was_independently_edited(decided_at=decided, target_changed_at=changed) is True

    def test_naive_vs_aware_handled(self) -> None:
        """Naive decided_at (stored without tz) vs aware changed_at."""
        decided = datetime(2026, 7, 20, 10, 0, 0)  # naive
        changed = datetime(2026, 7, 20, 10, 1, 0, tzinfo=timezone.utc)  # aware
        # Both normalize to UTC; naive is treated as UTC.
        assert _was_independently_edited(decided_at=decided, target_changed_at=changed) is True


# ── decide_reconciliation_action tests ──────────────────────────────────────


class TestDecideReconciliationAction:
    """Pure decision function for settling one derivation."""

    # ── Already terminal: superseded ──────────────────────────────────────

    def test_already_superseded_is_skipped(self) -> None:
        deriv = _make_derivation(decision="superseded")
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.skipped is True
        assert action.new_decision is None
        assert action.reason == REASON_ALREADY_SUPERSEDED
        assert action.target_independently_edited is False

    # ── Already terminal: rejected ────────────────────────────────────────

    def test_already_rejected_is_skipped(self) -> None:
        deriv = _make_derivation(decision="rejected")
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.skipped is True
        assert action.new_decision is None
        assert action.reason == REASON_ALREADY_REJECTED
        assert action.target_independently_edited is False

    # ── Deferred (never applied) ──────────────────────────────────────────

    def test_deferred_settled_to_superseded(self) -> None:
        deriv = _make_derivation(decision="deferred")
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.reason == REASON_SUPERSEDED_NO_TARGET
        assert action.target_independently_edited is False

    # ── Applied with no target coordinates ────────────────────────────────

    def test_applied_no_target_coordinates_treated_as_missing(self) -> None:
        deriv = _make_derivation(
            decision="applied",
            applied_target_table=None,
            applied_target_id=None,
        )
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.reason == REASON_TARGET_MISSING
        assert action.target_exists is False

    def test_applied_null_target_table(self) -> None:
        deriv = _make_derivation(
            decision="applied",
            applied_target_table=None,
            applied_target_id=_uid(),
        )
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.new_decision == "superseded"
        assert action.reason == REASON_TARGET_MISSING

    def test_applied_null_target_id(self) -> None:
        deriv = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=None,
        )
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.new_decision == "superseded"
        assert action.reason == REASON_TARGET_MISSING

    # ── Applied with no probe available ──────────────────────────────────

    def test_applied_no_probe_conservative_settlement(self) -> None:
        """When no probe is available, assume not independently edited."""
        deriv = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
        )
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.reason == REASON_SUPERSEDED_SOURCE_CORRECTED
        assert action.target_independently_edited is False

    # ── Applied: target missing ──────────────────────────────────────────

    def test_applied_target_gone(self) -> None:
        deriv = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
        )
        state = TargetState(exists=False, last_changed_at=None)
        action = decide_reconciliation_action(deriv, target_state=state)
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.reason == REASON_TARGET_MISSING
        assert action.target_exists is False
        assert action.target_independently_edited is False

    # ── Applied: target exists, not edited ───────────────────────────────

    def test_applied_target_not_independently_edited(self) -> None:
        decided = _dt(10, 0)
        deriv = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
            decided_at=decided,
        )
        state = TargetState(exists=True, last_changed_at=_dt(9, 59))
        action = decide_reconciliation_action(deriv, target_state=state)
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.reason == REASON_SUPERSEDED_SOURCE_CORRECTED
        assert action.target_exists is True
        assert action.target_independently_edited is False

    # ── Applied: target exists, independently edited ─────────────────────

    def test_applied_target_independently_edited_protected(self) -> None:
        decided = _dt(10, 0)
        deriv = _make_derivation(
            decision="applied",
            applied_target_table="memories",
            applied_target_id=_uid(),
            decided_at=decided,
        )
        state = TargetState(exists=True, last_changed_at=_dt(11, 0))
        action = decide_reconciliation_action(deriv, target_state=state)
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.reason == REASON_TARGET_INDEPENDENTLY_EDITED
        assert action.target_exists is True
        assert action.target_independently_edited is True

    # ── Reinforced (same semantics as applied) ────────────────────────────

    def test_reinforced_settled_like_applied(self) -> None:
        decided = _dt(10, 0)
        deriv = _make_derivation(
            decision="reinforced",
            applied_target_table="observations",
            applied_target_id=_uid(),
            decided_at=decided,
        )
        state = TargetState(exists=True, last_changed_at=_dt(10, 0))
        action = decide_reconciliation_action(deriv, target_state=state)
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.reason == REASON_SUPERSEDED_SOURCE_CORRECTED

    def test_reinforced_independently_edited(self) -> None:
        decided = _dt(10, 0)
        deriv = _make_derivation(
            decision="reinforced",
            applied_target_table="observations",
            applied_target_id=_uid(),
            decided_at=decided,
        )
        state = TargetState(exists=True, last_changed_at=_dt(12, 0))
        action = decide_reconciliation_action(deriv, target_state=state)
        assert action.target_independently_edited is True
        assert action.reason == REASON_TARGET_INDEPENDENTLY_EDITED

    # ── Unknown decision ─────────────────────────────────────────────────

    def test_unknown_decision_defensive_noop(self) -> None:
        deriv = _make_derivation(decision="bogus_unknown")
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.skipped is True
        assert action.new_decision is None
        assert action.reason == REASON_UNKNOWN_DECISION

    # ── Coordinates preserved on actions ─────────────────────────────────

    def test_coordinates_preserved_in_action(self) -> None:
        target_table = "distillations"
        target_id = _uid()
        deriv = _make_derivation(
            decision="applied",
            applied_target_table=target_table,
            applied_target_id=target_id,
            decided_at=_dt(10, 0),
        )
        state = TargetState(exists=True, last_changed_at=_dt(9, 0))
        action = decide_reconciliation_action(deriv, target_state=state)
        assert action.target_table == target_table
        assert action.target_id == target_id

    def test_derivation_kind_preserved_in_action(self) -> None:
        deriv = _make_derivation(decision="deferred", kind="orientation")
        action = decide_reconciliation_action(deriv, target_state=None)
        assert action.derivation_kind == "orientation"


# ── ReconciliationEngine tests ──────────────────────────────────────────────


class FakeTargetProbe:
    """In-memory TargetEditProbe for testing the engine."""

    def __init__(self, states: dict[UUID, TargetState] | None = None) -> None:
        self._states: dict[UUID, TargetState] = states or {}
        self.inspect_calls: list[tuple[str, UUID]] = []

    async def inspect_target(self, *, table: str, target_id: UUID) -> TargetState:
        self.inspect_calls.append((table, target_id))
        return self._states.get(target_id, TargetState(exists=False, last_changed_at=None))

    def set(self, target_id: UUID, state: TargetState) -> None:
        self._states[target_id] = state


class FailingProbe:
    """A probe that always raises — tests best-effort failure."""

    async def inspect_target(self, *, table: str, target_id: UUID) -> TargetState:
        raise RuntimeError("probe failure")


class TestReconciliationEngine:
    """End-to-end engine tests with mock ledger and probe."""

    @pytest.fixture
    def store_mock(self) -> AsyncMock:
        return AsyncMock(spec=ReflectionStore)

    @pytest.fixture
    def ledger(self, store_mock: AsyncMock) -> DerivationLedger:
        return DerivationLedger(store_mock)

    @pytest.fixture
    def probe(self) -> FakeTargetProbe:
        return FakeTargetProbe()

    # ── No derivations ───────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_no_derivations_returns_empty_result(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[])
        engine = ReconciliationEngine(ledger)

        result = await engine.reconcile_correction(
            user_id=_uid(),
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        assert result.reconciled_count == 0
        assert result.skipped_count == 0
        assert result.independently_edited_count == 0
        assert len(result.actions) == 0

    # ── Deferred derivations ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_deferred_derivations_settled(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        user_id = _uid()
        superseded_id = _uid()
        corrected_id = _uid()

        deriv = _make_derivation(decision="deferred", user_id=user_id)
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store_mock.update_derivation_decision = AsyncMock(return_value=deriv)

        engine = ReconciliationEngine(ledger)
        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=superseded_id,
            corrected_entry_id=corrected_id,
        )

        assert result.reconciled_count == 1
        assert result.skipped_count == 0
        assert result.independently_edited_count == 0
        assert len(result.actions) == 1
        assert result.actions[0].reason == REASON_SUPERSEDED_NO_TARGET

        # Ledger update was called with target coordinates preserved
        store_mock.update_derivation_decision.assert_called_once_with(
            user_id=user_id,
            derivation_id=deriv.id,
            decision="superseded",
            applied_target_table=deriv.applied_target_table,
            applied_target_id=deriv.applied_target_id,
            processor_version=None,
        )

    # ── Applied derivations, not independently edited ────────────────────

    @pytest.mark.asyncio
    async def test_applied_not_edited_settled(
        self, ledger: DerivationLedger, store_mock: AsyncMock, probe: FakeTargetProbe
    ) -> None:
        user_id = _uid()
        target_id = _uid()
        decided = _dt(10, 0)

        deriv = _make_derivation(
            decision="applied",
            user_id=user_id,
            applied_target_table="memories",
            applied_target_id=target_id,
            decided_at=decided,
        )
        probe.set(target_id, TargetState(exists=True, last_changed_at=_dt(9, 0)))

        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store_mock.update_derivation_decision = AsyncMock(return_value=deriv)

        engine = ReconciliationEngine(ledger, target_probe=probe)
        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        assert result.reconciled_count == 1
        assert result.independently_edited_count == 0
        assert result.actions[0].reason == REASON_SUPERSEDED_SOURCE_CORRECTED

    # ── Applied derivations, independently edited ────────────────────────

    @pytest.mark.asyncio
    async def test_applied_independently_edited_protected(
        self, ledger: DerivationLedger, store_mock: AsyncMock, probe: FakeTargetProbe
    ) -> None:
        user_id = _uid()
        target_id = _uid()
        decided = _dt(10, 0)

        deriv = _make_derivation(
            decision="applied",
            user_id=user_id,
            applied_target_table="memories",
            applied_target_id=target_id,
            decided_at=decided,
        )
        probe.set(target_id, TargetState(exists=True, last_changed_at=_dt(12, 0)))

        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store_mock.update_derivation_decision = AsyncMock(return_value=deriv)

        engine = ReconciliationEngine(ledger, target_probe=probe)
        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        assert result.reconciled_count == 1
        assert result.independently_edited_count == 1
        assert result.actions[0].reason == REASON_TARGET_INDEPENDENTLY_EDITED
        assert result.actions[0].target_independently_edited is True

    # ── Applied derivations, target missing ──────────────────────────────

    @pytest.mark.asyncio
    async def test_applied_target_missing(
        self, ledger: DerivationLedger, store_mock: AsyncMock, probe: FakeTargetProbe
    ) -> None:
        user_id = _uid()
        target_id = _uid()

        deriv = _make_derivation(
            decision="applied",
            user_id=user_id,
            applied_target_table="memories",
            applied_target_id=target_id,
            decided_at=_dt(10, 0),
        )
        # Probe returns exists=False by default (not set)

        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store_mock.update_derivation_decision = AsyncMock(return_value=deriv)

        engine = ReconciliationEngine(ledger, target_probe=probe)
        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        assert result.reconciled_count == 1
        assert result.actions[0].reason == REASON_TARGET_MISSING

    # ── Idempotent re-entry ──────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_already_superseded_skipped_idempotent(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        user_id = _uid()

        deriv = _make_derivation(decision="superseded", user_id=user_id)
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])

        engine = ReconciliationEngine(ledger)
        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        assert result.reconciled_count == 0
        assert result.skipped_count == 1
        assert result.actions[0].skipped is True
        assert result.actions[0].reason == REASON_ALREADY_SUPERSEDED

        # No mutation occurred
        store_mock.update_derivation_decision.assert_not_called()

    # ── Mixed derivations ────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_mixed_derivations_partial_settlement(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        user_id = _uid()

        d1 = _make_derivation(decision="superseded", user_id=user_id)  # already done
        d2 = _make_derivation(decision="deferred", user_id=user_id)  # needs settling
        d3 = _make_derivation(decision="rejected", user_id=user_id)  # terminal

        store_mock.list_derivations_for_entry = AsyncMock(return_value=[d1, d2, d3])
        store_mock.update_derivation_decision = AsyncMock(return_value=d2)

        engine = ReconciliationEngine(ledger)
        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        assert result.reconciled_count == 1  # only d2
        assert result.skipped_count == 2  # d1 and d3
        assert len(result.actions) == 3

    # ── Chained corrections (idempotent) ────────────────────────────────

    @pytest.mark.asyncio
    async def test_chained_corrections_each_settles_own(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        """E1 → E2 → E3: each correction settles only its own derivations."""
        user_id = _uid()

        # First correction: E1 -> E2
        d_from_e1 = _make_derivation(decision="deferred", user_id=user_id)
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[d_from_e1])
        store_mock.update_derivation_decision = AsyncMock(return_value=d_from_e1)

        engine = ReconciliationEngine(ledger)
        result1 = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),  # E1
            corrected_entry_id=_uid(),  # E2
        )
        assert result1.reconciled_count == 1

        # Second correction: E2 -> E3 — no derivations from E2 (new entry)
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[])
        store_mock.update_derivation_decision.reset_mock()

        result2 = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),  # E2
            corrected_entry_id=_uid(),  # E3
        )
        assert result2.reconciled_count == 0
        assert result2.skipped_count == 0

    # ── Best-effort: probe failure does not abort ────────────────────────

    @pytest.mark.asyncio
    async def test_probe_failure_best_effort(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        user_id = _uid()

        deriv = _make_derivation(
            decision="applied",
            user_id=user_id,
            applied_target_table="memories",
            applied_target_id=_uid(),
            decided_at=_dt(10, 0),
        )
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store_mock.update_derivation_decision = AsyncMock(return_value=deriv)

        engine = ReconciliationEngine(ledger, target_probe=FailingProbe())
        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        # Must not raise — probe failure is best-effort
        assert result.reconciled_count == 1
        # Falls back to conservative "source corrected" since probe failed
        assert result.actions[0].reason == REASON_SUPERSEDED_SOURCE_CORRECTED
        assert result.actions[0].target_independently_edited is False

    # ── Structural guarantee: no target writes ───────────────────────────

    @pytest.mark.asyncio
    async def test_no_target_write_for_any_decision(
        self, ledger: DerivationLedger, store_mock: AsyncMock, probe: FakeTargetProbe
    ) -> None:
        """Reconciliation NEVER writes to target tables — only updates the ledger."""
        user_id = _uid()
        target_id = _uid()

        deriv = _make_derivation(
            decision="applied",
            user_id=user_id,
            applied_target_table="memories",
            applied_target_id=target_id,
            decided_at=_dt(10, 0),
        )
        probe.set(target_id, TargetState(exists=True, last_changed_at=_dt(12, 0)))

        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store_mock.update_derivation_decision = AsyncMock(return_value=deriv)

        engine = ReconciliationEngine(ledger, target_probe=probe)
        await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

        # The only mutation should be update_derivation_decision
        store_mock.update_derivation_decision.assert_called_once()
        # No target-table writes (create_memory, create_observation, etc.)
        for call_args in store_mock.method_calls:
            method_name = str(call_args[0])
            assert "target" not in method_name.lower() or "applied_target" in method_name.lower()
            # Only derivation-related methods should be called
            assert method_name in (
                "list_derivations_for_entry",
                "update_derivation_decision",
            ), f"Unexpected method called: {method_name}"

    # ── Provenance traversal ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_traverse_provenance_after_reconciliation(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        """Provenance traversal still works for superseded derivations."""
        user_id = _uid()
        deriv_id = _uid()

        # Create a derivation and a mock provenance chain
        store_mock.get_derivation = AsyncMock(
            return_value=_make_derivation(derivation_id=deriv_id, decision="superseded")
        )
        store_mock.get_entry = AsyncMock(return_value=MagicMock())
        store_mock.get_session = AsyncMock(return_value=MagicMock(source_message_ids=[_uid()]))

        engine = ReconciliationEngine(ledger)
        chain = await engine.traverse_provenance(
            user_id=user_id, derivation_id=deriv_id
        )

        assert chain is not None
        store_mock.get_derivation.assert_called_once_with(
            user_id=user_id, derivation_id=deriv_id
        )

    # ── processor_version forwarding ────────────────────────────────────

    @pytest.mark.asyncio
    async def test_processor_version_forwarded(
        self, ledger: DerivationLedger, store_mock: AsyncMock
    ) -> None:
        user_id = _uid()
        deriv = _make_derivation(decision="deferred", user_id=user_id)
        store_mock.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store_mock.update_derivation_decision = AsyncMock(return_value=deriv)

        engine = ReconciliationEngine(ledger)
        await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
            processor_version="v2.3.0",
        )

        store_mock.update_derivation_decision.assert_called_once_with(
            user_id=user_id,
            derivation_id=deriv.id,
            decision="superseded",
            applied_target_table=deriv.applied_target_table,
            applied_target_id=deriv.applied_target_id,
            processor_version="v2.3.0",
        )


# ── ReconciliationResult model tests ────────────────────────────────────────


class TestReconciliationResult:
    """ReconciliationResult data model integrity."""

    def test_empty_result(self) -> None:
        result = ReconciliationResult(
            corrected_entry_id=_uid(),
            superseded_entry_id=_uid(),
        )
        assert result.reconciled_count == 0
        assert result.skipped_count == 0
        assert result.independently_edited_count == 0
        assert result.actions == []

    def test_counts_match_actions(self) -> None:
        result = ReconciliationResult(
            corrected_entry_id=_uid(),
            superseded_entry_id=_uid(),
            actions=[
                ReconciliationAction(
                    derivation_id=_uid(),
                    derivation_kind="memory",
                    previous_decision="deferred",
                    new_decision="superseded",
                    target_table=None,
                    target_id=None,
                    reason=REASON_SUPERSEDED_NO_TARGET,
                    target_independently_edited=False,
                    target_exists=None,
                    skipped=False,
                ),
            ],
            reconciled_count=1,
            skipped_count=0,
            independently_edited_count=0,
        )
        assert result.reconciled_count == 1


# ── ReconciliationAction model tests ────────────────────────────────────────


class TestReconciliationAction:
    """ReconciliationAction data model integrity."""

    def test_skipped_action_no_new_decision(self) -> None:
        action = ReconciliationAction(
            derivation_id=_uid(),
            derivation_kind="observation",
            previous_decision="superseded",
            new_decision=None,
            target_table=None,
            target_id=None,
            reason=REASON_ALREADY_SUPERSEDED,
            target_independently_edited=False,
            target_exists=None,
            skipped=True,
        )
        assert action.skipped is True
        assert action.new_decision is None

    def test_settled_action_has_new_decision(self) -> None:
        action = ReconciliationAction(
            derivation_id=_uid(),
            derivation_kind="memory",
            previous_decision="applied",
            new_decision="superseded",
            target_table="memories",
            target_id=_uid(),
            reason=REASON_SUPERSEDED_SOURCE_CORRECTED,
            target_independently_edited=False,
            target_exists=True,
            skipped=False,
        )
        assert action.skipped is False
        assert action.new_decision == "superseded"
        assert action.target_table == "memories"

    def test_independent_edit_action(self) -> None:
        action = ReconciliationAction(
            derivation_id=_uid(),
            derivation_kind="memory",
            previous_decision="applied",
            new_decision="superseded",
            target_table="memories",
            target_id=_uid(),
            reason=REASON_TARGET_INDEPENDENTLY_EDITED,
            target_independently_edited=True,
            target_exists=True,
            skipped=False,
        )
        assert action.target_independently_edited is True


# ── TargetState model tests ─────────────────────────────────────────────────


class TestTargetState:
    """TargetState data model integrity."""

    def test_exists_with_no_timestamp(self) -> None:
        state = TargetState(exists=True, last_changed_at=None)
        assert state.exists is True
        assert state.last_changed_at is None

    def test_missing_target(self) -> None:
        state = TargetState(exists=False, last_changed_at=None)
        assert state.exists is False

    def test_exists_with_timestamp(self) -> None:
        ts = _dt(10, 0)
        state = TargetState(exists=True, last_changed_at=ts)
        assert state.exists is True
        assert state.last_changed_at == ts


# ── TargetEditProbe protocol test ───────────────────────────────────────────


class TestTargetEditProbeProtocol:
    """The TargetEditProbe protocol works with conforming implementations."""

    def test_fake_probe_conforms(self) -> None:
        probe = FakeTargetProbe()
        assert isinstance(probe, TargetEditProbe)

    def test_failing_probe_conforms(self) -> None:
        probe = FailingProbe()
        assert isinstance(probe, TargetEditProbe)

    def test_inspect_target_in_fake(self) -> None:
        import asyncio

        async def _run() -> None:
            probe = FakeTargetProbe()
            target_id = _uid()
            probe.set(target_id, TargetState(exists=True, last_changed_at=_dt(10, 0)))
            state = await probe.inspect_target(table="memories", target_id=target_id)
            assert state.exists is True

        asyncio.run(_run())


# ── Integration: _reconcile_after_correction best-effort ───────────────────


class TestReconcileAfterCorrection:
    """Best-effort correction-service integration."""

    def _import_reconcile_after_correction(self):
        from app.services.tools.reflection_tools import _reconcile_after_correction

        return _reconcile_after_correction

    @pytest.mark.asyncio
    async def test_best_effort_no_pool_still_runs(self) -> None:
        """When pool is None, reconciliation still runs (no probe, conservative)."""
        store = AsyncMock(spec=ReflectionStore)
        user_id = _uid()
        superseded = _uid()
        corrected = _uid()

        deriv = _make_derivation(decision="deferred", user_id=user_id)
        store.list_derivations_for_entry = AsyncMock(return_value=[deriv])
        store.update_derivation_decision = AsyncMock(return_value=deriv)

        _reconcile_after_correction = self._import_reconcile_after_correction()
        await _reconcile_after_correction(
            store=store,
            pool=None,
            user_id=user_id,
            superseded_entry_id=superseded,
            corrected_entry_id=corrected,
        )

        # Should still run without probe
        store.list_derivations_for_entry.assert_called_once()
        store.update_derivation_decision.assert_called_once()

    @pytest.mark.asyncio
    async def test_best_effort_catches_and_swallows(self) -> None:
        """When the store raises, reconciliation swallows and returns."""
        store = AsyncMock(spec=ReflectionStore)
        store.list_derivations_for_entry = AsyncMock(side_effect=RuntimeError("DB down"))

        _reconcile_after_correction = self._import_reconcile_after_correction()
        # Must not raise
        await _reconcile_after_correction(
            store=store,
            pool=None,
            user_id=_uid(),
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )

    @pytest.mark.asyncio
    async def test_best_effort_no_derivations(self) -> None:
        """No derivations is not an error."""
        store = AsyncMock(spec=ReflectionStore)
        store.list_derivations_for_entry = AsyncMock(return_value=[])

        _reconcile_after_correction = self._import_reconcile_after_correction()
        await _reconcile_after_correction(
            store=store,
            pool=None,
            user_id=_uid(),
            superseded_entry_id=_uid(),
            corrected_entry_id=_uid(),
        )
        # No update called since there's nothing to reconcile
        store.update_derivation_decision.assert_not_called()


# ── Reason constants integrity ──────────────────────────────────────────────


class TestReasonConstants:
    """Ensure reason constants are stable strings."""

    def test_all_reasons_are_strings(self) -> None:
        reasons = [
            REASON_ALREADY_SUPERSEDED,
            REASON_ALREADY_REJECTED,
            REASON_SUPERSEDED_NO_TARGET,
            REASON_SUPERSEDED_SOURCE_CORRECTED,
            REASON_TARGET_INDEPENDENTLY_EDITED,
            REASON_TARGET_MISSING,
            REASON_UNKNOWN_DECISION,
            REASON_NO_DERIVATIONS,
        ]
        for r in reasons:
            assert isinstance(r, str), f"{r!r} is not a string"

    def test_all_reasons_are_unique(self) -> None:
        reasons = [
            REASON_ALREADY_SUPERSEDED,
            REASON_ALREADY_REJECTED,
            REASON_SUPERSEDED_NO_TARGET,
            REASON_SUPERSEDED_SOURCE_CORRECTED,
            REASON_TARGET_INDEPENDENTLY_EDITED,
            REASON_TARGET_MISSING,
            REASON_UNKNOWN_DECISION,
            REASON_NO_DERIVATIONS,
        ]
        assert len(reasons) == len(set(reasons))
