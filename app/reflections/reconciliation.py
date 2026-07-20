"""Correction reconciliation — settle ledgered derivations when a reflection is corrected.

Design contract (T16)
---------------------
When a reflection entry is corrected (an append-only revision ``E1 → E2``),
every derivation that was produced from the *superseded* entry ``E1`` is now
built on stale evidence.  Reconciliation settles those derivations in a way
that honours three invariants from the North Star and the locked design:

1. **No raw-evidence mutation.**  The correction itself already preserves the
   canonical raw messages (SD2); reconciliation never touches them either.

2. **No clobbering of independently maintained knowledge.**  Reconciliation
   **never performs a target write**.  An ``applied`` derivation already wrote
   its target row (memory / observation / distillation / orientation) via the
   existing write service, and that target may have been edited or superseded
   *independently* after the apply.  Reconciliation records the supersession on
   the **ledger row only** — the target row is left exactly as the owner left
   it.  This is the structural guarantee that the "subtle and costly regression"
   of overwriting an independently edited target cannot occur through the
   correction path.

3. **Auditable, append-style ledger decisions.**  Each settled derivation keeps
   its ``applied_target_table`` / ``applied_target_id`` coordinates (they are
   passed through to :meth:`DerivationLedger.update_decision` so the store does
   not null them out) so the superseded claim remains fully traceable to its
   target.  ``decided_at`` is advanced to the reconciliation timestamp, making
   each supersession a fresh, auditable ledger decision.  The full
   :class:`ReconciliationResult` records the per-derivation rationale.

4. **Idempotent across repeated corrections.**  Re-running reconciliation for
   the same superseded entry is a no-op: derivations already in the
   ``superseded`` decision are skipped.  Chained corrections (``E1 → E2 → E3``)
   each settle only the derivations linked to the entry they supersede.

Provenance is preserved throughout: a superseded derivation still points at its
reflection entry and source messages, and :meth:`DerivationLedger.traverse_provenance`
keeps working unchanged.

Schema version: 1
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable
from uuid import UUID

from app.reflections.derivation_ledger import DerivationLedger
from app.services.reflections import (
    ReflectionDerivation,
    ReflectionStore,
)

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────────────

#: Decisions that carry an existing target row.  Reconciliation must not write
#: a new target for any of these — it only settles the ledger row.
_APPLIED_DECISIONS: frozenset[str] = frozenset({"applied", "reinforced"})

#: Decisions that are already terminal and need no supersession action.
_TERMINAL_NO_TARGET: frozenset[str] = frozenset({"rejected", "superseded"})


# Machine-readable reason keys recorded on every ReconciliationAction.  These
# are stable identifiers (not free text) so audit consumers can filter/aggregate
# reconciliation decisions deterministically.
REASON_ALREADY_SUPERSEDED: str = "already_superseded"
"""The derivation was already superseded — repeated reconciliation is a no-op."""

REASON_ALREADY_REJECTED: str = "already_rejected"
"""The derivation was already rejected — terminal, nothing to settle."""

REASON_SUPERSEDED_NO_TARGET: str = "superseded_no_target"
"""A deferred derivation (no target row) is settled to superseded."""

REASON_SUPERSEDED_SOURCE_CORRECTED: str = "superseded_source_corrected"
"""An applied derivation whose target was NOT independently edited is settled
to superseded with its target coordinates preserved."""

REASON_TARGET_INDEPENDENTLY_EDITED: str = "target_independently_edited"
"""An applied derivation whose target row was edited *after* the apply.  The
target is preserved untouched (never clobbered); the ledger row is settled to
superseded with coordinates preserved and the independent-edit flag recorded."""

REASON_TARGET_MISSING: str = "target_missing"
"""An applied derivation whose target row no longer exists (deleted/retired
independently).  The ledger row is settled to superseded."""

REASON_UNKNOWN_DECISION: str = "unknown_decision_skipped"
"""Defensive: an unexpected decision value; reconciliation does not mutate it."""

REASON_NO_DERIVATIONS: str = "no_derivations"
"""No derivations were ledgered for the superseded entry."""


#: Best-available "last modified" column per durable target table.  These map
#: the ``applied_target_table`` value recorded on an applied derivation to the
#: column whose value advances when the target is edited independently of the
#: reconciliation process.  The probe is best-effort: for tables without an
#: edit-tracking column, the column simply cannot surface an independent edit,
#: and reconciliation falls back to preserving the target (the structural
#: guarantee holds regardless).
_DEFAULT_TARGET_TIMESTAMP_COLUMN: dict[str, str] = {
    "memories": "created_at",  # memories are append/ supersede-only
    "observations": "last_reinforced_at",  # reinforcement is an independent edit signal
    "distillations": "updated_at",
    "user_orientation_items": "updated_at",
}

#: Column used when a table is unknown to the probe — existence is still checked.
_EXISTENCE_ONLY_SENTINEL = "__existence_only__"


# ── Target edit probe ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TargetState:
    """Snapshot of a target knowledge row at reconciliation time.

    Attributes:
        exists: Whether the target row is still present in its table.  A row
            that was deleted or hard-retired reports ``exists=False``.
        last_changed_at: Best-available "last modified" timestamp for the row.
            ``None`` when the table has no edit-tracking column or the value is
            NULL.  Reconciliation compares this against the derivation's
            ``decided_at`` to detect independent edits.
    """

    exists: bool
    last_changed_at: datetime | None


@runtime_checkable
class TargetEditProbe(Protocol):
    """Read seam for inspecting a target knowledge row without writing to it.

    The reconciler uses this only to *audit* whether an applied target was
    touched after the derivation apply — it never writes through the probe.
    Tests inject a fake implementation; production uses
    :class:`PoolingTargetEditProbe`.
    """

    async def inspect_target(
        self, *, table: str, target_id: UUID
    ) -> TargetState:  # pragma: no cover - protocol body
        ...


class PoolingTargetEditProbe:
    """Default :class:`TargetEditProbe` backed by the shared asyncpg pool.

    Best-effort independent-edit detection: it reads the best-available
    "last modified" column for each known target table.  Unknown tables or
    NULL values simply yield ``last_changed_at=None`` (treated as "not
    independently edited"); the reconciliation still preserves the target row
    structurally because it never writes targets.

    The ``table`` argument is always one of the controlled values recorded on
    an applied derivation (``memories`` / ``observations`` / ``distillations`` /
    ``user_orientation_items``) — never user input — so it is safe to splice
    into the read query.
    """

    def __init__(
        self,
        pool: Any,
        table_columns: dict[str, str] | None = None,
    ) -> None:
        self._pool = pool
        self._columns: dict[str, str] = dict(_DEFAULT_TARGET_TIMESTAMP_COLUMN)
        if table_columns:
            self._columns.update(table_columns)

    async def inspect_target(self, *, table: str, target_id: UUID) -> TargetState:
        column = self._columns.get(table)
        if column is None:
            # Unknown table — existence check only.
            row = await self._pool.fetchrow(
                f"SELECT 1 AS present FROM {table} WHERE id = $1",
                target_id,
            )
            return TargetState(exists=row is not None, last_changed_at=None)

        row = await self._pool.fetchrow(
            f"SELECT {column} AS ts FROM {table} WHERE id = $1",
            target_id,
        )
        if row is None:
            return TargetState(exists=False, last_changed_at=None)
        ts = row["ts"]
        return TargetState(
            exists=True,
            last_changed_at=ts if isinstance(ts, datetime) else None,
        )


# ── Reconciliation result models ─────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReconciliationAction:
    """What the reconciler decided to do with a single derivation.

    Attributes:
        derivation_id: The settled derivation.
        derivation_kind: The derivation kind (memory/observation/...).
        previous_decision: The decision before reconciliation.
        new_decision: The decision after reconciliation, or ``None`` when the
            derivation was skipped (no mutation).
        target_table: The target table recorded on the derivation (if any),
            preserved for audit even after supersession.
        target_id: The target row id recorded on the derivation (if any).
        reason: A machine-readable reason key (one of the ``REASON_*``
            constants) explaining the decision.
        target_independently_edited: True when an applied target was edited
            after the apply and was therefore deliberately preserved.
        target_exists: Whether the target row still exists at reconciliation
            time.  ``None`` when no target was probed (deferred / skipped).
        skipped: True when no ledger mutation occurred (idempotent / terminal).
    """

    derivation_id: UUID
    derivation_kind: str
    previous_decision: str
    new_decision: str | None
    target_table: str | None
    target_id: UUID | None
    reason: str
    target_independently_edited: bool
    target_exists: bool | None
    skipped: bool


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    """Outcome of reconciling one correction.

    Attributes:
        corrected_entry_id: The new revision created by the correction (E2).
        superseded_entry_id: The prior revision that was superseded (E1).
        actions: Per-derivation decisions, in the order the derivations were
            listed (``created_at ASC``).
        reconciled_count: How many derivations had their decision advanced.
        skipped_count: How many derivations required no action (idempotent).
        independently_edited_count: How many applied targets were preserved
            because they had been edited after the apply.
    """

    corrected_entry_id: UUID
    superseded_entry_id: UUID
    actions: list[ReconciliationAction] = field(default_factory=list)
    reconciled_count: int = 0
    skipped_count: int = 0
    independently_edited_count: int = 0


# ── Pure decision function ───────────────────────────────────────────────────


def decide_reconciliation_action(
    derivation: ReflectionDerivation,
    target_state: TargetState | None,
) -> ReconciliationAction:
    """Decide — without performing any I/O — how to settle one derivation.

    This is the pure, fully testable core of reconciliation.  The engine
    performs the ledger mutation based on the returned ``new_decision``;
    targets are NEVER written here or anywhere in the reconciliation path.

    Args:
        derivation: The ledgered derivation derived from the superseded entry.
        target_state: Probed state of the derivation's target row, or ``None``
            when the derivation has no target (deferred) or probing is disabled.

    Returns:
        A :class:`ReconciliationAction` describing the (idempotent) decision.
    """
    previous = derivation.decision
    kind = derivation.derivation_kind
    target_table = derivation.applied_target_table
    target_id = derivation.applied_target_id

    # ── Already settled (idempotent re-entry) ─────────────────────────────
    if previous in _TERMINAL_NO_TARGET:
        reason = (
            REASON_ALREADY_SUPERSEDED
            if previous == "superseded"
            else REASON_ALREADY_REJECTED
        )
        return ReconciliationAction(
            derivation_id=derivation.id,
            derivation_kind=kind,
            previous_decision=previous,
            new_decision=None,
            target_table=target_table,
            target_id=target_id,
            reason=reason,
            target_independently_edited=False,
            target_exists=None,
            skipped=True,
        )

    # ── Deferred: never applied, no target row to guard ───────────────────
    if previous == "deferred":
        return ReconciliationAction(
            derivation_id=derivation.id,
            derivation_kind=kind,
            previous_decision=previous,
            new_decision="superseded",
            target_table=target_table,
            target_id=target_id,
            reason=REASON_SUPERSEDED_NO_TARGET,
            target_independently_edited=False,
            target_exists=None,
            skipped=False,
        )

    # ── Applied / reinforced: guard the target ───────────────────────────
    if previous in _APPLIED_DECISIONS:
        # No target coordinates recorded (shouldn't happen for an applied row,
        # but we treat it defensively as a missing target).
        if target_table is None or target_id is None:
            return ReconciliationAction(
                derivation_id=derivation.id,
                derivation_kind=kind,
                previous_decision=previous,
                new_decision="superseded",
                target_table=target_table,
                target_id=target_id,
                reason=REASON_TARGET_MISSING,
                target_independently_edited=False,
                target_exists=False,
                skipped=False,
            )

        if target_state is None:
            # No probe available — cannot detect independent edits.  Preserve
            # the target coordinates structurally and record the conservative
            # "source corrected" decision.
            return ReconciliationAction(
                derivation_id=derivation.id,
                derivation_kind=kind,
                previous_decision=previous,
                new_decision="superseded",
                target_table=target_table,
                target_id=target_id,
                reason=REASON_SUPERSEDED_SOURCE_CORRECTED,
                target_independently_edited=False,
                target_exists=None,
                skipped=False,
            )

        if not target_state.exists:
            return ReconciliationAction(
                derivation_id=derivation.id,
                derivation_kind=kind,
                previous_decision=previous,
                new_decision="superseded",
                target_table=target_table,
                target_id=target_id,
                reason=REASON_TARGET_MISSING,
                target_independently_edited=False,
                target_exists=False,
                skipped=False,
            )

        independently_edited = _was_independently_edited(
            decided_at=derivation.decided_at,
            target_changed_at=target_state.last_changed_at,
        )

        return ReconciliationAction(
            derivation_id=derivation.id,
            derivation_kind=kind,
            previous_decision=previous,
            new_decision="superseded",
            target_table=target_table,
            target_id=target_id,
            reason=(
                REASON_TARGET_INDEPENDENTLY_EDITED
                if independently_edited
                else REASON_SUPERSEDED_SOURCE_CORRECTED
            ),
            target_independently_edited=independently_edited,
            target_exists=True,
            skipped=False,
        )

    # ── Unknown decision: defensive no-op ────────────────────────────────
    return ReconciliationAction(
        derivation_id=derivation.id,
        derivation_kind=kind,
        previous_decision=previous,
        new_decision=None,
        target_table=target_table,
        target_id=target_id,
        reason=REASON_UNKNOWN_DECISION,
        target_independently_edited=False,
        target_exists=None,
        skipped=True,
    )


def _was_independently_edited(
    *,
    decided_at: datetime | None,
    target_changed_at: datetime | None,
) -> bool:
    """True when the target row advanced *strictly after* the derivation apply.

    Both timestamps are normalised to timezone-aware UTC before comparison so a
    naive ``decided_at`` (stored without tz) cannot falsely trigger.  ``None``
    on either side means "unknown" — reconciliation then conservatively reports
    *not* independently edited (the structural no-clobber guarantee holds
    regardless).
    """
    if decided_at is None or target_changed_at is None:
        return False

    decided = _as_utc(decided_at)
    changed = _as_utc(target_changed_at)

    # Strictly-after with a small epsilon guards against identical-microsecond
    # writes (apply + target create in the same instant is NOT an edit).
    return changed > decided


def _as_utc(ts: datetime) -> datetime:
    """Normalise a datetime to timezone-aware UTC for safe comparison."""
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


# ── Engine ───────────────────────────────────────────────────────────────────


class ReconciliationEngine:
    """Settles ledgered derivations after a reflection correction.

    The engine is the sole place where a correction triggers derivation
    settlement.  It enforces the contract documented at the top of this module:
    no target writes, preserved provenance, auditable decisions, idempotent
    re-entry.

    Usage::

        ledger = DerivationLedger(store)
        probe = PoolingTargetEditProbe(pool)
        engine = ReconciliationEngine(ledger, target_probe=probe)

        result = await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=old_entry.id,
            corrected_entry_id=new_entry.id,
        )
        assert result.reconciled_count >= 0  # always safe, even if 0
    """

    def __init__(
        self,
        ledger: DerivationLedger,
        target_probe: TargetEditProbe | None = None,
    ) -> None:
        self._ledger = ledger
        self._target_probe = target_probe

    @property
    def ledger(self) -> DerivationLedger:
        """The underlying DerivationLedger."""
        return self._ledger

    @property
    def target_probe(self) -> TargetEditProbe | None:
        """The configured target probe (or ``None`` when probing is disabled)."""
        return self._target_probe

    # ── Reconcile one correction ─────────────────────────────────────────

    async def reconcile_correction(
        self,
        *,
        user_id: UUID,
        superseded_entry_id: UUID,
        corrected_entry_id: UUID,
        processor_version: str | None = None,
    ) -> ReconciliationResult:
        """Settle every derivation derived from the superseded entry.

        For each derivation linked to ``superseded_entry_id``:

        * ``deferred`` → settled to ``superseded`` (no target existed).
        * ``applied``/``reinforced`` → target is probed; if it still exists and
          was NOT edited after the apply, settled to ``superseded`` with target
          coordinates preserved.  If it WAS edited independently, it is
          preserved untouched and the supersession records that fact.  If it no
          longer exists, settled to ``superseded`` (target missing).
        * ``superseded``/``rejected`` → skipped (idempotent / terminal).

        No target row is ever written.  Each settled derivation keeps its
        ``applied_target_table`` / ``applied_target_id`` so provenance survives.

        Args:
            user_id: Owner scope.
            superseded_entry_id: The entry that was superseded by the correction.
            corrected_entry_id: The new revision created by the correction.
            processor_version: Optional processor version stamp recorded on the
                supersession decisions.

        Returns:
            A :class:`ReconciliationResult` describing every decision.  Always
            returns (never raises on "no derivations"); a missing entry simply
            yields an empty action list.
        """
        derivations = await self._ledger.list_derivations_for_entry(
            user_id=user_id,
            reflection_entry_id=superseded_entry_id,
        )

        actions: list[ReconciliationAction] = []
        reconciled = 0
        skipped = 0
        independently_edited = 0

        for derivation in derivations:
            target_state = await self._probe_target_if_needed(derivation)
            action = decide_reconciliation_action(derivation, target_state)

            if not action.skipped and action.new_decision is not None:
                # Settle the ledger row, PRESERVING the target coordinates so
                # the store does not null them out.  The target row itself is
                # never touched — only the ledger decision advances.
                await self._ledger.update_decision(
                    user_id=user_id,
                    derivation_id=derivation.id,
                    decision=action.new_decision,
                    applied_target_table=derivation.applied_target_table,
                    applied_target_id=derivation.applied_target_id,
                    processor_version=processor_version,
                )
                reconciled += 1
            else:
                skipped += 1

            if action.target_independently_edited:
                independently_edited += 1

            actions.append(action)

        if not derivations:
            logger.info(
                "reconcile_correction: no derivations for superseded entry=%s "
                "(corrected_entry=%s)",
                superseded_entry_id,
                corrected_entry_id,
            )

        logger.info(
            "reconcile_correction: superseded_entry=%s corrected_entry=%s "
            "total=%s reconciled=%s skipped=%s independent_edits=%s",
            superseded_entry_id,
            corrected_entry_id,
            len(derivations),
            reconciled,
            skipped,
            independently_edited,
        )

        return ReconciliationResult(
            corrected_entry_id=corrected_entry_id,
            superseded_entry_id=superseded_entry_id,
            actions=actions,
            reconciled_count=reconciled,
            skipped_count=skipped,
            independently_edited_count=independently_edited,
        )

    # ── Provenance (delegated to the ledger) ─────────────────────────────

    async def traverse_provenance(
        self, *, user_id: UUID, derivation_id: UUID
    ):
        """Walk a (possibly superseded) derivation back to its source messages.

        Supersession does not break provenance: the derivation still points at
        its reflection entry and source messages, so the audit chain remains
        intact after reconciliation.
        """
        return await self._ledger.traverse_provenance(
            user_id=user_id, derivation_id=derivation_id
        )

    # ── Internals ────────────────────────────────────────────────────────

    async def _probe_target_if_needed(
        self, derivation: ReflectionDerivation
    ) -> TargetState | None:
        """Probe the target row only for applied derivations when a probe exists."""
        if derivation.decision not in _APPLIED_DECISIONS:
            return None
        if (
            derivation.applied_target_table is None
            or derivation.applied_target_id is None
        ):
            return None
        if self._target_probe is None:
            return None
        try:
            return await self._target_probe.inspect_target(
                table=derivation.applied_target_table,
                target_id=derivation.applied_target_id,
            )
        except Exception:  # noqa: BLE001 — probe failure must not abort reconciliation
            logger.warning(
                "reconcile_correction: target probe failed for "
                "table=%s id=%s; treating as not-independently-edited",
                derivation.applied_target_table,
                derivation.applied_target_id,
                exc_info=True,
            )
            return None


__all__ = [
    "PoolingTargetEditProbe",
    "REASON_ALREADY_REJECTED",
    "REASON_ALREADY_SUPERSEDED",
    "REASON_NO_DERIVATIONS",
    "REASON_SUPERSEDED_NO_TARGET",
    "REASON_SUPERSEDED_SOURCE_CORRECTED",
    "REASON_TARGET_INDEPENDENTLY_EDITED",
    "REASON_TARGET_MISSING",
    "REASON_UNKNOWN_DECISION",
    "ReconciliationAction",
    "ReconciliationEngine",
    "ReconciliationResult",
    "TargetEditProbe",
    "TargetState",
]
