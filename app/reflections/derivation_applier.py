"""Derivation applier — reuse existing knowledge write services for accepted derivations.

Design contract (T14)
---------------------
Accepted derivations (decision ``deferred`` → ``applied``) MUST reuse the existing
semantic write services — ``add_memory``, ``log_observation``, ``add_distillation``,
and ``create_orientation_item`` — rather than introducing a parallel write path.
Derivations of *forbidden* kinds (actions, tasks, reminders, follow-ups, ...) are
rejected before any target write is attempted (see SD3).

Claim text
~~~~~~~~~~
The persisted :class:`ReflectionDerivation` ledger row stores the candidate's
``assertion_source`` (a category: user_explicit / user_implied / agent_inferred)
and its ``supporting_message_ids``, but the candidate's human-readable ``summary``
is **not** persisted on the row (the encrypted payload slot is reserved but left
empty by the ledger).  The applier therefore receives the claim text explicitly
as the ``claim_text`` parameter from the caller that holds the original
:class:`DerivationCandidate`.  This keeps the applier from fabricating content
(SD1) while still reusing the existing write services verbatim.

Transaction / compensation resolution
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The target write (e.g. ``INSERT INTO memories``) and the ledger mark
(``UPDATE reflection_derivations SET decision='applied'``) run through two
separate write authorities — the write tools own their SQL via ``ctx.pool`` while
the ledger goes through ``ReflectionStore``.  They **cannot share a single
transaction**.  We resolve the resulting dual-write hazard with an explicit,
auditable compensation contract rather than an impossible shared transaction:

1. **Target-write-first ordering.**  The knowledge target is written first, the
   ledger row is marked applied second.  This guarantees we never record an
   ``applied`` decision for a target that does not exist (no "phantom apply").

2. **Idempotent re-entry.**  Re-applying a derivation whose ``decision`` is
   already ``applied`` is a **no-op**: the existing ``applied_target_table`` /
   ``applied_target_id`` are returned and the target write service is **never
   invoked again**.  This is what guarantees *no duplicate target writes* on the
   common retry paths (retry after success, replay, concurrent re-entry).

3. **Target-write failure is safe to retry.**  If the target write service raises,
   the ledger row is left untouched (still ``deferred``) and no target row was
   created, so a later ``apply_derivation`` call will perform the write exactly
   once.

4. **Partial-failure surfacing.**  If the target write succeeded but the ledger
   mark fails, the applier raises :class:`DerivationApplyPartialFailure`
   carrying the orphan ``target_table`` / ``target_id``.  The caller MUST NOT
   blindly retry ``apply_derivation`` after this exception — that could create a
   duplicate target row.  Instead the orphan is reconciled via
   :meth:`DerivationApplier.reconcile_after_partial_failure`, which closes the
   gap by marking the derivation applied with the orphan coordinates **without
   performing any further target write**.  This is the standard saga
   compensation shape: the two writes are not atomic, but every outcome is
   auditable and recoverable without duplication.

Every applied derivation remains fully traversable back to its source evidence
through :meth:`DerivationLedger.traverse_provenance`.

Schema version: 1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from app.reflections.derivation import _FORBIDDEN_KINDS, DerivationKind
from app.reflections.derivation_ledger import DerivationLedger, ProvenanceChain
from app.services.reflections import (
    DerivationNotFoundError,
    ReflectionDerivation,
)
from app.services.tools.write_tools import (
    add_distillation,
    add_memory,
    create_orientation_item,
    log_observation,
)
from tool_schemas import (
    AddDistillationInput,
    AddMemoryInput,
    Confidence,
    CreateOrientationItemInput,
    DistillationSensitivity,
    DistillationVisibility,
    LogObservationInput,
    OrientationKind,
    OrientationSource,
)


# ── Target table registry ───────────────────────────────────────────────────
#
# Maps each allowed derivation kind to the durable table its target write lands
# in.  This is recorded on the ledger row as ``applied_target_table`` so any
# applied claim can be located and audited.

_TARGET_TABLES: dict[str, str] = {
    DerivationKind.memory.value: "memories",
    DerivationKind.observation.value: "observations",
    DerivationKind.distillation.value: "distillations",
    DerivationKind.orientation.value: "user_orientation_items",
}

#: Decisions that already carry an existing target row — re-applying a
#: derivation in any of these states must NOT trigger another target write.
_APPLIED_DECISIONS: frozenset[str] = frozenset({"applied", "reinforced"})

# ── Evidence thresholds per derivation kind ──────────────────────────────────
#
# Each knowledge type has a minimum number of distinct supporting message IDs
# required before a target write can proceed.  The eligibility gate in
# ``derivation.py`` already enforces these, but the applier double-checks
# at apply time as a defence-in-depth measure.

_MIN_EVIDENCE_BY_KIND: dict[str, int] = {
    DerivationKind.memory.value: 1,
    DerivationKind.observation.value: 1,
    DerivationKind.distillation.value: 2,  # multi-evidence requirement
    DerivationKind.orientation.value: 1,
}
"""Minimum distinct supporting message IDs required per derivation kind."""


def _check_evidence_threshold(derivation: ReflectionDerivation) -> None:
    """Enforce the per-kind evidence threshold before any target write.

    Raises:
        UnderEvidencedError: If the derivation has insufficient evidence for
            its kind.
    """
    kind = derivation.derivation_kind
    minimum = _MIN_EVIDENCE_BY_KIND.get(kind)
    if minimum is None:
        return  # Unknown kind — let the kind gate catch it later.

    supporting = derivation.supporting_message_ids or []
    distinct = len(set(supporting))

    if distinct < minimum:
        raise UnderEvidencedError(
            f"Derivation kind '{kind}' requires at least {minimum} distinct "
            f"supporting message IDs (multi-evidence requirement); got {distinct}. "
            f"The derivation stays deferred until sufficient evidence is available."
        )


def _enforce_orientation_lifecycle(derivation: ReflectionDerivation) -> None:
    """Enforce the bot_proposed → pending/unreviewed orientation lifecycle.

    Derived orientation items MUST follow the reviewed/proposed lifecycle:
    - Source MUST be ``bot_proposed`` (derivations cannot claim user_stated
      or user_confirmed — only explicit user action can confirm).
    - Status starts as ``pending``, review_state as ``unreviewed`` — the
      orientation is hidden from Compass until the user explicitly reviews it.

    This function verifies the derivation's *intent* by checking that the
    ``assertion_source`` is ``agent_inferred`` (the only source that makes
    sense for a bot-proposed orientation).  The actual write-service input
    always uses ``OrientationSource.bot_proposed`` (see
    ``_build_orientation_input``), so this is a defence-in-depth check.

    Raises:
        OrientationLifecycleError: If the derivation attempts to bypass
            the lifecycle.
    """
    # A derivation-derived orientation MUST be agent_inferred — if someone
    # claims user_explicit for an orientation derivation, something is off.
    # The derivation engine produces orientation candidates with
    # assertion_source=agent_inferred by design.
    source = derivation.assertion_source
    if source not in ("agent_inferred", "user_implied"):
        raise OrientationLifecycleError(
            f"Orientation derivations must have assertion_source 'agent_inferred' "
            f"or 'user_implied'; got '{source}'.  Orientations derived from "
            f"reflections start as bot_proposed (pending/unreviewed) and require "
            f"explicit user review before becoming Compass-visible."
        )


class DerivationApplyError(RuntimeError):
    """Base class for derivation-apply failures."""


class ForbiddenDerivationKindError(DerivationApplyError):
    """Raised when an apply is attempted for a forbidden derivation kind.

    Per SD3, actions/tasks/reminders/follow-ups are never derivable.  The
    applier rejects them before any target write is attempted.
    """


class UnsupportedDerivationKindError(DerivationApplyError):
    """Raised when an apply is attempted for an unknown/unsupported kind."""


class UnderEvidencedError(DerivationApplyError):
    """Raised when a derivation lacks sufficient evidence for its kind.

    Each knowledge type has a minimum evidence threshold enforced before
    any target write is attempted:

    * **distillation**: requires ≥2 distinct supporting message IDs
      (multi-evidence requirement — a single message cannot constitute a
      synthesized pattern across evidence signals).
    * **observation**: requires ≥1 supporting message ID (the baseline
      evidence gate already enforce this in eligibility).

    This error is raised BEFORE any target write, so the ledger row stays
    ``deferred`` and the caller can retry with more evidence.
    """


class OrientationLifecycleError(DerivationApplyError):
    """Raised when an orientation derivation attempts to bypass the lifecycle.

    Derived orientation items MUST follow the bot_proposed → pending/unreviewed
    lifecycle.  They CANNOT be auto-set to active/reviewed.  This ensures
    derivations never silently add Compass-visible headings without explicit
    user review.
    """


class DerivationApplyPartialFailure(DerivationApplyError):
    """Target write succeeded but the ledger mark failed — orphan created.

    The two writes (target insert, ledger update) cannot share a transaction.
    When the target write commits but the ledger mark does not, an *orphan*
    target row exists while the derivation row still reads ``deferred``.  Blindly
    retrying :meth:`DerivationApplier.apply_derivation` would re-run the target
    write and create a **duplicate** target row, so the caller MUST reconcile the
    orphan instead via
    :meth:`DerivationApplier.reconcile_after_partial_failure`.

    Attributes:
        derivation_id: The derivation that was partially applied.
        target_table: The durable table the orphan row was written to.
        target_id: The orphan row's UUID.
        cause: The original exception raised by the ledger mark.
    """

    def __init__(
        self,
        *,
        derivation_id: UUID,
        target_table: str,
        target_id: UUID,
        cause: BaseException,
    ) -> None:
        self.derivation_id = derivation_id
        self.target_table = target_table
        self.target_id = target_id
        self.cause = cause
        super().__init__(
            f"Partial apply of derivation {derivation_id}: target row written "
            f"to {target_table} (id={target_id}) but ledger mark failed: {cause!r}. "
            f"Reconcile via reconcile_after_partial_failure(); do NOT blindly retry "
            f"apply_derivation()."
        )


# ── Result model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApplyResult:
    """Outcome of applying a derivation.

    Attributes:
        derivation_id: The derivation that was processed.
        target_table: The durable table the target row lives in (if any).
        target_id: The target row UUID (if any).
        already_applied: True when no target write occurred because the
            derivation was already in an applied state (idempotent re-entry) or
            the row was reconciled without a fresh write.
    """

    derivation_id: UUID
    target_table: str
    target_id: UUID | None
    already_applied: bool


# ── Target writer protocol ───────────────────────────────────────────────────


@runtime_checkable
class TargetWriter(Protocol):
    """Write seam for the four allowed derivation target services.

    The default concrete implementation,
    :class:`TurnContextTargetWriter`, delegates each method to the existing
    memory/observation/distillation/orientation write services.  Tests and
    integration callers may substitute a different implementation of this
    protocol to inject retry/partial-failure behaviour.

    Each method receives the ledgered derivation plus the human-readable
    ``claim_text`` (the candidate summary, not persisted on the ledger row).
    """

    async def write_memory(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:  # noqa: D102
        ...

    async def write_observation(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:  # noqa: D102
        ...

    async def write_distillation(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:  # noqa: D102
        ...

    async def write_orientation(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:  # noqa: D102
        ...


# ── Confidence mapping ───────────────────────────────────────────────────────


def confidence_to_enum(confidence: float | None) -> Confidence:
    """Map a derivation confidence in [0, 1] to the write-service Confidence enum.

    Conservative bucketing: anything below 0.4 is ``low``, below 0.7 ``medium``,
    otherwise ``high``.  ``None`` defaults to ``medium`` (the write-service
    default) rather than fabricating a high confidence.
    """
    if confidence is None:
        return Confidence.medium
    if confidence >= 0.7:
        return Confidence.high
    if confidence >= 0.4:
        return Confidence.medium
    return Confidence.low


def _orientation_kind_from_detail(detail: dict[str, Any] | None) -> OrientationKind:
    """Resolve an OrientationKind from optional candidate detail.

    Defaults to ``principle`` (which needs neither ``priority_rank`` nor
    ``target_date``) so a derivation never violates the
    ``CreateOrientationItemInput`` kind constraints unless the caller explicitly
    supplies the required fields.
    """
    if not detail:
        return OrientationKind.principle
    raw = detail.get("orientation_kind") or detail.get("kind")
    if not raw:
        return OrientationKind.principle
    try:
        return OrientationKind(raw)
    except ValueError:
        return OrientationKind.principle


def _build_memory_input(
    derivation: ReflectionDerivation, claim_text: str
) -> AddMemoryInput:
    """Build the memory write-service input from a derivation.

    The candidate ``claim_text`` IS the knowledge claim, recorded verbatim as
    the memory ``content``.  Visibility stays ``private`` (conservative); the
    derivation never upgrades a claim to dyad-shareable on its own.
    """
    return AddMemoryInput(
        about_user_id=derivation.user_id,
        content=claim_text,
        visibility=DistillationVisibility.private,
    )


def _build_observation_input(
    derivation: ReflectionDerivation, claim_text: str
) -> LogObservationInput:
    """Build the observation write-service input from a derivation."""
    return LogObservationInput(
        content=claim_text,
        about_user_id=derivation.user_id,
        confidence=confidence_to_enum(derivation.confidence),
        supporting_message_ids=list(derivation.supporting_message_ids or []),
        # significance left None — let the existing scorer run rather than
        # fabricating a significance from the derivation.
    )


def _build_distillation_input(
    derivation: ReflectionDerivation, claim_text: str
) -> AddDistillationInput:
    """Build the distillation write-service input from a derivation.

    Distillations require at least one supporting link; the derivation's
    ``supporting_message_ids`` satisfy that evidence gate.
    """
    return AddDistillationInput(
        content=claim_text,
        confidence=confidence_to_enum(derivation.confidence),
        sensitivity=DistillationSensitivity.medium,
        visibility=DistillationVisibility.private,
        source_user_ids=[derivation.user_id],
        supporting_message_ids=list(derivation.supporting_message_ids or []),
    )


def _build_orientation_input(
    derivation: ReflectionDerivation, claim_text: str, detail: dict[str, Any] | None
) -> CreateOrientationItemInput:
    """Build the orientation write-service input from a derivation.

    Derived orientation items are recorded as ``bot_proposed`` so they start
    pending/unreviewed and require explicit user review before becoming
    Compass-visible (conservative — a reflection never silently adds a Compass
    heading).
    """
    kind = _orientation_kind_from_detail(detail)
    kwargs: dict[str, Any] = {
        "kind": kind,
        "label": (claim_text[:200] or "Derived orientation"),
        "source": OrientationSource.bot_proposed,
    }
    # Only forward fields the chosen kind allows; otherwise the input validator
    # raises (e.g. priority requires priority_rank, manifestation requires
    # target_date).  The detail dict is the only place those come from.
    if detail:
        if kind == OrientationKind.priority and detail.get("priority_rank") is not None:
            kwargs["priority_rank"] = detail["priority_rank"]
        if (
            kind == OrientationKind.manifestation
            and detail.get("target_date") is not None
        ):
            kwargs["target_date"] = detail["target_date"]
        if detail.get("detail"):
            kwargs["detail"] = detail["detail"]
    return CreateOrientationItemInput(**kwargs)


# ── Concrete target writer (reuses existing write services) ──────────────────


class TurnContextTargetWriter:
    """Concrete :class:`TargetWriter` that delegates to the existing write tools.

    Each method maps a ledgered :class:`ReflectionDerivation` (plus its
    ``claim_text``) to the appropriate write-service input model and calls the
    *existing* ``add_memory`` / ``log_observation`` / ``add_distillation`` /
    ``create_orientation_item`` handler.  No parallel write path is introduced —
    derivations flow through exactly the same scope checks, encryption, embedding
    sync, and tool-call audit as an interactive tool call.

    The ``ctx`` is provided by the caller (e.g. a worker that builds a
    ``TurnContext`` from the derivation's user/bot/topic scope), so the applier
    itself never constructs turn context and stays free of agentic-loop wiring.
    """

    def __init__(self, ctx: Any, orientation_detail: dict[str, Any] | None = None) -> None:
        self._ctx = ctx
        self._orientation_detail = orientation_detail

    async def write_memory(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:
        result = await add_memory(self._ctx, _build_memory_input(derivation, claim_text))
        return result.id

    async def write_observation(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:
        result = await log_observation(
            self._ctx, _build_observation_input(derivation, claim_text)
        )
        return result.id

    async def write_distillation(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:
        result = await add_distillation(
            self._ctx, _build_distillation_input(derivation, claim_text)
        )
        return result.id

    async def write_orientation(
        self, derivation: ReflectionDerivation, claim_text: str
    ) -> UUID:
        result = await create_orientation_item(
            self._ctx,
            _build_orientation_input(
                derivation, claim_text, self._orientation_detail
            ),
        )
        return result.id


# ── Applier ──────────────────────────────────────────────────────────────────


def _resolve_writer_fn(writer: TargetWriter, kind: str):
    """Return the writer coroutine for a derivation kind, or raise."""
    if kind in _FORBIDDEN_KINDS:
        raise ForbiddenDerivationKindError(
            f"Derivation kind {kind!r} is forbidden; actions/tasks/reminders/"
            f"follow-ups are never applied (SD3)."
        )
    mapping = {
        DerivationKind.memory.value: writer.write_memory,
        DerivationKind.observation.value: writer.write_observation,
        DerivationKind.distillation.value: writer.write_distillation,
        DerivationKind.orientation.value: writer.write_orientation,
    }
    fn = mapping.get(kind)
    if fn is None:
        raise UnsupportedDerivationKindError(
            f"Derivation kind {kind!r} has no target write service."
        )
    return fn


def _target_table_for(kind: str) -> str:
    """Return the durable target table name for a derivation kind."""
    if kind in _FORBIDDEN_KINDS:
        raise ForbiddenDerivationKindError(
            f"Derivation kind {kind!r} is forbidden (SD3)."
        )
    table = _TARGET_TABLES.get(kind)
    if table is None:
        raise UnsupportedDerivationKindError(
            f"Derivation kind {kind!r} has no target table."
        )
    return table


class DerivationApplier:
    """Applies accepted derivations via the existing knowledge write services.

    The applier is the sole place where a ledgered derivation transitions from
    ``deferred`` to ``applied``.  It enforces the compensation contract
    documented at the top of this module: target-write-first ordering, idempotent
    re-entry (no duplicate target writes), and explicit orphan reconciliation on
    partial failure.

    Usage::

        applier = DerivationApplier(ledger=DerivationLedger(store))
        result = await applier.apply_derivation(
            user_id=user_id,
            derivation_id=derivation.id,
            claim_text=candidate.summary,
            writer=TurnContextTargetWriter(ctx),
        )
        if result.already_applied:
            ...  # no-op, target already existed
    """

    def __init__(self, ledger: DerivationLedger) -> None:
        self._ledger = ledger

    @property
    def ledger(self) -> DerivationLedger:
        """The underlying DerivationLedger."""
        return self._ledger

    # ── Apply ────────────────────────────────────────────────────────────

    async def apply_derivation(
        self,
        *,
        user_id: UUID,
        derivation_id: UUID,
        claim_text: str,
        writer: TargetWriter,
        processor_version: str | None = None,
    ) -> ApplyResult:
        """Apply one derivation via its target write service.

        See the module docstring for the full compensation contract.  In short:

        * If the derivation is already applied, return immediately **without**
          invoking the writer (idempotent — no duplicate target write).
        * Otherwise write the target first, then mark the ledger applied.
        * If the target write succeeds but the ledger mark fails, raise
          :class:`DerivationApplyPartialFailure` with the orphan coordinates;
          reconcile via :meth:`reconcile_after_partial_failure`.

        Args:
            user_id: Owner scope.
            derivation_id: The derivation to apply.
            claim_text: The human-readable claim (candidate summary) to record
                as the target's content.  Supplied by the caller that holds the
                original candidate; not read from the ledger row.
            writer: The :class:`TargetWriter` (e.g. TurnContextTargetWriter).
            processor_version: Optional processor version stamp.

        Returns:
            :class:`ApplyResult` describing the outcome.

        Raises:
            DerivationNotFoundError: The derivation doesn't exist for user_id.
            ForbiddenDerivationKindError: The kind is a forbidden action/task/...
            UnsupportedDerivationKindError: The kind has no target service.
            DerivationApplyPartialFailure: Target written, ledger mark failed.
        """
        derivation = await self._ledger.get_derivation(
            user_id=user_id, derivation_id=derivation_id
        )
        if derivation is None:
            raise DerivationNotFoundError(
                f"Derivation {derivation_id} not found for user {user_id}"
            )

        kind = derivation.derivation_kind
        target_table = _target_table_for(kind)

        # ── Idempotent re-entry ────────────────────────────────────────
        # An already-applied (or reinforced) derivation has an existing target
        # row; re-applying MUST NOT call the writer again.
        if derivation.decision in _APPLIED_DECISIONS:
            return ApplyResult(
                derivation_id=derivation.id,
                target_table=target_table,
                target_id=derivation.applied_target_id,
                already_applied=True,
            )

        # ── Evidence threshold enforcement ─────────────────────────────
        # Defence-in-depth check: even though the eligibility gate in
        # derivation.py already filters under-evidenced candidates, the
        # applier re-verifies at apply time.  This is especially important
        # for distillations (multi-evidence requirement: ≥2 messages).
        _check_evidence_threshold(derivation)

        # ── Orientation lifecycle enforcement ──────────────────────────
        # Derived orientation items MUST start as bot_proposed →
        # pending/unreviewed.  The applier verifies this invariant before
        # any write.  Derivations that attempt to bypass the lifecycle
        # (e.g. by supplying a source other than bot_proposed) are rejected.
        if kind == DerivationKind.orientation.value:
            _enforce_orientation_lifecycle(derivation)

        # ── Target write (first) ───────────────────────────────────────
        # Forbidden kinds never reach the writer — rejected up front.
        write_fn = _resolve_writer_fn(writer, kind)
        target_id = await write_fn(derivation, claim_text)

        # ── Ledger mark (second) ───────────────────────────────────────
        # If this fails we have an orphan target row.  Surface it explicitly so
        # the caller reconciles instead of blindly retrying (which would write a
        # duplicate target).
        try:
            await self._ledger.update_decision(
                user_id=user_id,
                derivation_id=derivation.id,
                decision="applied",
                applied_target_table=target_table,
                applied_target_id=target_id,
                processor_version=processor_version,
            )
        except Exception as exc:  # noqa: BLE001 — surface any ledger failure
            raise DerivationApplyPartialFailure(
                derivation_id=derivation.id,
                target_table=target_table,
                target_id=target_id,
                cause=exc,
            ) from exc

        return ApplyResult(
            derivation_id=derivation.id,
            target_table=target_table,
            target_id=target_id,
            already_applied=False,
        )

    # ── Compensation / reconciliation ────────────────────────────────────

    async def reconcile_after_partial_failure(
        self,
        *,
        user_id: UUID,
        derivation_id: UUID,
        orphan_target_table: str,
        orphan_target_id: UUID,
        processor_version: str | None = None,
    ) -> ApplyResult:
        """Close the gap left by a :class:`DerivationApplyPartialFailure`.

        After a partial failure, an orphan target row exists while the ledger
        row still reads ``deferred``.  This method marks the derivation applied
        with the orphan coordinates **without performing any further target
        write**, so the orphan becomes the canonical applied target and a
        subsequent :meth:`apply_derivation` call will be a no-op.

        If the derivation turns out to already be applied (e.g. a concurrent
        apply raced ahead), the existing target coordinates are returned and no
        ledger mutation occurs.

        Args:
            user_id: Owner scope.
            derivation_id: The partially-applied derivation.
            orphan_target_table: The orphan row's durable table.
            orphan_target_id: The orphan row's UUID.
            processor_version: Optional processor version stamp.

        Returns:
            :class:`ApplyResult` with ``already_applied=True`` (no target write
            occurred).

        Raises:
            DerivationNotFoundError: The derivation doesn't exist for user_id.
        """
        derivation = await self._ledger.get_derivation(
            user_id=user_id, derivation_id=derivation_id
        )
        if derivation is None:
            raise DerivationNotFoundError(
                f"Derivation {derivation_id} not found for user {user_id}"
            )

        # Already reconciled/applied by a concurrent caller — return existing.
        if derivation.decision in _APPLIED_DECISIONS:
            return ApplyResult(
                derivation_id=derivation.id,
                target_table=derivation.applied_target_table or orphan_target_table,
                target_id=derivation.applied_target_id,
                already_applied=True,
            )

        await self._ledger.update_decision(
            user_id=user_id,
            derivation_id=derivation.id,
            decision="applied",
            applied_target_table=orphan_target_table,
            applied_target_id=orphan_target_id,
            processor_version=processor_version,
        )
        return ApplyResult(
            derivation_id=derivation.id,
            target_table=orphan_target_table,
            target_id=orphan_target_id,
            already_applied=True,
        )

    # ── Provenance (delegated to the ledger) ─────────────────────────────

    async def traverse_provenance(
        self, *, user_id: UUID, derivation_id: UUID
    ) -> ProvenanceChain:
        """Walk an applied (or any) derivation back to its source messages.

        Delegates to :meth:`DerivationLedger.traverse_provenance` so an applied
        derivation remains fully auditable: target row ← derivation ← entry ←
        session ← source messages.
        """
        return await self._ledger.traverse_provenance(
            user_id=user_id, derivation_id=derivation_id
        )


__all__ = [
    "ApplyResult",
    "DerivationApplier",
    "DerivationApplyError",
    "DerivationApplyPartialFailure",
    "ForbiddenDerivationKindError",
    "OrientationLifecycleError",
    "TargetWriter",
    "TurnContextTargetWriter",
    "UnderEvidencedError",
    "UnsupportedDerivationKindError",
    "confidence_to_enum",
]
