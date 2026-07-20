"""Derivation ledger — auditable trace from derivation candidate to persistence.

Design contract (T13)
---------------------
Every derived claim must remain traceable to the reflection entry and its
source messages.  The ledger records each derivation decision with:

* **target** — derivation_kind (memory, observation, distillation, orientation)
* **assertion_source** — user_explicit, user_implied, or agent_inferred
* **confidence** — [0.0, 1.0] score
* **eligibility_reasons** — deterministic rule keys that fired
* **supporting_message_ids** — exact message IDs within the entry

The ledger is **idempotent**: retried submissions with the same
``idempotency_key`` return the existing derivation rather than creating a
duplicate.  Idempotency keys are deterministic SHA-256 digests of the
canonical candidate fields (entry_id, kind, source, summary, supporting
message IDs).

**Provenance traversal** walks the full chain:
``derivation → reflection_entry → reflection_session → source_message_ids``,
so every claim can be audited back to the original evidence.

This module bridges the pure derivation logic (``derivation.py``) with the
persistence layer (``ReflectionStore`` in ``reflections.py``).  It is the
sole integration seam for recording derivation decisions.

Schema version: 1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

from app.reflections.derivation import DerivationCandidate, DerivationResult
from app.services.reflections import (
    DerivationNotFoundError,
    ReflectionDerivation,
    ReflectionEntry,
    ReflectionSession,
    ReflectionStore,
)


# ── Provenance chain ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProvenanceChain:
    """Complete auditable trace from a derivation back to source messages.

    Every derivation ledger entry can be traversed back through:
    1. The derivation itself (kind, assertion_source, confidence, reasons)
    2. The reflection entry it was derived from (summary, payload)
    3. The session that collected the evidence (temporal scope, phase)
    4. The canonical source message IDs

    If a link in the chain cannot be resolved (e.g. entry was deleted),
    the corresponding field is ``None`` and ``is_complete`` is ``False``.
    """

    derivation: ReflectionDerivation
    """The ledgered derivation record."""

    entry: ReflectionEntry | None
    """The reflection entry this derivation was produced from."""

    session: ReflectionSession | None
    """The session that collected the source messages."""

    source_message_ids: list[UUID] = field(default_factory=list)
    """Canonical source message IDs (from session.source_message_ids)."""

    @property
    def is_complete(self) -> bool:
        """True when all links in the chain are resolvable."""
        return self.entry is not None and self.session is not None


# ── Idempotency key construction ─────────────────────────────────────────────


def build_idempotency_key(
    entry_id: UUID,
    kind: str,
    assertion_source: str,
    summary: str,
    supporting_message_ids: list[UUID],
) -> str:
    """Build a deterministic idempotency key for a derivation candidate.

    The key is a SHA-256 hex digest of the canonical fields that uniquely
    identify a derivation.  Same inputs always produce the same key, making
    retry-safe ledgering possible regardless of concurrent callers.

    Args:
        entry_id: The reflection entry UUID.
        kind: DerivationKind value (memory, observation, etc.).
        assertion_source: AssertionSource value.
        summary: The candidate's human-readable summary.
        supporting_message_ids: Ordered supporting message UUIDs.

    Returns:
        A 64-character hex digest usable as an idempotency_key.
    """
    sorted_ids = sorted(str(mid) for mid in supporting_message_ids)
    raw = (
        f"{entry_id}|{kind}|{assertion_source}|{summary}|"
        f"{','.join(sorted_ids)}"
    )
    return sha256(raw.encode("utf-8")).hexdigest()


# ── Derivation ledger ────────────────────────────────────────────────────────


class DerivationLedger:
    """Records derivation candidates as auditable, idempotent ledger entries.

    The ledger bridges the pure derivation engine (``derivation.py``) with the
    persistence layer (``ReflectionStore``).  Every recorded derivation is
    traceable back to its reflection entry and source messages.

    **Idempotency**: the ledger constructs a deterministic ``idempotency_key``
    for each candidate.  Retried submissions with the same key return the
    existing derivation rather than creating a duplicate.

    **Provenance**: ``traverse_provenance()`` walks the full chain from
    derivation → entry → session → source messages.

    Usage::

        store = ReflectionStore(pool)
        ledger = DerivationLedger(store)

        # Record all eligible candidates from a DerivationResult
        derivations = await ledger.record_candidates(
            user_id=user_id,
            reflection_entry_id=entry.id,
            derivation_result=result,
        )

        # Trace a derivation back to its evidence
        chain = await ledger.traverse_provenance(
            user_id=user_id,
            derivation_id=derivations[0].id,
        )
        assert chain.is_complete
        assert len(chain.source_message_ids) > 0
    """

    def __init__(self, store: ReflectionStore) -> None:
        self._store = store

    @property
    def store(self) -> ReflectionStore:
        """The underlying ReflectionStore (for direct access if needed)."""
        return self._store

    # ── Record candidates ────────────────────────────────────────────────

    async def record_candidates(
        self,
        *,
        user_id: UUID,
        reflection_entry_id: UUID,
        derivation_result: DerivationResult,
        processor_version: str | None = None,
        processor_turn_id: UUID | None = None,
    ) -> list[ReflectionDerivation]:
        """Record all **eligible** candidates from a derivation result.

        Each candidate is recorded as a separate ledger entry with a
        deterministic ``idempotency_key``.  **Rejected** candidates (those
        that failed the eligibility gate) are NOT recorded — only eligible
        candidates become ledger entries.

        Args:
            user_id: Owner scope for the derivations.
            reflection_entry_id: The reflection entry these derive from.
            derivation_result: Result from ``DerivationEngine.produce_candidates()``.
            processor_version: Optional processor version string.
            processor_turn_id: Optional turn ID of the processing agent.

        Returns:
            List of created (or idempotently returned) ``ReflectionDerivation``
            records, in the same order as ``derivation_result.candidates``.
        """
        recorded: list[ReflectionDerivation] = []

        for candidate in derivation_result.candidates:
            derivation = await self.record_candidate(
                user_id=user_id,
                reflection_entry_id=reflection_entry_id,
                candidate=candidate,
                processor_version=processor_version,
                processor_turn_id=processor_turn_id,
            )
            recorded.append(derivation)

        return recorded

    async def record_candidate(
        self,
        *,
        user_id: UUID,
        reflection_entry_id: UUID,
        candidate: DerivationCandidate,
        processor_version: str | None = None,
        processor_turn_id: UUID | None = None,
    ) -> ReflectionDerivation:
        """Record a single derivation candidate as a ledger entry.

        Constructs a deterministic ``idempotency_key`` from the candidate's
        canonical fields.  Retries with the same key return the existing
        derivation without creating a duplicate.

        Args:
            user_id: Owner scope.
            reflection_entry_id: The reflection entry.
            candidate: A single eligible ``DerivationCandidate``.
            processor_version: Optional processor version.
            processor_turn_id: Optional turn ID.

        Returns:
            The created or existing ``ReflectionDerivation``.

        Raises:
            EntryNotFoundError: If the reflection entry doesn't exist or
                isn't owned by user_id.
            DerivationIdempotencyConflictError: If an idempotency key
                collision cannot be resolved (should not happen in practice).
        """
        idempotency_key = build_idempotency_key(
            entry_id=reflection_entry_id,
            kind=candidate.kind.value,
            assertion_source=candidate.assertion_source.value,
            summary=candidate.summary,
            supporting_message_ids=candidate.supporting_message_ids,
        )

        return await self._store.create_derivation(
            user_id=user_id,
            reflection_entry_id=reflection_entry_id,
            derivation_kind=candidate.kind.value,
            assertion_source=candidate.assertion_source.value,
            candidate_payload_encrypted=None,
            confidence=candidate.confidence,
            eligibility_reasons=candidate.eligibility_reasons,
            supporting_message_ids=candidate.supporting_message_ids,
            decision="deferred",
            applied_target_table=None,
            applied_target_id=None,
            processor_version=processor_version,
            processor_turn_id=processor_turn_id,
            idempotency_key=idempotency_key,
        )

    # ── Provenance traversal ─────────────────────────────────────────────

    async def traverse_provenance(
        self,
        *,
        user_id: UUID,
        derivation_id: UUID,
    ) -> ProvenanceChain:
        """Walk the provenance chain from derivation back to source messages.

        Traversal path:
          1. Fetch the derivation by ID (scoped to *user_id*).
          2. Fetch the reflection entry it belongs to.
          3. Fetch the session the entry belongs to.
          4. Collect ``source_message_ids`` from the session (the canonical
             evidence set).

        Args:
            user_id: Owner scope.
            derivation_id: The derivation to trace.

        Returns:
            ``ProvenanceChain`` with all available links.  Check
            ``chain.is_complete`` to verify the full chain was resolved.

        Raises:
            DerivationNotFoundError: If the derivation doesn't exist or isn't
                owned by *user_id*.
        """
        derivation = await self._store.get_derivation(
            user_id=user_id,
            derivation_id=derivation_id,
        )
        if derivation is None:
            raise DerivationNotFoundError(
                f"Derivation {derivation_id} not found for user {user_id}"
            )

        # Fetch the entry this derivation belongs to.
        entry = await self._store.get_entry(
            user_id=user_id,
            entry_id=derivation.reflection_entry_id,
        )

        session: ReflectionSession | None = None
        source_message_ids: list[UUID] = list(derivation.supporting_message_ids)

        if entry is not None:
            # Walk to the session and get the canonical source message set.
            session = await self._store.get_session(
                user_id=user_id,
                session_id=entry.session_id,
            )
            if session is not None and session.source_message_ids:
                source_message_ids = list(session.source_message_ids)

        return ProvenanceChain(
            derivation=derivation,
            entry=entry,
            session=session,
            source_message_ids=source_message_ids,
        )

    # ── Decision updates ─────────────────────────────────────────────────

    async def update_decision(
        self,
        *,
        user_id: UUID,
        derivation_id: UUID,
        decision: str,
        applied_target_table: str | None = None,
        applied_target_id: UUID | None = None,
        processor_version: str | None = None,
    ) -> ReflectionDerivation:
        """Update the decision on a derivation (apply, reject, supersede, etc.).

        This is the sole mutation path for derivations after creation.
        Delegates to ``ReflectionStore.update_derivation_decision()``.

        Args:
            user_id: Owner scope.
            derivation_id: The derivation to update.
            decision: One of ``applied``, ``reinforced``, ``deferred``,
                ``rejected``, ``superseded``.
            applied_target_table: Required when ``decision='applied'`` —
                the target table that received the write.
            applied_target_id: Required when ``decision='applied'`` —
                the target row ID.
            processor_version: Optional processor version.

        Returns:
            The updated ``ReflectionDerivation``.

        Raises:
            DerivationNotFoundError: If the derivation doesn't exist or isn't
                owned by *user_id*.
            DerivationDecisionError: If ``decision='applied'`` but target
                table/id are missing.
        """
        return await self._store.update_derivation_decision(
            user_id=user_id,
            derivation_id=derivation_id,
            decision=decision,
            applied_target_table=applied_target_table,
            applied_target_id=applied_target_id,
            processor_version=processor_version,
        )

    # ── Lookup helpers ───────────────────────────────────────────────────

    async def get_derivation(
        self,
        *,
        user_id: UUID,
        derivation_id: UUID,
    ) -> ReflectionDerivation | None:
        """Fetch a single derivation by ID, scoped to *user_id*."""
        return await self._store.get_derivation(
            user_id=user_id,
            derivation_id=derivation_id,
        )

    async def get_derivation_by_key(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> ReflectionDerivation | None:
        """Look up a derivation by its idempotency_key, scoped to *user_id*."""
        return await self._store.get_derivation_by_idempotency_key(
            user_id=user_id,
            idempotency_key=idempotency_key,
        )

    async def list_derivations_for_entry(
        self,
        *,
        user_id: UUID,
        reflection_entry_id: UUID,
        derivation_kind: str | None = None,
        decision: str | None = None,
        limit: int = 100,
    ) -> list[ReflectionDerivation]:
        """List derivations for a specific reflection entry.

        Optionally filter by *derivation_kind* and/or *decision*.
        """
        return await self._store.list_derivations_for_entry(
            user_id=user_id,
            reflection_entry_id=reflection_entry_id,
            derivation_kind=derivation_kind,
            decision=decision,
            limit=limit,
        )

    async def list_derivations_for_session(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        derivation_kind: str | None = None,
        decision: str | None = None,
        limit: int = 500,
    ) -> list[ReflectionDerivation]:
        """List all derivations across entries in a session.

        Optionally filter by *derivation_kind* and/or *decision*.
        """
        return await self._store.list_derivations_for_session(
            user_id=user_id,
            session_id=session_id,
            derivation_kind=derivation_kind,
            decision=decision,
            limit=limit,
        )


# ── Module-level convenience ────────────────────────────────────────────────

__all__ = [
    "DerivationLedger",
    "ProvenanceChain",
    "build_idempotency_key",
]
