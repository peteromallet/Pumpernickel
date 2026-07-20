"""Reflection tool handlers for the agentic loop.

Read tools (list, get) search across the immutable reflection entry store
with authorization scoped to the calling user.  Internal classification
metadata and structured payloads are hidden by default.

Write tools (finalize, correct) enforce ownership scope and, for corrections,
follow the append-only contract: a new revision row supersedes the prior
entry without mutating the canonical raw evidence.

All handlers follow the existing pattern: they receive a ``TurnContext``
and a typed Pydantic input model, and return a typed output model.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.reflections.derivation_ledger import DerivationLedger
from app.reflections.reconciliation import (
    PoolingTargetEditProbe,
    ReconciliationEngine,
)
from app.services.reflections import (
    EntryNotFoundError,
    ReflectionStore,
    SessionFinalizeConflictError,
    SessionNotFoundError,
    SessionNotCollectingError,
)
from app.services.turn_context import TurnContext
from tool_schemas import (
    CorrectReflectionInput,
    CorrectReflectionOutput,
    FinalizeReflectionInput,
    FinalizeReflectionOutput,
    GetReflectionInput,
    GetReflectionOutput,
    ListReflectionsInput,
    ListReflectionsOutput,
    ReflectionEntryDetail,
    ReflectionEntrySummary,
)

logger = logging.getLogger(__name__)


# ── Shared helpers ───────────────────────────────────────────────────────────


def _store(ctx: TurnContext) -> ReflectionStore:
    """Return a ReflectionStore backed by the turn's database pool."""
    return ReflectionStore(ctx.pool)


def _caller_bot_id(ctx: TurnContext) -> str:
    """Resolve the calling bot id from TurnContext.

    If ctx.bot_id is None, returns 'unknown'.  In practice, bot_id is always
    set on chat turns; scheduled tasks without a bot context are not
    reflection-aware.
    """
    return ctx.bot_id or "unknown"


def _entry_to_summary(entry: Any) -> ReflectionEntrySummary:
    """Map an internal ReflectionEntry to the public summary model."""
    return ReflectionEntrySummary(
        id=entry.id,
        session_id=entry.session_id,
        template_key=entry.template_key,
        temporal_scope=entry.temporal_scope,
        phase=entry.phase,
        period_start=entry.period_start,
        period_end=entry.period_end,
        revision_number=entry.revision_number,
        created_at=entry.created_at,
    )


def _entry_to_detail(
    entry: Any,
    *,
    include_internals: bool,
    session_classification_metadata: dict[str, Any] | None = None,
    payload_fields: dict[str, Any] | None = None,
    fields_unsupported: list[str] | None = None,
) -> ReflectionEntryDetail:
    """Map an internal ReflectionEntry to the full detail model.

    Internal fields (classification metadata, payload) are only populated
    when *include_internals* is explicitly True.
    """
    detail = ReflectionEntryDetail(
        id=entry.id,
        session_id=entry.session_id,
        template_key=entry.template_key,
        temporal_scope=entry.temporal_scope,
        phase=entry.phase,
        period_start=entry.period_start,
        period_end=entry.period_end,
        revision_number=entry.revision_number,
        created_at=entry.created_at,
        bot_id=entry.bot_id,
        user_id=entry.user_id,
        topic_id=entry.topic_id,
        source_message_ids=list(entry.source_message_ids or []),
        timezone=entry.timezone,
        plaintext_searchable=entry.plaintext_searchable,
        summary_encrypted=entry.summary_encrypted,
        schema_version=entry.schema_version,
        processor_version=entry.processor_version,
        supersedes_entry_id=entry.supersedes_entry_id,
        created_by_turn_id=entry.created_by_turn_id,
    )
    if include_internals:
        detail.classification_metadata = session_classification_metadata
        detail.payload_fields = payload_fields
        detail.fields_unsupported = fields_unsupported or []
    return detail


# ── list_reflections ─────────────────────────────────────────────────────────


