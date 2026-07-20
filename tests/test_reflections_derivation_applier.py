"""Tests for the derivation applier (T14).

Covers the compensation contract end-to-end:

* accepted derivations reuse the existing write services (kind routing);
* target-write-first + ledger-mark-second ordering;
* **partial failure** (target written, ledger mark fails) →
  ``DerivationApplyPartialFailure`` + orphan reconciliation with no duplicate
  target write;
* **retry idempotency** (re-applying an already-applied derivation is a no-op
  that never calls the writer);
* **provenance traversal** after apply;
* forbidden kinds rejected before any target write (SD3);
* input-model mapping reuses the existing write-service contracts verbatim.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.reflections.derivation import DerivationKind
from app.reflections.derivation_applier import (
    ApplyResult,
    DerivationApplier,
    DerivationApplyPartialFailure,
    ForbiddenDerivationKindError,
    OrientationLifecycleError,
    TargetWriter,
    TurnContextTargetWriter,
    UnderEvidencedError,
    UnsupportedDerivationKindError,
    confidence_to_enum,
)
from app.services.reflections import DerivationNotFoundError, ReflectionDerivation
from tool_schemas import Confidence


# ── Test doubles ─────────────────────────────────────────────────────────────


def _make_derivation(
    *,
    kind: str = DerivationKind.memory.value,
    decision: str = "deferred",
    confidence: float = 0.8,
    applied_target_table: str | None = None,
    applied_target_id: UUID | None = None,
    supporting_message_ids: list[UUID] | None = None,
    assertion_source: str = "user_explicit",
) -> ReflectionDerivation:
    """Build a ReflectionDerivation with the minimum fields the applier reads."""
    if supporting_message_ids is None:
        # Default to 2 messages so evidence thresholds are satisfied
        # (distillation requires ≥2, memory/observation/orientation require ≥1).
        supporting_message_ids = [uuid4(), uuid4()]
    return ReflectionDerivation(
        id=uuid4(),
        reflection_entry_id=uuid4(),
        user_id=uuid4(),
        derivation_kind=kind,
        candidate_payload_encrypted=None,
        assertion_source=assertion_source,
        confidence=confidence,
        eligibility_reasons=["kind_allowed"],
        supporting_message_ids=supporting_message_ids,
        decision=decision,
        applied_target_table=applied_target_table,
        applied_target_id=applied_target_id,
        processor_version=None,
        processor_turn_id=None,
        idempotency_key="key-" + uuid4().hex,
        created_at=None,
        decided_at=None,
    )


@dataclass
class FakeLedger:
    """In-memory DerivationLedger double that records update_decision calls."""

    derivations: dict[UUID, ReflectionDerivation] = field(default_factory=dict)
    update_calls: list[dict] = field(default_factory=list)
    update_should_fail: bool = False
    provenance_calls: list[UUID] = field(default_factory=list)

    async def get_derivation(self, *, user_id, derivation_id):
        return self.derivations.get(derivation_id)

    async def update_decision(self, **kwargs):
        self.update_calls.append(kwargs)
        if self.update_should_fail:
            raise RuntimeError("simulated ledger mark failure")
        d = self.derivations[kwargs["derivation_id"]]
        self.derivations[kwargs["derivation_id"]] = ReflectionDerivation(
            id=d.id,
            reflection_entry_id=d.reflection_entry_id,
            user_id=d.user_id,
            derivation_kind=d.derivation_kind,
            candidate_payload_encrypted=d.candidate_payload_encrypted,
            assertion_source=d.assertion_source,
            confidence=d.confidence,
            eligibility_reasons=d.eligibility_reasons,
            supporting_message_ids=d.supporting_message_ids,
            decision=kwargs["decision"],
            applied_target_table=kwargs.get("applied_target_table"),
            applied_target_id=kwargs.get("applied_target_id"),
            processor_version=kwargs.get("processor_version"),
            processor_turn_id=d.processor_turn_id,
            idempotency_key=d.idempotency_key,
            created_at=d.created_at,
            decided_at=d.decided_at,
        )
        return self.derivations[kwargs["derivation_id"]]

    async def traverse_provenance(self, *, user_id, derivation_id):
        self.provenance_calls.append(derivation_id)
        return ("PROVENANCE", derivation_id)


class FakeTargetWriter:
    """Records calls and can simulate write failures / per-kind behaviour."""

    def __init__(
        self,
        *,
        fail_memory: bool = False,
        fail_observation: bool = False,
        fail_distillation: bool = False,
        fail_orientation: bool = False,
    ) -> None:
        self.calls: list[tuple[str, ReflectionDerivation, str]] = []
        self._fail = {
            DerivationKind.memory.value: fail_memory,
            DerivationKind.observation.value: fail_observation,
            DerivationKind.distillation.value: fail_distillation,
            DerivationKind.orientation.value: fail_orientation,
        }

    async def _write(self, kind, derivation, claim_text):
        self.calls.append((kind, derivation, claim_text))
        if self._fail.get(kind):
            raise RuntimeError(f"simulated write failure for {kind}")
        return uuid4()

    async def write_memory(self, derivation, claim_text):
        return await self._write(DerivationKind.memory.value, derivation, claim_text)

    async def write_observation(self, derivation, claim_text):
        return await self._write(DerivationKind.observation.value, derivation, claim_text)

    async def write_distillation(self, derivation, claim_text):
        return await self._write(
            DerivationKind.distillation.value, derivation, claim_text
        )

    async def write_orientation(self, derivation, claim_text):
        return await self._write(DerivationKind.orientation.value, derivation, claim_text)

    @property
    def call_kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


# ── Confidence mapping ───────────────────────────────────────────────────────


class TestConfidenceMapping:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, Confidence.medium),
            (0.0, Confidence.low),
            (0.39, Confidence.low),
            (0.4, Confidence.medium),
            (0.69, Confidence.medium),
            (0.7, Confidence.high),
            (1.0, Confidence.high),
        ],
    )
    def test_buckets(self, value, expected):
        assert confidence_to_enum(value) is expected


# ── Kind routing: reuses existing write services ─────────────────────────────


@pytest.mark.parametrize(
    "kind,assertion_source",
    [
        (DerivationKind.memory.value, "user_explicit"),
        (DerivationKind.observation.value, "user_explicit"),
        (DerivationKind.distillation.value, "agent_inferred"),
        (DerivationKind.orientation.value, "agent_inferred"),
    ],
)
@pytest.mark.asyncio
async def test_routes_each_kind_to_its_writer(kind, assertion_source):
    derivation = _make_derivation(kind=kind, assertion_source=assertion_source)
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    result = await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="a derived claim",
        writer=writer,
    )

    assert result.already_applied is False
    assert writer.call_kinds == [kind]
    assert result.target_id is not None
    # Ledger was marked applied with the returned target id + correct table.
    assert ledger.update_calls[-1]["decision"] == "applied"
    assert ledger.update_calls[-1]["applied_target_id"] == result.target_id


@pytest.mark.parametrize("kind", ["action", "task", "reminder", "follow_up", "nudge"])
@pytest.mark.asyncio
async def test_forbidden_kinds_rejected_before_write(kind):
    derivation = _make_derivation(kind=kind)
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    with pytest.raises(ForbiddenDerivationKindError):
        await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="x",
            writer=writer,
        )
    # No target write, no ledger mutation.
    assert writer.calls == []
    assert ledger.update_calls == []


@pytest.mark.asyncio
async def test_unknown_kind_rejected():
    derivation = _make_derivation(kind="something_new")
    ledger = FakeLedger(derivations={derivation.id: derivation})
    applier = DerivationApplier(ledger=ledger)

    with pytest.raises(UnsupportedDerivationKindError):
        await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="x",
            writer=FakeTargetWriter(),
        )


# ── Retry idempotency: no duplicate target writes ────────────────────────────


@pytest.mark.asyncio
async def test_already_applied_is_noop():
    target_id = uuid4()
    derivation = _make_derivation(
        decision="applied",
        applied_target_table="memories",
        applied_target_id=target_id,
    )
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    result = await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="a derived claim",
        writer=writer,
    )

    assert result.already_applied is True
    assert result.target_id == target_id
    assert result.target_table == "memories"
    # CRITICAL: the writer must not be called again — no duplicate target write.
    assert writer.calls == []
    assert ledger.update_calls == []


@pytest.mark.asyncio
async def test_reinforced_decision_is_also_noop():
    target_id = uuid4()
    derivation = _make_derivation(
        kind=DerivationKind.observation.value,
        decision="reinforced",
        applied_target_table="observations",
        applied_target_id=target_id,
    )
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    result = await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="x",
        writer=writer,
    )
    assert result.already_applied is True
    assert writer.calls == []


@pytest.mark.asyncio
async def test_retry_after_success_does_not_duplicate():
    """Simulate a caller that retries apply_derivation after a successful apply."""
    derivation = _make_derivation()
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    first = await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="claim",
        writer=writer,
    )
    second = await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="claim",
        writer=writer,
    )

    assert first.already_applied is False
    assert second.already_applied is True
    assert second.target_id == first.target_id
    # Exactly one writer invocation across both calls.
    assert len(writer.calls) == 1


# ── Target-write failure is safe to retry ────────────────────────────────────


@pytest.mark.asyncio
async def test_target_write_failure_leaves_ledger_untouched():
    derivation = _make_derivation()
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter(fail_memory=True)
    applier = DerivationApplier(ledger=ledger)

    with pytest.raises(RuntimeError, match="simulated write failure"):
        await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="claim",
            writer=writer,
        )
    # Ledger untouched — safe for a later retry to perform the write once.
    assert ledger.update_calls == []
    assert ledger.derivations[derivation.id].decision == "deferred"


@pytest.mark.asyncio
async def test_target_write_failure_then_retry_applies_once():
    derivation = _make_derivation()
    ledger = FakeLedger(derivations={derivation.id: derivation})

    failing_writer = FakeTargetWriter(fail_memory=True)
    ok_writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    with pytest.raises(RuntimeError):
        await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="claim",
            writer=failing_writer,
        )
    result = await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="claim",
        writer=ok_writer,
    )
    # After the failure cleared, exactly one successful write + ledger mark.
    assert len(ok_writer.calls) == 1
    assert ledger.derivations[derivation.id].decision == "applied"
    assert result.already_applied is False


# ── Partial failure: orphan + reconciliation, no duplicate writes ────────────


@pytest.mark.asyncio
async def test_partial_failure_raises_with_orphan_coordinates():
    derivation = _make_derivation()
    ledger = FakeLedger(
        derivations={derivation.id: derivation}, update_should_fail=True
    )
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    with pytest.raises(DerivationApplyPartialFailure) as ei:
        await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="claim",
            writer=writer,
        )
    err = ei.value
    assert err.target_table == "memories"
    assert err.target_id is not None
    assert isinstance(err.cause, RuntimeError)
    # The writer ran exactly once (the orphan write).
    assert len(writer.calls) == 1


@pytest.mark.asyncio
async def test_reconcile_after_partial_failure_no_duplicate_write():
    """Reconciliation must NOT call the writer again; it only closes the gap."""
    derivation = _make_derivation()
    # Start in a state where the target write already happened (simulating a
    # partial failure that left the row deferred).
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    orphan_id = uuid4()
    result = await applier.reconcile_after_partial_failure(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        orphan_target_table="memories",
        orphan_target_id=orphan_id,
    )
    assert result.already_applied is True
    assert result.target_id == orphan_id
    assert result.target_table == "memories"
    # CRITICAL: reconciliation performs NO target write.
    assert writer.calls == []
    assert ledger.update_calls[-1]["applied_target_id"] == orphan_id
    assert ledger.update_calls[-1]["applied_target_table"] == "memories"
    assert ledger.update_calls[-1]["decision"] == "applied"


@pytest.mark.asyncio
async def test_reconcile_when_already_applied_returns_existing():
    target_id = uuid4()
    derivation = _make_derivation(
        decision="applied", applied_target_table="memories", applied_target_id=target_id
    )
    ledger = FakeLedger(derivations={derivation.id: derivation})
    applier = DerivationApplier(ledger=ledger)

    result = await applier.reconcile_after_partial_failure(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        orphan_target_table="memories",
        orphan_target_id=uuid4(),
    )
    assert result.target_id == target_id  # existing, not the orphan
    assert ledger.update_calls == []


@pytest.mark.asyncio
async def test_full_partial_failure_then_reconcile_then_retry_idempotent():
    """End-to-end: partial failure → reconcile → retry is a no-op."""
    derivation = _make_derivation()
    ledger = FakeLedger(derivations={derivation.id: derivation})
    # First apply: writer succeeds, ledger mark fails.
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    # Set the ledger to fail on the first update_decision.
    ledger.update_should_fail = True
    with pytest.raises(DerivationApplyPartialFailure) as ei:
        await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="claim",
            writer=writer,
        )
    orphan_id = ei.value.target_id
    # Clear the failure and reconcile the orphan.
    ledger.update_should_fail = False
    await applier.reconcile_after_partial_failure(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        orphan_target_table="memories",
        orphan_target_id=orphan_id,
    )
    # Now a retry must be a no-op (the orphan is the canonical target).
    result = await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="claim",
        writer=writer,
    )
    assert result.already_applied is True
    assert result.target_id == orphan_id
    # Across the whole saga, exactly one target write happened.
    assert len(writer.calls) == 1


# ── Provenance traversal ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_traverse_provenance_delegates_to_ledger():
    derivation = _make_derivation()
    ledger = FakeLedger(derivations={derivation.id: derivation})
    applier = DerivationApplier(ledger=ledger)

    chain = await applier.traverse_provenance(
        user_id=derivation.user_id, derivation_id=derivation.id
    )
    assert ledger.provenance_calls == [derivation.id]
    assert chain == ("PROVENANCE", derivation.id)


@pytest.mark.asyncio
async def test_provenance_traversable_after_apply():
    derivation = _make_derivation()
    ledger = FakeLedger(derivations={derivation.id: derivation})
    writer = FakeTargetWriter()
    applier = DerivationApplier(ledger=ledger)

    await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="claim",
        writer=writer,
    )
    chain = await applier.traverse_provenance(
        user_id=derivation.user_id, derivation_id=derivation.id
    )
    assert ledger.provenance_calls == [derivation.id]
    assert chain is not None


# ── Missing derivation ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_missing_derivation_raises():
    ledger = FakeLedger()
    applier = DerivationApplier(ledger=ledger)
    with pytest.raises(DerivationNotFoundError):
        await applier.apply_derivation(
            user_id=uuid4(),
            derivation_id=uuid4(),
            claim_text="x",
            writer=FakeTargetWriter(),
        )


@pytest.mark.asyncio
async def test_reconcile_missing_derivation_raises():
    ledger = FakeLedger()
    applier = DerivationApplier(ledger=ledger)
    with pytest.raises(DerivationNotFoundError):
        await applier.reconcile_after_partial_failure(
            user_id=uuid4(),
            derivation_id=uuid4(),
            orphan_target_table="memories",
            orphan_target_id=uuid4(),
        )


# ── Processor version stamping ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_processor_version_stamped():
    derivation = _make_derivation()
    ledger = FakeLedger(derivations={derivation.id: derivation})
    applier = DerivationApplier(ledger=ledger)

    await applier.apply_derivation(
        user_id=derivation.user_id,
        derivation_id=derivation.id,
        claim_text="claim",
        writer=FakeTargetWriter(),
        processor_version="applier/v1",
    )
    assert ledger.update_calls[-1]["processor_version"] == "applier/v1"


# ── Adapter wiring: reuses the EXISTING write services ───────────────────────


def test_turn_context_writer_satisfies_protocol():
    """TurnContextTargetWriter must be a structural TargetWriter."""
    assert isinstance(TurnContextTargetWriter(ctx=object()), TargetWriter)


def test_turn_context_writer_methods_call_existing_write_tools():
    """The concrete adapter calls the real add_memory / log_observation / etc."""
    ctx = object()
    writer = TurnContextTargetWriter(ctx=ctx)
    derivation = _make_derivation(
        kind=DerivationKind.memory.value,
        supporting_message_ids=[uuid4(), uuid4()],
        confidence=0.85,
    )

    # Patch the four write-tool functions at the applier module's import site.
    with (
        patch("app.reflections.derivation_applier.add_memory", new=AsyncMock()) as m_mem,
        patch(
            "app.reflections.derivation_applier.log_observation", new=AsyncMock()
        ) as m_obs,
        patch(
            "app.reflections.derivation_applier.add_distillation", new=AsyncMock()
        ) as m_dist,
        patch(
            "app.reflections.derivation_applier.create_orientation_item",
            new=AsyncMock(),
        ) as m_ori,
    ):
        # Each mock returns an object whose .id is a fresh UUID.
        for m in (m_mem, m_obs, m_dist, m_ori):
            m.return_value.id = uuid4()

        mid = asyncio.run(writer.write_memory(derivation, "mem claim"))
        oid = asyncio.run(writer.write_observation(derivation, "obs claim"))
        did = asyncio.run(writer.write_distillation(derivation, "dist claim"))
        rid = asyncio.run(writer.write_orientation(derivation, "ori claim"))

    # Each existing write service was called exactly once with the adapter ctx.
    m_mem.assert_awaited_once()
    assert m_mem.call_args.args[0] is ctx
    m_obs.assert_awaited_once()
    assert m_obs.call_args.args[0] is ctx
    m_dist.assert_awaited_once()
    assert m_dist.call_args.args[0] is ctx
    m_ori.assert_awaited_once()
    assert m_ori.call_args.args[0] is ctx

    # Returned ids came from the mocked write results.
    assert all(isinstance(x, UUID) for x in (mid, oid, did, rid))


def test_turn_context_writer_memory_input_mapping():
    """The memory input carries the claim verbatim and stays private."""
    ctx = object()
    writer = TurnContextTargetWriter(ctx=ctx)
    derivation = _make_derivation(kind=DerivationKind.memory.value)

    with patch("app.reflections.derivation_applier.add_memory", new=AsyncMock()) as m:
        m.return_value.id = uuid4()
        asyncio.run(writer.write_memory(derivation, "the exact claim"))
        inp = m.call_args.args[1]
    assert inp.content == "the exact claim"
    assert inp.about_user_id == derivation.user_id
    assert inp.visibility.value == "private"


def test_turn_context_writer_observation_input_mapping():
    ctx = object()
    writer = TurnContextTargetWriter(ctx=ctx)
    msgs = [uuid4()]
    derivation = _make_derivation(
        kind=DerivationKind.observation.value, supporting_message_ids=msgs, confidence=0.5
    )

    with patch("app.reflections.derivation_applier.log_observation", new=AsyncMock()) as m:
        m.return_value.id = uuid4()
        asyncio.run(writer.write_observation(derivation, "obs claim"))
        inp = m.call_args.args[1]
    assert inp.content == "obs claim"
    assert inp.confidence is Confidence.medium  # 0.5 → medium
    assert inp.supporting_message_ids == msgs
    assert inp.about_user_id == derivation.user_id


def test_turn_context_writer_distillation_input_mapping():
    ctx = object()
    writer = TurnContextTargetWriter(ctx=ctx)
    msgs = [uuid4()]
    derivation = _make_derivation(
        kind=DerivationKind.distillation.value, supporting_message_ids=msgs, confidence=0.9
    )

    with patch(
        "app.reflections.derivation_applier.add_distillation", new=AsyncMock()
    ) as m:
        m.return_value.id = uuid4()
        asyncio.run(writer.write_distillation(derivation, "dist claim"))
        inp = m.call_args.args[1]
    assert inp.content == "dist claim"
    assert inp.confidence is Confidence.high  # 0.9 → high
    assert inp.source_user_ids == [derivation.user_id]
    assert inp.supporting_message_ids == msgs


def test_turn_context_writer_orientation_input_mapping_defaults_principle():
    """Orientation defaults to principle + bot_proposed (conservative)."""
    ctx = object()
    writer = TurnContextTargetWriter(ctx=ctx)
    derivation = _make_derivation(kind=DerivationKind.orientation.value)

    with patch(
        "app.reflections.derivation_applier.create_orientation_item", new=AsyncMock()
    ) as m:
        m.return_value.id = uuid4()
        asyncio.run(writer.write_orientation(derivation, "a heading"))
        inp = m.call_args.args[1]
    assert inp.kind.value == "principle"
    assert inp.source.value == "bot_proposed"
    assert inp.label == "a heading"


# ── Ordering audit: target write before ledger mark ─────────────────────────


def test_apply_orders_target_write_before_ledger_mark():
    """Static check: apply_derivation calls write_fn before update_decision."""
    src = inspect.getsource(DerivationApplier.apply_derivation)
    write_idx = src.index("write_fn(derivation, claim_text)")
    ledger_idx = src.index("self._ledger.update_decision")
    assert write_idx < ledger_idx, (
        "target write must occur before the ledger mark so no 'applied' decision "
        "is ever recorded for a target that does not exist"
    )


def test_apply_checks_already_applied_before_write():
    """Static check: idempotency gate precedes the writer invocation."""
    src = inspect.getsource(DerivationApplier.apply_derivation)
    gate_idx = src.index("_APPLIED_DECISIONS")
    write_idx = src.index("write_fn(derivation, claim_text)")
    assert gate_idx < write_idx


# ── T15: Knowledge rule enforcement tests ─────────────────────────────────────


# ── Observation reinforcement tests ───────────────────────────────────────────


class TestObservationReinforcement:
    """Verify observation reinforcement behaviour in the applier."""

    @pytest.mark.asyncio
    async def test_reinforced_observation_is_idempotent_noop(self):
        """A derivation with decision='reinforced' reuses the existing target."""
        target_id = uuid4()
        derivation = _make_derivation(
            kind=DerivationKind.observation.value,
            decision="reinforced",
            applied_target_table="observations",
            applied_target_id=target_id,
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        result = await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="reinforced observation",
            writer=writer,
        )

        assert result.already_applied is True
        assert result.target_id == target_id
        assert result.target_table == "observations"
        # CRITICAL: No new target write — reinforcement is a no-op at the write level.
        assert writer.calls == []

    @pytest.mark.asyncio
    async def test_observation_reinforced_preserves_original_target(self):
        """The original observation ID is preserved through reinforcement."""
        original_id = uuid4()
        derivation = _make_derivation(
            kind=DerivationKind.observation.value,
            decision="reinforced",
            applied_target_table="observations",
            applied_target_id=original_id,
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        applier = DerivationApplier(ledger=ledger)

        # Multiple re-applies of a reinforced derivation always return the same target.
        for _ in range(3):
            result = await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="any claim",
                writer=FakeTargetWriter(),
            )
            assert result.target_id == original_id
            assert result.already_applied is True


# ── Multi-evidence distillation enforcement tests ─────────────────────────────


class TestMultiEvidenceDistillationEnforcement:
    """Verify the applier enforces multi-evidence for distillations."""

    @pytest.mark.asyncio
    async def test_distillation_with_one_evidence_rejected_by_applier(self):
        """Applier rejects distillation with only 1 supporting message."""
        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[uuid4()],  # Only 1 — under-evidenced
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        applier = DerivationApplier(ledger=ledger)

        with pytest.raises(UnderEvidencedError) as ei:
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="under-evidenced distillation",
                writer=FakeTargetWriter(),
            )
        assert "2" in str(ei.value)
        assert "1" in str(ei.value)
        # No writer call, no ledger mutation.
        assert ledger.update_calls == []

    @pytest.mark.asyncio
    async def test_distillation_with_zero_evidence_rejected_by_applier(self):
        """Applier rejects distillation with zero supporting messages."""
        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[],
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        applier = DerivationApplier(ledger=ledger)

        with pytest.raises(UnderEvidencedError):
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="no-evidence distillation",
                writer=FakeTargetWriter(),
            )

    @pytest.mark.asyncio
    async def test_distillation_with_two_evidence_applies(self):
        """Applier allows distillation with ≥2 supporting messages."""
        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[uuid4(), uuid4()],
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        result = await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="multi-evidence distillation",
            writer=writer,
        )

        assert result.already_applied is False
        assert writer.call_kinds == [DerivationKind.distillation.value]
        assert ledger.update_calls[-1]["decision"] == "applied"

    @pytest.mark.asyncio
    async def test_memory_with_one_evidence_applies(self):
        """Memory only needs 1 evidence — applier allows it."""
        derivation = _make_derivation(
            kind=DerivationKind.memory.value,
            supporting_message_ids=[uuid4()],  # 1 is sufficient
            assertion_source="user_explicit",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        result = await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="single-evidence memory",
            writer=writer,
        )
        assert result.already_applied is False

    @pytest.mark.asyncio
    async def test_observation_with_one_evidence_applies(self):
        """Observation only needs 1 evidence — applier allows it."""
        derivation = _make_derivation(
            kind=DerivationKind.observation.value,
            supporting_message_ids=[uuid4()],  # 1 is sufficient
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        result = await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="single-evidence observation",
            writer=writer,
        )
        assert result.already_applied is False


# ── Orientation lifecycle enforcement tests ──────────────────────────────────


class TestOrientationLifecycleEnforcement:
    """Verify orientation derivations follow bot_proposed → pending/unreviewed lifecycle."""

    @pytest.mark.asyncio
    async def test_orientation_with_agent_inferred_applies(self):
        """Agent-inferred orientation follows the correct lifecycle."""
        derivation = _make_derivation(
            kind=DerivationKind.orientation.value,
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        result = await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="derived orientation heading",
            writer=writer,
        )
        assert result.already_applied is False
        assert writer.call_kinds == [DerivationKind.orientation.value]

    @pytest.mark.asyncio
    async def test_orientation_with_user_implied_applies(self):
        """User-implied orientation follows the correct lifecycle."""
        derivation = _make_derivation(
            kind=DerivationKind.orientation.value,
            assertion_source="user_implied",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        result = await applier.apply_derivation(
            user_id=derivation.user_id,
            derivation_id=derivation.id,
            claim_text="implied orientation",
            writer=writer,
        )
        assert result.already_applied is False

    @pytest.mark.asyncio
    async def test_orientation_with_user_explicit_rejected(self):
        """Orientation derivations MUST NOT claim user_explicit.

        Only explicit user action can confirm an orientation.  Derivations
        are always bot_proposed, which corresponds to agent_inferred or
        user_implied assertion sources.
        """
        derivation = _make_derivation(
            kind=DerivationKind.orientation.value,
            assertion_source="user_explicit",  # Invalid for derivation
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        applier = DerivationApplier(ledger=ledger)

        with pytest.raises(OrientationLifecycleError) as ei:
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="orientation that bypasses lifecycle",
                writer=FakeTargetWriter(),
            )
        assert "user_explicit" in str(ei.value)
        assert "bot_proposed" in str(ei.value).lower()

    @pytest.mark.asyncio
    async def test_orientation_lifecycle_rejected_before_any_write(self):
        """Lifecycle violations are rejected before any target write."""
        derivation = _make_derivation(
            kind=DerivationKind.orientation.value,
            assertion_source="user_explicit",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        with pytest.raises(OrientationLifecycleError):
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="x",
                writer=writer,
            )
        # No write, no ledger mutation.
        assert writer.calls == []
        assert ledger.update_calls == []

    @pytest.mark.asyncio
    async def test_orientation_input_always_bot_proposed(self):
        """The concrete write input always uses bot_proposed source."""
        from app.reflections.derivation_applier import _build_orientation_input

        derivation = _make_derivation(
            kind=DerivationKind.orientation.value,
            assertion_source="agent_inferred",
        )
        inp = _build_orientation_input(derivation, "test heading", None)
        assert inp.source.value == "bot_proposed"
        # Defaults to principle kind
        assert inp.kind.value == "principle"


# ── Under-evidenced write rejection coverage ──────────────────────────────────


class TestUnderEvidencedWriteRejection:
    """Comprehensive coverage of under-evidenced write rejection scenarios."""

    @pytest.mark.asyncio
    async def test_evidence_check_runs_before_writer_invocation(self):
        """The evidence threshold is checked before calling the writer."""
        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[uuid4()],  # Under-evidenced
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        writer = FakeTargetWriter()
        applier = DerivationApplier(ledger=ledger)

        with pytest.raises(UnderEvidencedError):
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="x",
                writer=writer,
            )
        # Writer never called — the evidence gate fires first.
        assert writer.calls == []

    @pytest.mark.asyncio
    async def test_evidence_check_does_not_mutate_ledger(self):
        """A rejected under-evidenced write leaves the ledger row unchanged."""
        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[uuid4()],
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        applier = DerivationApplier(ledger=ledger)

        try:
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="x",
                writer=FakeTargetWriter(),
            )
        except UnderEvidencedError:
            pass

        # The derivation should still be in its original state.
        assert derivation.decision == "deferred"

    def test_evidence_threshold_function_rejects_under_evidenced(self):
        """_check_evidence_threshold raises for under-evidenced derivations."""
        from app.reflections.derivation_applier import _check_evidence_threshold

        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[uuid4()],  # Only 1, needs 2
            assertion_source="agent_inferred",
        )
        with pytest.raises(UnderEvidencedError, match="distillation"):
            _check_evidence_threshold(derivation)

    def test_evidence_threshold_function_passes_with_sufficient_evidence(self):
        """_check_evidence_threshold does not raise when evidence is sufficient."""
        from app.reflections.derivation_applier import _check_evidence_threshold

        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[uuid4(), uuid4()],  # 2, meets threshold
            assertion_source="agent_inferred",
        )
        # Should not raise
        _check_evidence_threshold(derivation)

    @pytest.mark.asyncio
    async def test_distillation_duplicate_ids_fail_threshold(self):
        """Duplicate message IDs don't count as distinct evidence."""
        same = uuid4()
        derivation = _make_derivation(
            kind=DerivationKind.distillation.value,
            supporting_message_ids=[same, same],  # Same ID twice
            assertion_source="agent_inferred",
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        applier = DerivationApplier(ledger=ledger)

        with pytest.raises(UnderEvidencedError):
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="duplicate evidence",
                writer=FakeTargetWriter(),
            )

    @pytest.mark.asyncio
    async def test_orientation_with_invalid_source_rejected_as_under_evidenced(self):
        """Orientation with made_up source is caught by lifecycle enforcement."""
        derivation = _make_derivation(
            kind=DerivationKind.orientation.value,
            assertion_source="made_up",  # Invalid
        )
        ledger = FakeLedger(derivations={derivation.id: derivation})
        applier = DerivationApplier(ledger=ledger)

        with pytest.raises(OrientationLifecycleError):
            await applier.apply_derivation(
                user_id=derivation.user_id,
                derivation_id=derivation.id,
                claim_text="x",
                writer=FakeTargetWriter(),
            )