async def list_reflections(
    ctx: TurnContext, args: ListReflectionsInput
) -> ListReflectionsOutput:
    """List reflection entries for the current user.

    Scoped to the caller's user_id.  Returns compact summaries by default;
    full detail (including source_message_ids and internals) only when
    ``include_internals`` is explicitly True.
    """
    store = _store(ctx)
    bot_id = args.bot_id or _caller_bot_id(ctx)

    logger.info(
        "list_reflections user=%s bot=%s session=%s topic=%s internals=%s",
        ctx.user_id,
        bot_id,
        args.session_id,
        args.topic_id,
        args.include_internals,
    )

    entries = await store.list_entries(
        user_id=ctx.user_id,
        session_id=args.session_id,
        bot_id=bot_id,
        topic_id=args.topic_id,
        current_only=args.current_only,
        limit=args.limit,
    )

    if not entries:
        return ListReflectionsOutput(
            entries=[], include_internals=args.include_internals
        )

    if args.include_internals:
        # For internals, we need session classification metadata and payload.
        # Fetch sessions in batch to avoid N+1.
        session_ids = {e.session_id for e in entries}
        session_map: dict[UUID, Any] = {}
        for sid in session_ids:
            sess = await store.get_session(user_id=ctx.user_id, session_id=sid)
            if sess is not None:
                session_map[sid] = sess

        detail_entries: list[ReflectionEntryDetail] = []
        for entry in entries:
            sess = session_map.get(entry.session_id)
            detail_entries.append(
                _entry_to_detail(
                    entry,
                    include_internals=True,
                    session_classification_metadata=(
                        sess.classification_metadata if sess else None
                    ),
                    # Payload is encrypted; we don't decrypt it here.
                    # The plaintext_searchable field serves as the public surface.
                    payload_fields=(
                        {"plaintext_searchable": entry.plaintext_searchable}
                        if entry.plaintext_searchable
                        else None
                    ),
                )
            )
        return ListReflectionsOutput(
            entries=detail_entries, include_internals=True
        )

    # Default: compact summaries without internals.
    summary_entries = [_entry_to_summary(e) for e in entries]
    return ListReflectionsOutput(
        entries=summary_entries, include_internals=False
    )


# ── get_reflection ───────────────────────────────────────────────────────────


async def get_reflection(
    ctx: TurnContext, args: GetReflectionInput
) -> GetReflectionOutput:
    """Fetch a single reflection entry by ID, scoped to the caller's user_id.

    Source message IDs are always returned.  Internal classification metadata
    and structured payload fields are only returned when ``include_internals``
    is explicitly True.
    """
    store = _store(ctx)

    logger.info(
        "get_reflection user=%s entry=%s internals=%s",
        ctx.user_id,
        args.entry_id,
        args.include_internals,
    )

    entry = await store.get_entry(user_id=ctx.user_id, entry_id=args.entry_id)

    if entry is None:
        return GetReflectionOutput(
            is_error=True,
            error=f"Reflection entry {args.entry_id} not found",
        )

    session_classification = None
    if args.include_internals:
        sess = await store.get_session(
            user_id=ctx.user_id, session_id=entry.session_id
        )
        if sess is not None:
            session_classification = sess.classification_metadata

    detail = _entry_to_detail(
        entry,
        include_internals=args.include_internals,
        session_classification_metadata=session_classification,
        payload_fields=(
            {"plaintext_searchable": entry.plaintext_searchable}
            if entry.plaintext_searchable and args.include_internals
            else None
        ),
    )

    return GetReflectionOutput(entry=detail)


# ── finalize_reflection ──────────────────────────────────────────────────────


async def finalize_reflection(
    ctx: TurnContext, args: FinalizeReflectionInput
) -> FinalizeReflectionOutput:
    """Explicitly finalize a collecting reflection session.

    The session must be owned by the calling user and in ``collecting`` status.
    Finalization transitions it to ``finalizing`` so it can be claimed and
    processed by the normalization worker.

    This is the user-driven finalization path — distinct from the automated
    idle-timeout finalization performed by the finalization worker.
    """
    store = _store(ctx)

    logger.info(
        "finalize_reflection user=%s session=%s",
        ctx.user_id,
        args.session_id,
    )

    try:
        session = await store.finalize_session(
            user_id=ctx.user_id,
            session_id=args.session_id,
        )
    except SessionNotFoundError:
        return FinalizeReflectionOutput(
            is_error=True,
            error=f"Session {args.session_id} not found or not owned by you",
        )
    except SessionFinalizeConflictError as exc:
        return FinalizeReflectionOutput(
            is_error=True,
            error=str(exc),
        )

    return FinalizeReflectionOutput(
        session_id=session.id,
        status=session.status,
        finalized_at=session.finalized_at,
        source_message_ids=list(session.source_message_ids),
    )


# ── correction reconciliation (best-effort) ─────────────────────────────────


async def _reconcile_after_correction(
    *,
    store: ReflectionStore,
    pool: Any,
    user_id: UUID,
    superseded_entry_id: UUID,
    corrected_entry_id: UUID,
) -> None:
    """Best-effort reconciliation of ledgered derivations after a correction.

    When a reflection entry is superseded by a correction (append-only revision
    ``E1 -> E2``), every derivation produced from ``E1`` is now built on stale
    evidence.  This settles those derivations via :class:`ReconciliationEngine`:
    each is advanced to ``superseded`` on the *ledger row only* — no target row
    is ever written, and independently edited targets are preserved untouched
    (the structural no-clobber guarantee from T16).

    The reconciliation is deliberately best-effort.  *Any* failure (probe
    construction, ledger lookup, or decision update) is logged and swallowed so
    it can never roll back the already-persisted append-only correction — the
    canonical raw evidence is immutable per SD2, and the ``correct_reflection``
    output shape is unchanged regardless of the reconciliation outcome.

    Args:
        store: The ReflectionStore used for ledger access.
        pool: The turn's asyncpg pool, used to build the target edit probe.
            ``None`` disables independent-edit detection (reconciliation still
            preserves target coordinates structurally).
        user_id: Owner scope.
        superseded_entry_id: The entry that was superseded by the correction.
        corrected_entry_id: The new revision created by the correction.
    """
    probe: PoolingTargetEditProbe | None = None
    if pool is not None:
        try:
            probe = PoolingTargetEditProbe(pool)
        except Exception:  # noqa: BLE001 — probe build must not abort the correction
            logger.warning(
                "_reconcile_after_correction: could not build target probe; "
                "proceeding without independent-edit detection",
                exc_info=True,
            )
            probe = None

    ledger = DerivationLedger(store)
    engine = ReconciliationEngine(ledger, target_probe=probe)
    try:
        await engine.reconcile_correction(
            user_id=user_id,
            superseded_entry_id=superseded_entry_id,
            corrected_entry_id=corrected_entry_id,
        )
    except Exception:  # noqa: BLE001 — best-effort: never roll back the correction
        logger.exception(
            "_reconcile_after_correction: reconciliation failed for "
            "superseded_entry=%s corrected_entry=%s; correction is unchanged",
            superseded_entry_id,
            corrected_entry_id,
        )


# ── correct_reflection ───────────────────────────────────────────────────────


async def correct_reflection(
    ctx: TurnContext, args: CorrectReflectionInput
) -> CorrectReflectionOutput:
    """Create a correction — a new revision that supersedes an existing entry.

    **Append-only contract**: the original entry is never mutated.  A new
    revision row is inserted with ``supersedes_entry_id`` pointing to the
    prior entry.  The canonical raw evidence (``source_message_ids``) of
    the original entry remains unchanged.

    Only the entry owner can create a correction.  The new revision inherits
    session metadata (temporal_scope, phase, period boundaries, timezone)
    from the superseded entry.

    The correction surfaces the ``correction_note`` as the plaintext_searchable
    field, and a new summary if provided.
    """
    store = _store(ctx)
    bot_id = _caller_bot_id(ctx)

    logger.info(
        "correct_reflection user=%s supersedes=%s bot=%s",
        ctx.user_id,
        args.supersedes_entry_id,
        bot_id,
    )

    # Build correction payload — only include fields that were provided.
    # The plaintext_searchable and summary are the user-facing correction
    # surface; we do NOT include internal classification metadata or
    # structured payload fields (those are gated behind include_internals
    # on the read side).
    correction_payload: dict[str, Any] = {}
    if args.correction_note:
        correction_payload["correction_note"] = args.correction_note

    try:
        entry = await store.correct_entry(
            user_id=ctx.user_id,
            supersedes_entry_id=args.supersedes_entry_id,
            bot_id=bot_id,
            plaintext_searchable=args.plaintext_searchable or args.correction_note,
            summary=args.summary,
            payload=correction_payload if correction_payload else None,
        )
    except EntryNotFoundError:
        return CorrectReflectionOutput(
            is_error=True,
            error=(
                f"Entry {args.supersedes_entry_id} not found or not owned by you"
            ),
        )
    except Exception as exc:
        logger.exception("correct_reflection failed")
        return CorrectReflectionOutput(
            is_error=True,
            error=f"Correction failed: {exc}",
        )

    # Reconcile ledgered derivations: when a reflection is corrected, every
    # derivation built from the superseded entry is now stale.  We settle them
    # to "superseded" (never clobbering independently edited targets, never
    # writing targets at all).  This is best-effort: a reconciliation failure
    # MUST NOT roll back the already-persisted append-only correction, and the
    # correction output shape is unchanged so callers are unaffected.
    await _reconcile_after_correction(
        store=store,
        pool=getattr(ctx, "pool", None),
        user_id=ctx.user_id,
        superseded_entry_id=args.supersedes_entry_id,
        corrected_entry_id=entry.id,
    )

    return CorrectReflectionOutput(
        entry_id=entry.id,
        session_id=entry.session_id,
        supersedes_entry_id=entry.supersedes_entry_id,
        revision_number=entry.revision_number,
        created_at=entry.created_at,
    )
