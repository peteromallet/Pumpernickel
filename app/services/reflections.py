"""Reflection session storage APIs.

Implements the session lifecycle: open/attach, finalize (durable queue
transition), claim, retry, and recovery.  Concurrency guarantees are provided
by the partial unique index ``idx_reflection_sessions_one_collecting`` on
``(user_id, bot_id) WHERE status = 'collecting'`` and by the idempotency_key
unique constraint.

Storage contract (see migration 0066 / plan_v1 SD-001–SD-016):
  * ``mediator.reflection_sessions``   — mutable coordination + claim/queue state
  * ``mediator.reflection_entries``    — immutable revisions (append-only, encrypted)
  * ``mediator.reflection_derivations`` — knowledge derivation ledger (not managed here)
  * Finalized sessions ARE the durable processing queue — no scheduled_jobs.
  * Entries are immutable revisions; session coordination state is mutable.

Session state machine::

    collecting ──► finalizing ──► processed
      │                │
      │                ├──► processing_failed ──► finalizing  (retry)
      │                │
      ▼                ▼
    abandoned       (stale claim recovery returns session to finalizing)

Locked boundary:
  * No inbound routing, prompt, retrieval, embedding, hot context, admin UI,
    scheduling, or feature-flag coupling.
  * This module is domain-specific to reflections; it does NOT introduce a
    generic longitudinal-state framework.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.services.reflection_redaction import redact_for_log_extra

logger = logging.getLogger(__name__)

# ── Public exception surface ────────────────────────────────────────────────


class SessionNotFoundError(LookupError):
    """Raised when a session cannot be found or is not owned by the caller."""


class SessionNotCollectingError(ValueError):
    """Raised when an operation requires a collecting session but status differs."""


class SessionNotFinalizingError(ValueError):
    """Raised when an operation requires a finalizing session but status differs."""


class SessionClaimConflictError(RuntimeError):
    """Raised when claim_session cannot acquire a session (already claimed)."""


class SessionFinalizeConflictError(RuntimeError):
    """Raised when finalization fails because the session is not in collecting state."""


class EntryNotFoundError(LookupError):
    """Raised when a reflection entry cannot be found or is not owned by the caller."""


class EntryRevisionConflictError(RuntimeError):
    """Raised when an entry revision_number collides (race on next-rev allocation)."""


class EntryCorrectionError(ValueError):
    """Raised when a correction request is malformed (e.g. missing supersedes_entry_id)."""


class DerivationNotFoundError(LookupError):
    """Raised when a derivation cannot be found or is not owned by the caller."""


class DerivationIdempotencyConflictError(RuntimeError):
    """Raised when a derivation with the same idempotency_key already exists."""


class DerivationDecisionError(ValueError):
    """Raised when a derivation decision update is invalid (e.g. missing applied targets)."""


# ── Enumerations (mirrors migration CHECK constraints) ──────────────────────

VALID_STATUSES: frozenset[str] = frozenset(
    {"collecting", "finalizing", "processed", "abandoned", "processing_failed"}
)

VALID_TEMPORAL_SCOPES: frozenset[str] = frozenset(
    {"instant", "day", "week", "month", "custom", "none"}
)

VALID_PHASES: frozenset[str] = frozenset(
    {"opening", "closing", "checkpoint", "prospective", "retrospective", "freeform"}
)

# ── Failure-class taxonomy (reflection sessions) ───────────────────────────
#
# The reflection pipeline has its OWN four-class failure taxonomy, distinct
# from the message-level taxonomy in ``app/services/failure_policy.py``.
# These two taxonomies are intentionally separate because they model
# different failure domains:
#
#   Reflection taxonomy (this module)          Message taxonomy (failure_policy)
#   ─────────────────────────────────          ────────────────────────────────
#   retryable_processor  – transient proc err  retryable_pre_send
#   terminal_input       – bad/missing input   terminal_post_send
#   terminal_internal    – internal bug        infra_bug
#   stale_claim          – claim timed out     model_provider_bad_request
#                                              model_provider_timeout
#                                              tool_validation_recoverable
#                                              delivery_provider_failure
#
#   Storage: mediator.reflection_sessions      Storage: mediator.messages
#   CHECK (migration 0066)                     CHECK (migration 0046)
#   Retry via retry_session() → finalizing     Retry via FAILURE_POLICY +
#                                              recovery sweep
#
# No cross-mapping is needed.  The admin listing surface and any future
# retry/operator diagnostics that expose reflection failure_class MUST
# use this taxonomy, not the message-level one.  Conversely, the
# message-level recovery sweep MUST NOT inspect reflection_session
# failure_class.
#
# Reconciliation decision (M4 / T8): keep both taxonomies independent.
# Do not merge, do not create a third taxonomy, do not silently map
# one onto the other.
#
# The canonical source of truth for both taxonomies is now
# ``app/services/failure_class_reconciliation.py``, which also provides
# domain classification, display formatting, and cross-taxonomy validation.
from app.services.failure_class_reconciliation import (
    REFLECTION_FAILURE_CLASSES,
    validate_reflection_failure_class as _validate_failure_class,
)

VALID_FAILURE_CLASSES: frozenset[str] = REFLECTION_FAILURE_CLASSES

VALID_DERIVATION_KINDS: frozenset[str] = frozenset(
    {"memory", "observation", "distillation", "orientation"}
)

VALID_ASSERTION_SOURCES: frozenset[str] = frozenset(
    {"user_explicit", "user_implied", "agent_inferred"}
)

VALID_DECISIONS: frozenset[str] = frozenset(
    {"applied", "reinforced", "deferred", "rejected", "superseded"}
)

# ── Read model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReflectionSession:
    """Read model for a single reflection session row."""

    id: UUID
    user_id: UUID
    topic_id: UUID | None
    bot_id: str
    opened_by_message_id: UUID | None
    opened_by_turn_id: UUID | None
    source_message_ids: list[UUID]
    template_key: str
    temporal_scope: str
    phase: str
    period_start: datetime | None
    period_end: datetime | None
    timezone: str | None
    classification_source: str | None
    classification_confidence: float | None
    classification_metadata: dict[str, Any] | None
    status: str
    idle_finalize_at: datetime | None
    finalized_at: datetime | None
    processed_at: datetime | None
    abandoned_at: datetime | None
    claimed_by: str | None
    claimed_at: datetime | None
    retry_count: int
    failure_class: str | None
    failure_reason: str | None
    last_error: str | None
    idempotency_key: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> ReflectionSession:
        """Construct from an asyncpg Record or dict."""
        get = _row_getter(row)
        source_ids = get("source_message_ids") or []
        if not isinstance(source_ids, list):
            source_ids = list(source_ids)
        classification_metadata = get("classification_metadata")
        if isinstance(classification_metadata, str):
            import json

            try:
                classification_metadata = json.loads(classification_metadata)
            except (json.JSONDecodeError, TypeError):
                classification_metadata = None
        return cls(
            id=get("id"),
            user_id=get("user_id"),
            topic_id=get("topic_id"),
            bot_id=get("bot_id"),
            opened_by_message_id=get("opened_by_message_id"),
            opened_by_turn_id=get("opened_by_turn_id"),
            source_message_ids=source_ids,
            template_key=get("template_key"),
            temporal_scope=get("temporal_scope"),
            phase=get("phase"),
            period_start=get("period_start"),
            period_end=get("period_end"),
            timezone=get("timezone"),
            classification_source=get("classification_source"),
            classification_confidence=get("classification_confidence"),
            classification_metadata=classification_metadata,
            status=get("status"),
            idle_finalize_at=get("idle_finalize_at"),
            finalized_at=get("finalized_at"),
            processed_at=get("processed_at"),
            abandoned_at=get("abandoned_at"),
            claimed_by=get("claimed_by"),
            claimed_at=get("claimed_at"),
            retry_count=int(get("retry_count") or 0),
            failure_class=get("failure_class"),
            failure_reason=get("failure_reason"),
            last_error=get("last_error"),
            idempotency_key=get("idempotency_key"),
            created_at=get("created_at"),
            updated_at=get("updated_at"),
        )


# ── Entry read model ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReflectionEntry:
    """Read model for a single immutable reflection entry row.

    Entries are append-only revisions.  Corrections create a new row with
    ``supersedes_entry_id`` pointing to the prior revision; the prior row
    is never mutated.  Consumers should prefer the *current* revision: the
    leaf row that is not referenced by any successor.
    """

    id: UUID
    session_id: UUID
    user_id: UUID
    topic_id: UUID | None
    bot_id: str
    template_key: str
    temporal_scope: str
    phase: str
    period_start: datetime | None
    period_end: datetime | None
    timezone: str | None
    source_message_ids: list[UUID]
    payload_encrypted: bytes | None
    plaintext_searchable: str | None
    summary_encrypted: bytes | None
    schema_version: int
    processor_version: str | None
    revision_number: int
    supersedes_entry_id: UUID | None
    created_by_turn_id: UUID | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> ReflectionEntry:
        """Construct from an asyncpg Record or dict."""
        get = _row_getter(row)
        source_ids = get("source_message_ids") or []
        if not isinstance(source_ids, list):
            source_ids = list(source_ids)
        return cls(
            id=get("id"),
            session_id=get("session_id"),
            user_id=get("user_id"),
            topic_id=get("topic_id"),
            bot_id=get("bot_id"),
            template_key=get("template_key"),
            temporal_scope=get("temporal_scope"),
            phase=get("phase"),
            period_start=get("period_start"),
            period_end=get("period_end"),
            timezone=get("timezone"),
            source_message_ids=source_ids,
            payload_encrypted=get("payload_encrypted"),
            plaintext_searchable=get("plaintext_searchable"),
            summary_encrypted=get("summary_encrypted"),
            schema_version=int(get("schema_version") or 1),
            processor_version=get("processor_version"),
            revision_number=int(get("revision_number") or 1),
            supersedes_entry_id=get("supersedes_entry_id"),
            created_by_turn_id=get("created_by_turn_id"),
            created_at=get("created_at"),
        )


# ── Derivation read model ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReflectionDerivation:
    """Read model for a single reflection derivation row.

    Derivations represent knowledge-claims derived from a reflection entry
    that may feed into later memory, observation, distillation, or Compass
    stages.  Each derivation is traceable to a specific reflection entry
    and carries conservative metadata (kind, assertion source, confidence,
    eligibility reasons, supporting message IDs) without implementing the
    M2/M3 policy logic itself.

    Idempotency is enforced via the ``idempotency_key`` unique constraint:
    retried submissions with the same key return the existing derivation
    rather than creating a duplicate.
    """

    id: UUID
    reflection_entry_id: UUID
    user_id: UUID
    derivation_kind: str
    candidate_payload_encrypted: bytes | None
    assertion_source: str
    confidence: float | None
    eligibility_reasons: list[str] | None
    supporting_message_ids: list[UUID]
    decision: str
    applied_target_table: str | None
    applied_target_id: UUID | None
    processor_version: str | None
    processor_turn_id: UUID | None
    idempotency_key: str | None
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def from_row(cls, row: Any) -> ReflectionDerivation:
        """Construct from an asyncpg Record or dict."""
        get = _row_getter(row)
        support_ids = get("supporting_message_ids") or []
        if not isinstance(support_ids, list):
            support_ids = list(support_ids)
        eligibility = get("eligibility_reasons")
        if isinstance(eligibility, str):
            import json

            try:
                eligibility = json.loads(eligibility)
            except (json.JSONDecodeError, TypeError):
                eligibility = None
        if eligibility is not None and not isinstance(eligibility, list):
            eligibility = None
        return cls(
            id=get("id"),
            reflection_entry_id=get("reflection_entry_id"),
            user_id=get("user_id"),
            derivation_kind=get("derivation_kind"),
            candidate_payload_encrypted=get("candidate_payload_encrypted"),
            assertion_source=get("assertion_source"),
            confidence=get("confidence"),
            eligibility_reasons=eligibility,
            supporting_message_ids=support_ids,
            decision=get("decision"),
            applied_target_table=get("applied_target_table"),
            applied_target_id=get("applied_target_id"),
            processor_version=get("processor_version"),
            processor_turn_id=get("processor_turn_id"),
            idempotency_key=get("idempotency_key"),
            created_at=get("created_at"),
            decided_at=get("decided_at"),
        )


def _row_getter(row: Any) -> Any:
    """Return a key-accessor callable for asyncpg Record or dict."""
    if isinstance(row, dict):
        return row.get
    return lambda key: row[key]


# ── Validation helpers ──────────────────────────────────────────────────────


def _require_user_id(user_id: UUID | None) -> UUID:
    if user_id is None:
        raise ValueError("user_id is required and must not be None")
    return user_id


def _require_bot_id(bot_id: str | None) -> str:
    if not bot_id or not bot_id.strip():
        raise ValueError("bot_id must be a non-blank string")
    return bot_id


def _validate_status(status: str) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    return status


def _validate_temporal_scope(scope: str) -> str:
    if scope not in VALID_TEMPORAL_SCOPES:
        raise ValueError(
            f"invalid temporal_scope {scope!r}; "
            f"expected one of {sorted(VALID_TEMPORAL_SCOPES)}"
        )
    return scope


def _validate_phase(phase: str) -> str:
    if phase not in VALID_PHASES:
        raise ValueError(
            f"invalid phase {phase!r}; expected one of {sorted(VALID_PHASES)}"
        )
    return phase


# _validate_failure_class is now imported from
# app.services.failure_class_reconciliation (see module-level import).


def _validate_derivation_kind(kind: str) -> str:
    if not kind or not isinstance(kind, str):
        raise ValueError("derivation_kind must be a non-blank string")
    if kind not in VALID_DERIVATION_KINDS:
        raise ValueError(
            f"invalid derivation_kind {kind!r}; "
            f"expected one of {sorted(VALID_DERIVATION_KINDS)}"
        )
    return kind


def _validate_assertion_source(source: str) -> str:
    if not source or not isinstance(source, str):
        raise ValueError("assertion_source must be a non-blank string")
    if source not in VALID_ASSERTION_SOURCES:
        raise ValueError(
            f"invalid assertion_source {source!r}; "
            f"expected one of {sorted(VALID_ASSERTION_SOURCES)}"
        )
    return source


def _validate_decision(decision: str) -> str:
    if not decision or not isinstance(decision, str):
        raise ValueError("decision must be a non-blank string")
    if decision not in VALID_DECISIONS:
        raise ValueError(
            f"invalid decision {decision!r}; "
            f"expected one of {sorted(VALID_DECISIONS)}"
        )
    return decision


def _session_source_alive_condition(session_alias: str) -> str:
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM messages opened
            WHERE opened.id = {session_alias}.opened_by_message_id
              AND opened.deleted_at IS NOT NULL
        )
        AND NOT EXISTS (
            SELECT 1
            FROM unnest(COALESCE({session_alias}.source_message_ids, ARRAY[]::uuid[])) AS source_message_id
            JOIN messages source_messages
              ON source_messages.id = source_message_id
            WHERE source_messages.deleted_at IS NOT NULL
        )
    """


def _visible_entry_conditions(
    entry_alias: str = "re",
    session_alias: str = "rs",
) -> list[str]:
    return [
        f"{entry_alias}.plaintext_searchable IS NOT NULL",
        f"btrim({entry_alias}.plaintext_searchable) <> ''",
        f"{session_alias}.status = 'processed'",
        _session_source_alive_condition(session_alias),
    ]


def _current_entry_condition(entry_alias: str = "re") -> str:
    """Return the append-only leaf predicate for a reflection revision.

    ``supersedes_entry_id`` points from a newer revision to the revision it
    replaces.  Therefore a current revision is an entry that no later row
    references, not an entry whose own pointer is null.
    """
    return f"""
        NOT EXISTS (
            SELECT 1
            FROM mediator.reflection_entries successor
            WHERE successor.supersedes_entry_id = {entry_alias}.id
        )
    """


# ── Store ───────────────────────────────────────────────────────────────────


class ReflectionStore:
    """Async store for reflection session lifecycle.

    All methods require an explicit ``user_id``.  Session state transitions
    are validated before SQL, and the database enforces the remaining
    invariants via CHECK constraints and partial unique indexes.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ── Read ────────────────────────────────────────────────────────────

    async def get_session(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        visible_only: bool = False,
    ) -> ReflectionSession | None:
        """Fetch a single reflection session by ID, scoped to user_id."""
        _require_user_id(user_id)
        visibility_clause = ""
        if visible_only:
            visibility_clause = f"\n              AND {_session_source_alive_condition('rs')}"
        row = await self._pool.fetchrow(
            f"""
            SELECT *
            FROM mediator.reflection_sessions rs
            WHERE rs.id = $1
              AND rs.user_id = $2{visibility_clause}
            """,
            session_id,
            user_id,
        )
        if row is None:
            return None
        return ReflectionSession.from_row(row)

    async def list_sessions(
        self,
        *,
        user_id: UUID,
        statuses: list[str] | None = None,
        limit: int = 50,
    ) -> list[ReflectionSession]:
        """List sessions for a user, newest first.

        Optionally filter by status.
        """
        _require_user_id(user_id)

        conditions = ["user_id = $1"]
        params: list[Any] = [user_id]
        param_idx = 2

        if statuses:
            for s in statuses:
                _validate_status(s)
            conditions.append(f"status = ANY(${param_idx}::text[])")
            params.append(statuses)
            param_idx += 1

        where_clause = " AND ".join(conditions)
        sql = f"""
            SELECT *
            FROM mediator.reflection_sessions
            WHERE {where_clause}
            ORDER BY created_at DESC
            LIMIT ${param_idx}
        """
        params.append(limit)
        rows = await self._pool.fetch(sql, *params)
        return [ReflectionSession.from_row(r) for r in rows]

    # ── open_or_attach_session ──────────────────────────────────────────

    async def open_or_attach_session(
        self,
        *,
        user_id: UUID,
        bot_id: str,
        template_key: str,
        temporal_scope: str,
        phase: str,
        topic_id: UUID | None = None,
        opened_by_message_id: UUID | None = None,
        opened_by_turn_id: UUID | None = None,
        source_message_ids: list[UUID] | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        timezone_: str | None = None,
        classification_source: str | None = None,
        classification_confidence: float | None = None,
        classification_metadata: dict[str, Any] | None = None,
        idle_finalize_at: datetime | None = None,
        idempotency_key: str | None = None,
    ) -> ReflectionSession:
        """Open a new reflection session or attach to an existing collecting one.

        Guarantees at most one collecting session per ``(user_id, bot_id)``.
        The partial unique index ``idx_reflection_sessions_one_collecting``
        enforces this at the database level, so concurrent callers that both
        attempt an INSERT will see a unique violation on the second INSERT;
        that caller falls back to re-reading the session that won the race.

        When an existing collecting session is found:
          * New ``source_message_ids`` are **appended** (deduplicated) to the
            existing array.
          * ``idle_finalize_at`` is updated if the caller provides a later value.
          * Other fields (template_key, temporal_scope, phase, etc.) are left
            unchanged — the first opener owns the session shape.

        Returns the current session (newly created or existing).
        """
        _require_user_id(user_id)
        _require_bot_id(bot_id)
        _validate_temporal_scope(temporal_scope)
        _validate_phase(phase)

        if not template_key or not template_key.strip():
            raise ValueError("template_key must be a non-blank string")

        if classification_confidence is not None:
            if not (0 <= classification_confidence <= 1):
                raise ValueError(
                    f"classification_confidence must be in [0, 1], "
                    f"got {classification_confidence}"
                )

        if idle_finalize_at is not None and idle_finalize_at.tzinfo is None:
            raise ValueError("idle_finalize_at must be timezone-aware")

        now = datetime.now(timezone.utc)

        # ── Step 1: try to attach to an existing collecting session ──────
        existing = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.reflection_sessions
            WHERE user_id = $1 AND bot_id = $2 AND status = 'collecting'
            """,
            user_id,
            bot_id,
        )

        if existing is not None:
            return await self._attach_to_session(
                existing=existing,
                source_message_ids=source_message_ids,
                idle_finalize_at=idle_finalize_at,
            )

        # ── Step 2: no existing session — try to create one ──────────────
        try:
            row = await self._pool.fetchrow(
                """
                INSERT INTO mediator.reflection_sessions (
                    user_id, topic_id, bot_id,
                    opened_by_message_id, opened_by_turn_id,
                    source_message_ids,
                    template_key, temporal_scope, phase,
                    period_start, period_end, timezone,
                    classification_source, classification_confidence,
                    classification_metadata,
                    status, idle_finalize_at,
                    idempotency_key,
                    created_at, updated_at
                ) VALUES (
                    $1, $2, $3,
                    $4, $5,
                    $6::uuid[],
                    $7, $8, $9,
                    $10, $11, $12,
                    $13, $14,
                    $15::jsonb,
                    'collecting', $16,
                    $17,
                    $18, $18
                )
                RETURNING *
                """,
                user_id,
                topic_id,
                bot_id,
                opened_by_message_id,
                opened_by_turn_id,
                list(source_message_ids or []),
                template_key,
                temporal_scope,
                phase,
                period_start,
                period_end,
                timezone_,
                classification_source,
                classification_confidence,
                classification_metadata,
                idle_finalize_at,
                idempotency_key,
                now,
            )
            logger.info(
                "open_or_attach_session: created new session %s for user=%s bot=%s",
                row["id"],
                user_id,
                bot_id,
            )
            return ReflectionSession.from_row(row)

        except Exception as exc:
            # Unique violation on idempotency_key or the partial unique index
            # idx_reflection_sessions_one_collecting.  In either case, another
            # caller won the race; fall back to attaching.
            exc_name = type(exc).__name__
            if "UniqueViolation" in exc_name or "unique" in str(exc).lower():
                logger.debug(
                    "open_or_attach_session: unique violation (race), "
                    "re-reading for user=%s bot=%s",
                    user_id,
                    bot_id,
                )
                existing = await self._pool.fetchrow(
                    """
                    SELECT *
                    FROM mediator.reflection_sessions
                    WHERE user_id = $1 AND bot_id = $2 AND status = 'collecting'
                    """,
                    user_id,
                    bot_id,
                )
                if existing is not None:
                    return await self._attach_to_session(
                        existing=existing,
                        source_message_ids=source_message_ids,
                        idle_finalize_at=idle_finalize_at,
                    )
                # Should be unreachable: the index violation means a row exists,
                # but if it was concurrently deleted/transitioned we loop once.
                logger.warning(
                    "open_or_attach_session: unique violation but no collecting "
                    "session found for user=%s bot=%s — retrying create",
                    user_id,
                    bot_id,
                )
                # One retry — this time if it fails we let it raise.
                row = await self._pool.fetchrow(
                    """
                    INSERT INTO mediator.reflection_sessions (
                        user_id, topic_id, bot_id,
                        opened_by_message_id, opened_by_turn_id,
                        source_message_ids,
                        template_key, temporal_scope, phase,
                        period_start, period_end, timezone,
                        classification_source, classification_confidence,
                        classification_metadata,
                        status, idle_finalize_at,
                        idempotency_key,
                        created_at, updated_at
                    ) VALUES (
                        $1, $2, $3,
                        $4, $5,
                        $6::uuid[],
                        $7, $8, $9,
                        $10, $11, $12,
                        $13, $14,
                        $15::jsonb,
                        'collecting', $16,
                        $17,
                        $18, $18
                    )
                    RETURNING *
                    """,
                    user_id,
                    topic_id,
                    bot_id,
                    opened_by_message_id,
                    opened_by_turn_id,
                    list(source_message_ids or []),
                    template_key,
                    temporal_scope,
                    phase,
                    period_start,
                    period_end,
                    timezone_,
                    classification_source,
                    classification_confidence,
                    classification_metadata,
                    idle_finalize_at,
                    idempotency_key,
                    now,
                )
                return ReflectionSession.from_row(row)
            raise

    async def _attach_to_session(
        self,
        *,
        existing: Any,
        source_message_ids: list[UUID] | None = None,
        idle_finalize_at: datetime | None = None,
    ) -> ReflectionSession:
        """Update an existing collecting session with new source messages and
        optionally bump idle_finalize_at.

        New message IDs are appended and deduplicated.  idle_finalize_at is
        only moved forward (never backward).
        """
        session_id = existing["id"]
        now = datetime.now(timezone.utc)

        # Build the updated source_message_ids array.
        current_ids: list[UUID] = list(existing.get("source_message_ids") or [])
        if source_message_ids:
            existing_set = set(current_ids)
            new_ids = [mid for mid in source_message_ids if mid not in existing_set]
            if new_ids:
                current_ids.extend(new_ids)

        # Determine effective idle_finalize_at.
        current_idle = existing.get("idle_finalize_at")
        effective_idle = current_idle
        if idle_finalize_at is not None:
            if current_idle is None or idle_finalize_at > current_idle:
                effective_idle = idle_finalize_at

        # Only UPDATE if something actually changed.
        ids_changed = bool(source_message_ids and any(
            mid not in set(existing.get("source_message_ids") or [])
            for mid in source_message_ids
        ))
        idle_changed = effective_idle != current_idle

        if not ids_changed and not idle_changed:
            logger.debug(
                "_attach_to_session: no changes for session=%s", session_id
            )
            return ReflectionSession.from_row(existing)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET source_message_ids = $2::uuid[],
                idle_finalize_at = $3,
                updated_at = $4
            WHERE id = $1 AND status = 'collecting'
            RETURNING *
            """,
            session_id,
            current_ids,
            effective_idle,
            now,
        )

        if row is None:
            # Session transitioned away from collecting between our SELECT
            # and UPDATE.  Re-read to return current state.
            row = await self._pool.fetchrow(
                "SELECT * FROM mediator.reflection_sessions WHERE id = $1",
                session_id,
            )
            if row is None:
                raise SessionNotFoundError(
                    f"Session {session_id} vanished during attach"
                )

        logger.debug("_attach_to_session: updated session=%s", session_id)
        return ReflectionSession.from_row(row)

    # ── finalize_session ────────────────────────────────────────────────

    async def finalize_session(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        idle_finalize_at: datetime | None = None,
    ) -> ReflectionSession:
        """Transition a session from ``collecting`` to ``finalizing``.

        Finalization is the durable queue transition: once a session is
        finalized, it becomes eligible for claiming and processing.

        Only the session owner (``user_id``) can finalize.  The session must
        be in ``collecting`` status.
        """
        _require_user_id(user_id)

        if idle_finalize_at is not None and idle_finalize_at.tzinfo is None:
            raise ValueError("idle_finalize_at must be timezone-aware")

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET status = 'finalizing',
                finalized_at = $3,
                idle_finalize_at = COALESCE($4, idle_finalize_at),
                updated_at = $3
            WHERE id = $1
              AND user_id = $2
              AND status = 'collecting'
            RETURNING *
            """,
            session_id,
            user_id,
            now,
            idle_finalize_at,
        )

        if row is None:
            # Determine why: does the session exist at all?
            current = await self._pool.fetchrow(
                "SELECT id, status FROM mediator.reflection_sessions WHERE id = $1",
                session_id,
            )
            if current is None:
                raise SessionNotFoundError(
                    f"Session {session_id} not found"
                )
            if current["status"] != "collecting":
                raise SessionFinalizeConflictError(
                    f"Session {session_id} has status {current['status']!r}, "
                    f"expected 'collecting'"
                )
            # Exists and is collecting but user_id didn't match.
            raise SessionNotFoundError(
                f"Session {session_id} not found for user {user_id}"
            )

        logger.info(
            "finalize_session: session=%s finalized for user=%s",
            session_id,
            user_id,
        )
        return ReflectionSession.from_row(row)

    # ── claim_session ───────────────────────────────────────────────────

    async def claim_session(
        self,
        *,
        claimed_by: str,
        stale_claim_seconds: int = 300,
    ) -> ReflectionSession | None:
        """Atomically claim the oldest finalized session for processing.

        Uses an atomic CTE (UPDATE ... WHERE ... RETURNING) so at most one
        worker can claim any given session.  Sessions that are already claimed
        but whose claim is older than *stale_claim_seconds* are also eligible
        (stale claim recovery inline).

        Returns the claimed session, or ``None`` if no finalized session is
        available.
        """
        if not claimed_by or not claimed_by.strip():
            raise ValueError("claimed_by must be a non-blank string")
        if stale_claim_seconds < 0:
            raise ValueError("stale_claim_seconds must be >= 0")

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            WITH claimed AS (
                SELECT id
                FROM mediator.reflection_sessions
                WHERE status = 'finalizing'
                  AND (
                      claimed_by IS NULL
                      OR claimed_at < $2::timestamptz - make_interval(secs => $3)
                  )
                ORDER BY finalized_at ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE mediator.reflection_sessions s
            SET claimed_by   = $1,
                claimed_at   = $2,
                retry_count  = CASE
                    WHEN s.claimed_by IS NOT NULL
                    THEN s.retry_count + 1
                    ELSE s.retry_count
                END,
                updated_at   = $2
            FROM claimed c
            WHERE s.id = c.id
            RETURNING s.*
            """,
            claimed_by,
            now,
            stale_claim_seconds,
        )

        if row is None:
            logger.debug("claim_session: no finalized session available")
            return None

        logger.info(
            "claim_session: claimed session=%s by=%s",
            row["id"],
            claimed_by,
        )
        return ReflectionSession.from_row(row)

    # ── release_claim ───────────────────────────────────────────────────

    async def release_claim(
        self,
        *,
        session_id: UUID,
        claimed_by: str,
    ) -> ReflectionSession | None:
        """Gracefully release a claim, returning the session to ``finalizing``.

        Only the current claim holder can release.  This is used when a worker
        decides it cannot process the session (e.g. shutdown, overload).
        """
        if not claimed_by or not claimed_by.strip():
            raise ValueError("claimed_by must be a non-blank string")

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET claimed_by = NULL,
                claimed_at = NULL,
                updated_at = $3
            WHERE id = $1
              AND claimed_by = $2
              AND status = 'finalizing'
            RETURNING *
            """,
            session_id,
            claimed_by,
            now,
        )

        if row is None:
            logger.debug(
                "release_claim: session=%s not found or not claimed by=%s",
                session_id,
                claimed_by,
            )
            return None

        logger.info("release_claim: released session=%s", session_id)
        return ReflectionSession.from_row(row)

    # ── mark_session_processed ──────────────────────────────────────────

    async def mark_session_processed(
        self,
        *,
        session_id: UUID,
        claimed_by: str,
    ) -> ReflectionSession | None:
        """Mark a claimed session as successfully processed.

        Only the current claim holder can mark processed.  The session must
        be in ``finalizing`` status and claimed by *claimed_by*.
        """
        if not claimed_by or not claimed_by.strip():
            raise ValueError("claimed_by must be a non-blank string")

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET status = 'processed',
                processed_at = $3,
                claimed_by = NULL,
                claimed_at = NULL,
                failure_class = NULL,
                failure_reason = NULL,
                last_error = NULL,
                updated_at = $3
            WHERE id = $1
              AND claimed_by = $2
              AND status = 'finalizing'
            RETURNING *
            """,
            session_id,
            claimed_by,
            now,
        )

        if row is None:
            logger.debug(
                "mark_session_processed: session=%s not found or not claimed by=%s",
                session_id,
                claimed_by,
            )
            return None

        logger.info("mark_session_processed: session=%s processed", session_id)

        session = ReflectionSession.from_row(row)

        # ── Embedding lifecycle: enqueue for all current entries ──
        try:
            from app.services.message_embedding_lifecycle import (
                enqueue_reflection_embed,
            )

            entries = await self._pool.fetch(
                f"""
                SELECT re.id, re.plaintext_searchable
                FROM mediator.reflection_entries re
                WHERE re.session_id = $1
                  AND {_current_entry_condition('re')}
                  AND plaintext_searchable IS NOT NULL
                  AND btrim(plaintext_searchable) <> ''
                """,
                session_id,
            )
            for entry_row in entries:
                await enqueue_reflection_embed(
                    self._pool,
                    entry_id=entry_row["id"],
                    plaintext_searchable=entry_row["plaintext_searchable"],
                )
        except Exception:
            logger.warning(
                "mark_session_processed: failed to enqueue reflection embeds "
                "for session=%s",
                session_id,
                exc_info=True,
                extra=redact_for_log_extra(
                    {"session_id": str(session_id)}
                ),
            )

        return session

    # ── complete_session ────────────────────────────────────────────────

    async def complete_session(
        self,
        *,
        session_id: UUID,
        user_id: UUID,
    ) -> ReflectionSession | None:
        """Transition a session from ``finalizing`` to ``processed``.

        This is the claim-free completion path used by the finalization
        worker.  Unlike :meth:`mark_session_processed`, it does not require
        a ``claimed_by`` match — it guards on ``finalizing`` status and
        ``user_id`` ownership instead.

        Idempotent: if the session is already ``processed``, it returns
        the current row without error.
        """
        _require_user_id(user_id)

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET status = 'processed',
                updated_at = $3
            WHERE id = $1
              AND user_id = $2
              AND status = 'finalizing'
            RETURNING *
            """,
            session_id,
            user_id,
            now,
        )

        if row is None:
            # Check if it's already processed (idempotent).
            current = await self._pool.fetchrow(
                """
                SELECT id, status, user_id
                FROM mediator.reflection_sessions
                WHERE id = $1
                """,
                session_id,
            )
            if current is None:
                raise SessionNotFoundError(
                    f"Session {session_id} not found"
                )
            if current["status"] == "processed":
                logger.debug(
                    "complete_session: session=%s already processed (idempotent)",
                    session_id,
                )
                return ReflectionSession.from_row(current)
            if current["status"] != "finalizing":
                raise ValueError(
                    f"Session {session_id} has status {current['status']!r}, "
                    f"expected 'finalizing' or 'processed'"
                )
            if str(current["user_id"]) != str(user_id):
                raise SessionNotFoundError(
                    f"Session {session_id} not found for user {user_id}"
                )

        logger.info(
            "complete_session: session=%s transitioned to processed",
            session_id,
        )
        return ReflectionSession.from_row(row)

    # ── mark_session_failed ─────────────────────────────────────────────

    async def mark_session_failed(
        self,
        *,
        session_id: UUID,
        claimed_by: str,
        failure_class: str,
        failure_reason: str | None = None,
        last_error: str | None = None,
    ) -> ReflectionSession | None:
        """Mark a claimed session as failed.

        Only the current claim holder can mark failed.  The session must be
        in ``finalizing`` status and claimed by *claimed_by*.

        *failure_class* must be a recognised value from
        ``VALID_FAILURE_CLASSES``.
        """
        if not claimed_by or not claimed_by.strip():
            raise ValueError("claimed_by must be a non-blank string")
        _validate_failure_class(failure_class)

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET status = 'processing_failed',
                failure_class = $3,
                failure_reason = $4,
                last_error = $5,
                updated_at = $6
            WHERE id = $1
              AND claimed_by = $2
              AND status = 'finalizing'
            RETURNING *
            """,
            session_id,
            claimed_by,
            failure_class,
            failure_reason,
            last_error,
            now,
        )

        if row is None:
            logger.debug(
                "mark_session_failed: session=%s not found or not claimed by=%s",
                session_id,
                claimed_by,
            )
            return None

        logger.info(
            "mark_session_failed: session=%s failed class=%s",
            session_id,
            failure_class,
        )
        return ReflectionSession.from_row(row)

    # ── retry_session ───────────────────────────────────────────────────

    async def retry_session(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
    ) -> ReflectionSession:
        """Retry a failed session, transitioning it back to ``finalizing``.

        Increments ``retry_count``.  The session must be in
        ``processing_failed`` status.  Only the session owner can retry.
        """
        _require_user_id(user_id)

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET status = 'finalizing',
                retry_count = retry_count + 1,
                claimed_by = NULL,
                claimed_at = NULL,
                failure_class = NULL,
                failure_reason = NULL,
                last_error = NULL,
                updated_at = $3
            WHERE id = $1
              AND user_id = $2
              AND status = 'processing_failed'
            RETURNING *
            """,
            session_id,
            user_id,
            now,
        )

        if row is None:
            current = await self._pool.fetchrow(
                "SELECT id, status, user_id FROM mediator.reflection_sessions WHERE id = $1",
                session_id,
            )
            if current is None:
                raise SessionNotFoundError(
                    f"Session {session_id} not found"
                )
            if current["status"] != "processing_failed":
                raise ValueError(
                    f"Session {session_id} has status {current['status']!r}, "
                    f"expected 'processing_failed'"
                )
            raise SessionNotFoundError(
                f"Session {session_id} not found for user {user_id}"
            )

        logger.info(
            "retry_session: session=%s retried (retry_count=%s)",
            session_id,
            row["retry_count"],
        )
        return ReflectionSession.from_row(row)

    # ── recover_stale_claims ────────────────────────────────────────────

    async def recover_stale_claims(
        self,
        *,
        stale_claim_seconds: int = 300,
        limit: int = 100,
    ) -> list[ReflectionSession]:
        """Recover sessions whose claims have exceeded *stale_claim_seconds*.

        Transitions stale claimed sessions to ``processing_failed`` with
        ``failure_class = 'stale_claim'`` so they are visible in operator
        dashboards and eligible for ``retry_session``.  Previously this
        left sessions in ``finalizing`` with a failure_class set — an
        inconsistent state that was invisible to retry/operator surfaces.

        This is a sweeper operation — it does not require a specific user_id.

        Sessions that have already exceeded retry limits should be handled
        by a separate policy; this method recovers ALL stale claims regardless
        of retry_count.
        """
        if stale_claim_seconds < 0:
            raise ValueError("stale_claim_seconds must be >= 0")

        now = datetime.now(timezone.utc)

        rows = await self._pool.fetch(
            """
            WITH stale AS (
                SELECT id
                FROM mediator.reflection_sessions
                WHERE status = 'finalizing'
                  AND claimed_by IS NOT NULL
                  AND claimed_at < $1::timestamptz - make_interval(secs => $2)
                ORDER BY claimed_at ASC
                LIMIT $3
                FOR UPDATE SKIP LOCKED
            )
            UPDATE mediator.reflection_sessions s
            SET status        = 'processing_failed',
                claimed_by   = NULL,
                claimed_at   = NULL,
                failure_class = 'stale_claim',
                updated_at   = $1
            FROM stale st
            WHERE s.id = st.id
            RETURNING s.*
            """,
            now,
            stale_claim_seconds,
            limit,
        )

        recovered = [ReflectionSession.from_row(r) for r in rows]
        if recovered:
            logger.info(
                "recover_stale_claims: recovered %d stale claims "
                "(transitioned to processing_failed)",
                len(recovered),
            )
        return recovered

    # ── abandon_session ─────────────────────────────────────────────────

    async def abandon_session(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
    ) -> ReflectionSession:
        """Abandon a collecting session.

        Only the session owner can abandon.  The session must be in
        ``collecting`` status.
        """
        _require_user_id(user_id)

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET status = 'abandoned',
                abandoned_at = $3,
                updated_at = $3
            WHERE id = $1
              AND user_id = $2
              AND status = 'collecting'
            RETURNING *
            """,
            session_id,
            user_id,
            now,
        )

        if row is None:
            current = await self._pool.fetchrow(
                "SELECT id, status, user_id FROM mediator.reflection_sessions WHERE id = $1",
                session_id,
            )
            if current is None:
                raise SessionNotFoundError(
                    f"Session {session_id} not found"
                )
            if current["status"] != "collecting":
                raise SessionNotCollectingError(
                    f"Session {session_id} has status {current['status']!r}, "
                    f"expected 'collecting'"
                )
            raise SessionNotFoundError(
                f"Session {session_id} not found for user {user_id}"
            )

        logger.info("abandon_session: session=%s abandoned", session_id)
        return ReflectionSession.from_row(row)

    # ── update_idle_finalize ────────────────────────────────────────────

    async def update_idle_finalize(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        idle_finalize_at: datetime,
    ) -> ReflectionSession:
        """Update the idle-finalization deadline for a collecting session.

        Only moves the deadline forward (never backward).  The session must
        be in ``collecting`` status.
        """
        _require_user_id(user_id)
        if idle_finalize_at.tzinfo is None:
            raise ValueError("idle_finalize_at must be timezone-aware")

        now = datetime.now(timezone.utc)

        row = await self._pool.fetchrow(
            """
            UPDATE mediator.reflection_sessions
            SET idle_finalize_at = $3,
                updated_at = $4
            WHERE id = $1
              AND user_id = $2
              AND status = 'collecting'
              AND (idle_finalize_at IS NULL OR idle_finalize_at < $3)
            RETURNING *
            """,
            session_id,
            user_id,
            idle_finalize_at,
            now,
        )

        if row is None:
            # Re-read to determine why.
            current = await self._pool.fetchrow(
                "SELECT id, status, user_id, idle_finalize_at "
                "FROM mediator.reflection_sessions WHERE id = $1",
                session_id,
            )
            if current is None:
                raise SessionNotFoundError(
                    f"Session {session_id} not found"
                )
            if current["status"] != "collecting":
                raise SessionNotCollectingError(
                    f"Session {session_id} has status {current['status']!r}, "
                    f"expected 'collecting'"
                )
            if current.get("user_id") != user_id:
                raise SessionNotFoundError(
                    f"Session {session_id} not found for user {user_id}"
                )
            # idle_finalize_at was already later — return current state.
            return ReflectionSession.from_row(current)

        return ReflectionSession.from_row(row)

    # ── find_idle_sessions ──────────────────────────────────────────────

    async def find_idle_sessions(
        self,
        *,
        before: datetime | None = None,
        limit: int = 100,
    ) -> list[ReflectionSession]:
        """Find collecting sessions whose idle_finalize_at has passed.

        These are candidates for auto-finalization by a sweeper.
        """
        if before is None:
            before = datetime.now(timezone.utc)

        rows = await self._pool.fetch(
            """
            SELECT *
            FROM mediator.reflection_sessions
            WHERE status = 'collecting'
              AND idle_finalize_at IS NOT NULL
              AND idle_finalize_at <= $1
            ORDER BY idle_finalize_at ASC
            LIMIT $2
            """,
            before,
            limit,
        )
        return [ReflectionSession.from_row(r) for r in rows]

    # ── list_finalized_ready ────────────────────────────────────────────

    async def list_finalized_ready(
        self,
        *,
        limit: int = 100,
    ) -> list[ReflectionSession]:
        """List finalized sessions that are ready for processing (unclaimed)."""
        rows = await self._pool.fetch(
            """
            SELECT *
            FROM mediator.reflection_sessions
            WHERE status = 'finalizing'
              AND claimed_by IS NULL
            ORDER BY finalized_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [ReflectionSession.from_row(r) for r in rows]

    # ── list_failed_retryable ───────────────────────────────────────────

    async def list_failed_retryable(
        self,
        *,
        limit: int = 100,
    ) -> list[ReflectionSession]:
        """List processing_failed sessions eligible for retry.

        Ordered by retry_count ASC, finalized_at ASC so sessions with fewer
        retries are picked up first.
        """
        rows = await self._pool.fetch(
            """
            SELECT *
            FROM mediator.reflection_sessions
            WHERE status = 'processing_failed'
            ORDER BY retry_count ASC, finalized_at ASC
            LIMIT $1
            """,
            limit,
        )
        return [ReflectionSession.from_row(r) for r in rows]

    # ═════════════════════════════════════════════════════════════════════
    # Reflection entry APIs — immutable append-only revisions
    # ═════════════════════════════════════════════════════════════════════

    # ── create_entry ────────────────────────────────────────────────────

    async def create_entry(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        bot_id: str,
        topic_id: UUID | None = None,
        template_key: str | None = None,
        temporal_scope: str | None = None,
        phase: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        timezone_: str | None = None,
        source_message_ids: list[UUID] | None = None,
        payload: dict[str, Any] | None = None,
        plaintext_searchable: str | None = None,
        summary: str | None = None,
        schema_version: int = 1,
        processor_version: str | None = None,
        created_by_turn_id: UUID | None = None,
    ) -> ReflectionEntry:
        """Create a new immutable reflection entry revision.

        This is the primary writer for entries.  It:

        * Validates and normalizes the payload through the template registry
          (see ``app/services/reflection_templates.py``).
        * Encrypts the payload as ``payload_encrypted`` (AES-GCM bytea,
          AGV1 prefix) and the summary as ``summary_encrypted`` following
          the dual-column convention from migration 0007 /
          ``app/services/crypto.py``.  ``plaintext_searchable`` is stored
          as-is for retrieval/embedding use.
        * Assigns the next ``revision_number`` for the session atomically by
          computing ``MAX(revision_number) + 1`` within a CTE that also
          verifies ownership.  Concurrent callers that compute the same
          revision will hit the ``UNIQUE (session_id, revision_number)``
          constraint; the second caller must retry with a fresh number.
        * Preserves ``source_message_ids`` as an ordered array of UUIDs
          — the immutable snapshot of evidence at revision creation time.

        The session must be in ``finalizing`` or ``processed`` status
        (entries cannot be written to collecting sessions).  The caller
        must own the session (``user_id`` ownership check).

        Transaction boundary: this single INSERT is self-contained.
        When a caller wants to atomically claim a session AND create the
        first entry, use :meth:`create_entry_for_claim` which wraps both
        operations in a single database transaction via the connection
        pool's ``transaction()`` context manager.
        """
        import json

        from app.services.crypto import encrypt_value
        from app.services.reflection_templates import validate_entry_payload

        _require_user_id(user_id)
        _require_bot_id(bot_id)
        if template_key is not None and not template_key.strip():
            raise ValueError("template_key must be a non-blank string when set")

        # Validate and normalize the payload through the template registry.
        effective_template_key = template_key or "freeform"
        normalized_payload: dict[str, Any] | None = None
        if payload is not None:
            normalized_payload = validate_entry_payload(
                effective_template_key, payload
            )

        # Encrypt payload and summary.
        payload_json: str | None = None
        if normalized_payload is not None:
            payload_json = json.dumps(normalized_payload, sort_keys=True, default=str)

        payload_encrypted = encrypt_value(payload_json)
        summary_encrypted = encrypt_value(summary)

        now = datetime.now(timezone.utc)
        source_ids = list(source_message_ids or [])

        # Atomic INSERT with revision-number computation via a CTE that
        # also verifies session ownership and status.
        row = await self._pool.fetchrow(
            """
            WITH next_rev AS (
                SELECT COALESCE(MAX(revision_number), 0) + 1 AS rev
                FROM mediator.reflection_entries
                WHERE session_id = $2
            ),
            session_check AS (
                SELECT id, user_id, bot_id, topic_id,
                       template_key AS sess_template_key,
                       temporal_scope, phase,
                       period_start, period_end, timezone
                FROM mediator.reflection_sessions
                WHERE id = $2
                  AND user_id = $1
                  AND status IN ('finalizing', 'processed')
            )
            INSERT INTO mediator.reflection_entries (
                session_id, user_id, topic_id, bot_id,
                template_key, temporal_scope, phase,
                period_start, period_end, timezone,
                source_message_ids,
                payload_encrypted, plaintext_searchable, summary_encrypted,
                schema_version, processor_version,
                revision_number,
                created_by_turn_id,
                created_at
            )
            SELECT
                sc.id, sc.user_id, $3, sc.bot_id,
                $4, sc.temporal_scope, sc.phase,
                sc.period_start, sc.period_end, sc.timezone,
                $5::uuid[],
                $6, $7, $8,
                $9, $10,
                nr.rev,
                $11,
                $12
            FROM session_check sc, next_rev nr
            RETURNING *
            """,
            user_id,
            session_id,
            topic_id,
            effective_template_key,
            source_ids,
            payload_encrypted,
            plaintext_searchable,
            summary_encrypted,
            schema_version,
            processor_version,
            created_by_turn_id,
            now,
        )

        if row is None:
            # Determine why: session missing, wrong user, or wrong status.
            session = await self._pool.fetchrow(
                "SELECT id, user_id, status FROM mediator.reflection_sessions WHERE id = $1",
                session_id,
            )
            if session is None:
                raise SessionNotFoundError(
                    f"Session {session_id} not found"
                )
            if str(session["user_id"]) != str(user_id):
                raise SessionNotFoundError(
                    f"Session {session_id} not found for user {user_id}"
                )
            raise ValueError(
                f"Session {session_id} has status {session['status']!r}; "
                f"entries can only be created for sessions in 'finalizing' or 'processed'"
            )

        logger.info(
            "create_entry: entry=%s session=%s rev=%s template=%s",
            row["id"],
            session_id,
            row["revision_number"],
            effective_template_key,
        )

        entry = ReflectionEntry.from_row(row)

        # ── Embedding lifecycle: enqueue if session is already processed ──
        if plaintext_searchable and plaintext_searchable.strip():
            try:
                session_status = await self._pool.fetchval(
                    "SELECT status FROM mediator.reflection_sessions WHERE id = $1",
                    session_id,
                )
                if session_status == "processed":
                    from app.services.message_embedding_lifecycle import (
                        enqueue_reflection_embed,
                    )

                    await enqueue_reflection_embed(
                        self._pool,
                        entry_id=entry.id,
                        plaintext_searchable=plaintext_searchable,
                    )
            except Exception:
                logger.warning(
                    "create_entry: failed to enqueue reflection embed for entry=%s",
                    entry.id,
                    exc_info=True,
                    extra=redact_for_log_extra(
                        {
                            "entry_id": str(entry.id),
                            "session_id": str(session_id),
                        }
                    ),
                )

        return entry

    # ── create_entry_for_claim ──────────────────────────────────────────

    async def create_entry_for_claim(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        claimed_by: str,
        bot_id: str,
        topic_id: UUID | None = None,
        template_key: str | None = None,
        temporal_scope: str | None = None,
        phase: str | None = None,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        timezone_: str | None = None,
        source_message_ids: list[UUID] | None = None,
        payload: dict[str, Any] | None = None,
        plaintext_searchable: str | None = None,
        summary: str | None = None,
        schema_version: int = 1,
        processor_version: str | None = None,
        created_by_turn_id: UUID | None = None,
        stale_claim_seconds: int = 300,
    ) -> tuple[ReflectionEntry, ReflectionSession]:
        """Atomically claim a finalized session and create its first entry.

        TRANSACTION BOUNDARY
        --------------------
        Both the claim and the entry INSERT execute within a single database
        transaction acquired from the pool::

            async with self._pool.acquire() as conn:
                async with conn.transaction():
                    # 1. Atomic claim via CTE (same as claim_session).
                    # 2. Session ownership + status verification.
                    # 3. revision_number = 1 INSERT.

        If the claim fails (session not found, wrong status, already claimed)
        the entire transaction rolls back — no orphaned entries.  If the entry
        INSERT fails on a constraint violation, the claim also rolls back,
        leaving the session available for the next worker.

        This is the recommended pattern for processors that claim a session
        and immediately produce the first reflection entry.  Using this
        method guarantees that a claimed session always has at least one
        entry committed alongside the claim, or neither exists.

        Returns a ``(entry, session)`` tuple.
        """
        import json

        from app.services.crypto import encrypt_value
        from app.services.reflection_templates import validate_entry_payload

        _require_user_id(user_id)
        _require_bot_id(bot_id)
        if not claimed_by or not claimed_by.strip():
            raise ValueError("claimed_by must be a non-blank string")
        if template_key is not None and not template_key.strip():
            raise ValueError("template_key must be a non-blank string when set")
        if stale_claim_seconds < 0:
            raise ValueError("stale_claim_seconds must be >= 0")

        effective_template_key = template_key or "freeform"
        normalized_payload: dict[str, Any] | None = None
        if payload is not None:
            normalized_payload = validate_entry_payload(
                effective_template_key, payload
            )

        payload_json: str | None = None
        if normalized_payload is not None:
            payload_json = json.dumps(normalized_payload, sort_keys=True, default=str)

        payload_encrypted = encrypt_value(payload_json)
        summary_encrypted = encrypt_value(summary)

        now = datetime.now(timezone.utc)
        source_ids = list(source_message_ids or [])

        async with self._pool.acquire() as conn:
            async with conn.transaction():
                # Step 1: Atomically claim the session.
                claimed_row = await conn.fetchrow(
                    """
                    WITH claimed AS (
                        SELECT id
                        FROM mediator.reflection_sessions
                        WHERE id = $1
                          AND user_id = $2
                          AND status = 'finalizing'
                          AND (
                              claimed_by IS NULL
                              OR claimed_at < $3::timestamptz - make_interval(secs => $4)
                          )
                        FOR UPDATE SKIP LOCKED
                    )
                    UPDATE mediator.reflection_sessions s
                    SET claimed_by   = $5,
                        claimed_at   = $3,
                        retry_count  = CASE
                            WHEN s.claimed_by IS NOT NULL
                            THEN s.retry_count + 1
                            ELSE s.retry_count
                        END,
                        updated_at   = $3
                    FROM claimed c
                    WHERE s.id = c.id
                    RETURNING s.*
                    """,
                    session_id,
                    user_id,
                    now,
                    stale_claim_seconds,
                    claimed_by,
                )

                if claimed_row is None:
                    raise SessionClaimConflictError(
                        f"Cannot claim session {session_id}: "
                        f"not found, wrong user, or already claimed"
                    )

                session = ReflectionSession.from_row(claimed_row)

                # Step 2: Insert the first entry (revision_number = 1).
                entry_row = await conn.fetchrow(
                    """
                    INSERT INTO mediator.reflection_entries (
                        session_id, user_id, topic_id, bot_id,
                        template_key, temporal_scope, phase,
                        period_start, period_end, timezone,
                        source_message_ids,
                        payload_encrypted, plaintext_searchable, summary_encrypted,
                        schema_version, processor_version,
                        revision_number,
                        created_by_turn_id,
                        created_at
                    ) VALUES (
                        $1,  $2,  $3,  $4,
                        $5,  $6,  $7,
                        $8,  $9,  $10,
                        $11::uuid[],
                        $12, $13, $14,
                        $15, $16,
                        1,
                        $17,
                        $18
                    )
                    RETURNING *
                    """,
                    session_id,
                    user_id,
                    topic_id,
                    bot_id,
                    effective_template_key,
                    session.temporal_scope,
                    session.phase,
                    session.period_start,
                    session.period_end,
                    session.timezone,
                    source_ids,
                    payload_encrypted,
                    plaintext_searchable,
                    summary_encrypted,
                    schema_version,
                    processor_version,
                    created_by_turn_id,
                    now,
                )

                entry = ReflectionEntry.from_row(entry_row)

        logger.info(
            "create_entry_for_claim: claimed session=%s entry=%s rev=1",
            session_id,
            entry.id,
        )
        return entry, session

    # ── correct_entry ───────────────────────────────────────────────────

    async def correct_entry(
        self,
        *,
        user_id: UUID,
        supersedes_entry_id: UUID,
        bot_id: str,
        topic_id: UUID | None = None,
        template_key: str | None = None,
        source_message_ids: list[UUID] | None = None,
        payload: dict[str, Any] | None = None,
        plaintext_searchable: str | None = None,
        summary: str | None = None,
        schema_version: int = 1,
        processor_version: str | None = None,
        created_by_turn_id: UUID | None = None,
    ) -> ReflectionEntry:
        """Create a correction — a new current revision superseding an old one.

        Corrections follow the append-only model: the prior entry is **never**
        mutated.  Instead, a new row is inserted with:

        * ``supersedes_entry_id`` pointing to the prior entry.
        * A new ``revision_number`` (``MAX + 1`` for the session).
        * Fresh ``payload_encrypted``, ``summary_encrypted``, and
          ``source_message_ids`` as supplied by the caller.

        The prior entry's ``supersedes_entry_id`` remains unchanged, and its
        row is never touched.  Consumers looking for the current revision
        must select the leaf row that no successor references.

        Raises ``EntryNotFoundError`` if the superseded entry does not exist
        or is not owned by *user_id*.
        Raises ``EntryCorrectionError`` if *supersedes_entry_id* is None.
        """
        import json

        from app.services.crypto import encrypt_value
        from app.services.reflection_templates import validate_entry_payload

        _require_user_id(user_id)
        _require_bot_id(bot_id)
        if supersedes_entry_id is None:
            raise EntryCorrectionError("supersedes_entry_id is required for corrections")
        if template_key is not None and not template_key.strip():
            raise ValueError("template_key must be a non-blank string when set")

        effective_template_key = template_key or "freeform"
        normalized_payload: dict[str, Any] | None = None
        if payload is not None:
            normalized_payload = validate_entry_payload(
                effective_template_key, payload
            )

        payload_json: str | None = None
        if normalized_payload is not None:
            payload_json = json.dumps(normalized_payload, sort_keys=True, default=str)

        payload_encrypted = encrypt_value(payload_json)
        summary_encrypted = encrypt_value(summary)

        now = datetime.now(timezone.utc)
        source_ids = list(source_message_ids or [])

        # Verify the superseded entry exists and is owned by this user,
        # and resolve its session_id, temporal_scope, phase, etc.
        superseded_row = await self._pool.fetchrow(
            """
            SELECT id, session_id, user_id, topic_id, bot_id,
                   template_key, temporal_scope, phase,
                   period_start, period_end, timezone
            FROM mediator.reflection_entries
            WHERE id = $1 AND user_id = $2
            """,
            supersedes_entry_id,
            user_id,
        )

        if superseded_row is None:
            raise EntryNotFoundError(
                f"Entry {supersedes_entry_id} not found for user {user_id}"
            )

        session_id = superseded_row["session_id"]

        # Compute next revision number.
        row = await self._pool.fetchrow(
            """
            WITH next_rev AS (
                SELECT COALESCE(MAX(revision_number), 0) + 1 AS rev
                FROM mediator.reflection_entries
                WHERE session_id = $1
            )
            INSERT INTO mediator.reflection_entries (
                session_id, user_id, topic_id, bot_id,
                template_key, temporal_scope, phase,
                period_start, period_end, timezone,
                source_message_ids,
                payload_encrypted, plaintext_searchable, summary_encrypted,
                schema_version, processor_version,
                revision_number,
                supersedes_entry_id,
                created_by_turn_id,
                created_at
            )
            SELECT
                $1, $2, $3, $4,
                $5, $6, $7,
                $8, $9, $10,
                $11::uuid[],
                $12, $13, $14,
                $15, $16,
                nr.rev,
                $17,
                $18,
                $19
            FROM next_rev nr
            RETURNING *
            """,
            session_id,
            user_id,
            topic_id if topic_id is not None else superseded_row["topic_id"],
            bot_id,
            effective_template_key,
            superseded_row["temporal_scope"],
            superseded_row["phase"],
            superseded_row["period_start"],
            superseded_row["period_end"],
            superseded_row["timezone"],
            source_ids,
            payload_encrypted,
            plaintext_searchable,
            summary_encrypted,
            schema_version,
            processor_version,
            supersedes_entry_id,
            created_by_turn_id,
            now,
        )

        if row is None:
            raise EntryRevisionConflictError(
                f"Failed to insert correction for entry {supersedes_entry_id}"
            )

        logger.info(
            "correct_entry: created correction entry=%s superseding=%s session=%s rev=%s",
            row["id"],
            supersedes_entry_id,
            session_id,
            row["revision_number"],
        )

        entry = ReflectionEntry.from_row(row)

        # ── Embedding lifecycle: embed new + drop old ──
        try:
            from app.services.message_embedding_lifecycle import (
                enqueue_reflection_drop,
                enqueue_reflection_embed,
            )

            # Drop embedding for the superseded entry (no longer current).
            await enqueue_reflection_drop(
                self._pool,
                entry_id=supersedes_entry_id,
            )

            # Enqueue embedding for the new current entry if it has searchable plaintext.
            if plaintext_searchable and plaintext_searchable.strip():
                await enqueue_reflection_embed(
                    self._pool,
                    entry_id=entry.id,
                    plaintext_searchable=plaintext_searchable,
                )
        except Exception:
            logger.warning(
                "correct_entry: failed to enqueue reflection embed lifecycle "
                "for new=%s old=%s",
                entry.id,
                supersedes_entry_id,
                exc_info=True,
                extra=redact_for_log_extra(
                    {
                        "entry_id": str(entry.id),
                        "supersedes_entry_id": str(supersedes_entry_id),
                    }
                ),
            )

        return entry

    # ── get_entry ───────────────────────────────────────────────────────

    async def get_entry(
        self,
        *,
        user_id: UUID,
        entry_id: UUID,
        visible_only: bool = False,
    ) -> ReflectionEntry | None:
        """Fetch a single reflection entry by ID, scoped to *user_id*."""
        _require_user_id(user_id)
        if visible_only:
            visible_where = " AND ".join(["re.id = $1", "re.user_id = $2", *_visible_entry_conditions()])
            row = await self._pool.fetchrow(
                f"""
                SELECT re.*
                FROM mediator.reflection_entries re
                JOIN mediator.reflection_sessions rs
                  ON rs.id = re.session_id
                WHERE {visible_where}
                """,
                entry_id,
                user_id,
            )
        else:
            row = await self._pool.fetchrow(
                """
                SELECT *
                FROM mediator.reflection_entries
                WHERE id = $1 AND user_id = $2
                """,
                entry_id,
                user_id,
            )
        if row is None:
            return None
        return ReflectionEntry.from_row(row)

    # ── get_current_entry ───────────────────────────────────────────────

    async def get_current_entry(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
        visible_only: bool = False,
    ) -> ReflectionEntry | None:
        """Fetch the current (un-superseded) entry for a session.

        The current revision is the leaf row that no successor references.
        """
        _require_user_id(user_id)
        if visible_only:
            visible_where = " AND ".join(
                [
                    "re.session_id = $1",
                    "re.user_id = $2",
                    _current_entry_condition("re"),
                    *_visible_entry_conditions(),
                ]
            )
            row = await self._pool.fetchrow(
                f"""
                SELECT re.*
                FROM mediator.reflection_entries re
                JOIN mediator.reflection_sessions rs
                  ON rs.id = re.session_id
                WHERE {visible_where}
                """,
                session_id,
                user_id,
            )
        else:
            row = await self._pool.fetchrow(
                f"""
                SELECT re.*
                FROM mediator.reflection_entries re
                WHERE re.session_id = $1
                  AND re.user_id = $2
                  AND {_current_entry_condition('re')}
                """,
                session_id,
                user_id,
            )
        if row is None:
            return None
        return ReflectionEntry.from_row(row)

    # ── list_entries ────────────────────────────────────────────────────

    async def list_entries(
        self,
        *,
        user_id: UUID,
        session_id: UUID | None = None,
        bot_id: str | None = None,
        topic_id: UUID | None = None,
        current_only: bool = True,
        limit: int = 50,
        visible_only: bool = False,
    ) -> list[ReflectionEntry]:
        """List reflection entries scoped by user, bot, topic, and session.

        Parameters
        ----------
        user_id:
            Required owner scope.
        session_id:
            If given, restricts results to a single session.
        bot_id:
            If given, restricts results to a specific bot.
        topic_id:
            If given, restricts results to a specific topic.
        current_only:
            When ``True`` (default), only returns the current (un-superseded)
            revision for each session.  Set to ``False`` to return the full
            revision history including superseded entries.
        limit:
            Maximum number of rows to return (default 50).

        Results are ordered by ``created_at DESC``.
        """
        _require_user_id(user_id)

        table_name = "mediator.reflection_entries re"
        select_expr = "re.*"
        conditions = ["re.user_id = $1"]
        if visible_only:
            table_name = "mediator.reflection_entries re JOIN mediator.reflection_sessions rs ON rs.id = re.session_id"
            select_expr = "re.*"
            conditions = ["re.user_id = $1", *_visible_entry_conditions()]
        params: list[Any] = [user_id]
        param_idx = 2

        if session_id is not None:
            field = "re.session_id"
            conditions.append(f"{field} = ${param_idx}")
            params.append(session_id)
            param_idx += 1

        if bot_id is not None:
            _require_bot_id(bot_id)
            field = "re.bot_id"
            conditions.append(f"{field} = ${param_idx}")
            params.append(bot_id)
            param_idx += 1

        if topic_id is not None:
            field = "re.topic_id"
            conditions.append(f"{field} = ${param_idx}")
            params.append(topic_id)
            param_idx += 1

        if current_only:
            conditions.append(_current_entry_condition("re"))

        where_clause = " AND ".join(conditions)
        sql = f"""
            SELECT {select_expr}
            FROM {table_name}
            WHERE {where_clause}
            ORDER BY re.created_at DESC
            LIMIT ${param_idx}
        """
        params.append(limit)

        rows = await self._pool.fetch(sql, *params)
        return [ReflectionEntry.from_row(r) for r in rows]

    # ── get_entry_revision_history ──────────────────────────────────────

    async def get_entry_revision_history(
        self,
        *,
        user_id: UUID,
        session_id: UUID,
    ) -> list[ReflectionEntry]:
        """Return the complete revision chain for a session, oldest first.

        This includes superseded entries so consumers can reconstruct the
        full correction history.
        """
        _require_user_id(user_id)
        rows = await self._pool.fetch(
            """
            SELECT *
            FROM mediator.reflection_entries
            WHERE session_id = $1 AND user_id = $2
            ORDER BY revision_number ASC
            """,
            session_id,
            user_id,
        )
        return [ReflectionEntry.from_row(r) for r in rows]

    # ═════════════════════════════════════════════════════════════════════
    # Reflection derivation APIs — knowledge derivation ledger
    # ═════════════════════════════════════════════════════════════════════
    #
    # Derivations represent knowledge-claims derived from a reflection
    # entry.  They are traceable (every derivation links to the entry and
    # user that produced it), conservative (they carry metadata without
    # implementing M2/M3 write policy), and idempotent (retried submissions
    # with the same idempotency_key return the existing row).

    # ── create_derivation ────────────────────────────────────────────────

    async def create_derivation(
        self,
        *,
        user_id: UUID,
        reflection_entry_id: UUID,
        derivation_kind: str,
        assertion_source: str,
        candidate_payload_encrypted: bytes | None = None,
        confidence: float | None = None,
        eligibility_reasons: list[str] | None = None,
        supporting_message_ids: list[UUID] | None = None,
        decision: str = "deferred",
        applied_target_table: str | None = None,
        applied_target_id: UUID | None = None,
        processor_version: str | None = None,
        processor_turn_id: UUID | None = None,
        idempotency_key: str | None = None,
        decided_at: datetime | None = None,
    ) -> ReflectionDerivation:
        """Record a derivation decision traceable to a reflection entry.

        Parameters
        ----------
        user_id:
            Owner scope — must own the referenced entry.
        reflection_entry_id:
            The reflection entry from which this knowledge is derived.
        derivation_kind:
            One of ``memory``, ``observation``, ``distillation``,
            ``orientation``.
        assertion_source:
            Provenance strength: ``user_explicit``, ``user_implied``, or
            ``agent_inferred``.
        candidate_payload_encrypted:
            Encrypted candidate payload (what the processor proposed to
            write to the target system).  Encryption is the caller's
            responsibility; this method stores the bytes as-is.
        confidence:
            Optional confidence score in [0, 1].
        eligibility_reasons:
            Deterministic eligibility rule keys that fired (JSON array).
        supporting_message_ids:
            Exact supporting message IDs within the reflection entry.
        decision:
            Initial decision (default ``deferred``).  Must be one of
            ``applied``, ``reinforced``, ``deferred``, ``rejected``,
            ``superseded``.
        applied_target_table / applied_target_id:
            When ``decision='applied'``, the target durable row that was
            written.  Both must be set together.
        idempotency_key:
            If provided, retried submissions with the same key return the
            existing derivation instead of creating a duplicate.  The
            database UNIQUE constraint on ``idempotency_key`` enforces
            this regardless of concurrent callers.

        Returns
        -------
        ReflectionDerivation
            The newly created derivation, or the existing one if an
            idempotency_key was provided and a matching row already exists.
        """
        import json

        _require_user_id(user_id)
        _validate_derivation_kind(derivation_kind)
        _validate_assertion_source(assertion_source)
        _validate_decision(decision)

        if confidence is not None and not (0 <= confidence <= 1):
            raise ValueError(
                f"confidence must be in [0, 1], got {confidence}"
            )

        if decision == "applied":
            if not applied_target_table or applied_target_id is None:
                raise DerivationDecisionError(
                    "decision='applied' requires both applied_target_table "
                    "and applied_target_id"
                )

        # Verify the referenced entry exists and is owned by user_id.
        entry = await self._pool.fetchrow(
            f"""
            SELECT re.id, re.user_id, re.session_id
            FROM mediator.reflection_entries re
            JOIN mediator.reflection_sessions rs
              ON rs.id = re.session_id
            WHERE re.id = $1
              AND re.user_id = $2
              AND {' AND '.join(_visible_entry_conditions())}
            """,
            reflection_entry_id,
            user_id,
        )
        if entry is None:
            raise EntryNotFoundError(
                f"Entry {reflection_entry_id} not found for user {user_id}"
            )

        eligibility_json: str | None = None
        if eligibility_reasons is not None:
            eligibility_json = json.dumps(eligibility_reasons, sort_keys=True)

        now = datetime.now(timezone.utc)
        support_ids = list(supporting_message_ids or [])

        # If idempotency_key is provided, attempt INSERT and catch unique
        # violation to return the existing row.
        if idempotency_key:
            try:
                row = await self._pool.fetchrow(
                    """
                    INSERT INTO mediator.reflection_derivations (
                        reflection_entry_id, user_id,
                        derivation_kind,
                        candidate_payload_encrypted,
                        assertion_source, confidence,
                        eligibility_reasons,
                        supporting_message_ids,
                        decision,
                        applied_target_table, applied_target_id,
                        processor_version, processor_turn_id,
                        idempotency_key,
                        created_at, decided_at
                    ) VALUES (
                        $1,  $2,
                        $3,
                        $4,
                        $5,  $6,
                        $7::jsonb,
                        $8::uuid[],
                        $9,
                        $10, $11,
                        $12, $13,
                        $14,
                        $15, $16
                    )
                    RETURNING *
                    """,
                    reflection_entry_id,
                    user_id,
                    derivation_kind,
                    candidate_payload_encrypted,
                    assertion_source,
                    confidence,
                    eligibility_json,
                    support_ids,
                    decision,
                    applied_target_table,
                    applied_target_id,
                    processor_version,
                    processor_turn_id,
                    idempotency_key,
                    now,
                    decided_at,
                )
                logger.info(
                    "create_derivation: derivation=%s entry=%s kind=%s "
                    "decision=%s",
                    row["id"],
                    reflection_entry_id,
                    derivation_kind,
                    decision,
                )
                return ReflectionDerivation.from_row(row)

            except Exception as exc:
                exc_name = type(exc).__name__
                if "UniqueViolation" in exc_name or "unique" in str(exc).lower():
                    # Idempotent retry: return the existing row.
                    logger.debug(
                        "create_derivation: idempotency_key collision for "
                        "key=%r, re-reading",
                        idempotency_key,
                    )
                    existing = await self._pool.fetchrow(
                        """
                        SELECT *
                        FROM mediator.reflection_derivations
                        WHERE idempotency_key = $1
                        """,
                        idempotency_key,
                    )
                    if existing is not None:
                        return ReflectionDerivation.from_row(existing)
                    # Edge: violated unique but row vanished — retry insert.
                    logger.warning(
                        "create_derivation: unique violation but no "
                        "matching row for idempotency_key=%r — retrying",
                        idempotency_key,
                    )
                    row = await self._pool.fetchrow(
                        """
                        INSERT INTO mediator.reflection_derivations (
                            reflection_entry_id, user_id,
                            derivation_kind,
                            candidate_payload_encrypted,
                            assertion_source, confidence,
                            eligibility_reasons,
                            supporting_message_ids,
                            decision,
                            applied_target_table, applied_target_id,
                            processor_version, processor_turn_id,
                            idempotency_key,
                            created_at, decided_at
                        ) VALUES (
                            $1,  $2,
                            $3,
                            $4,
                            $5,  $6,
                            $7::jsonb,
                            $8::uuid[],
                            $9,
                            $10, $11,
                            $12, $13,
                            $14,
                            $15, $16
                        )
                        RETURNING *
                        """,
                        reflection_entry_id,
                        user_id,
                        derivation_kind,
                        candidate_payload_encrypted,
                        assertion_source,
                        confidence,
                        eligibility_json,
                        support_ids,
                        decision,
                        applied_target_table,
                        applied_target_id,
                        processor_version,
                        processor_turn_id,
                        idempotency_key,
                        now,
                        decided_at,
                    )
                    return ReflectionDerivation.from_row(row)
                raise
        else:
            # No idempotency key — straightforward INSERT.
            row = await self._pool.fetchrow(
                """
                INSERT INTO mediator.reflection_derivations (
                    reflection_entry_id, user_id,
                    derivation_kind,
                    candidate_payload_encrypted,
                    assertion_source, confidence,
                    eligibility_reasons,
                    supporting_message_ids,
                    decision,
                    applied_target_table, applied_target_id,
                    processor_version, processor_turn_id,
                    idempotency_key,
                    created_at, decided_at
                ) VALUES (
                    $1,  $2,
                    $3,
                    $4,
                    $5,  $6,
                    $7::jsonb,
                    $8::uuid[],
                    $9,
                    $10, $11,
                    $12, $13,
                    $14,
                    $15, $16
                )
                RETURNING *
                """,
                reflection_entry_id,
                user_id,
                derivation_kind,
                candidate_payload_encrypted,
                assertion_source,
                confidence,
                eligibility_json,
                support_ids,
                decision,
                applied_target_table,
                applied_target_id,
                processor_version,
                processor_turn_id,
                None,  # idempotency_key
                now,
                decided_at,
            )
            logger.info(
                "create_derivation: derivation=%s entry=%s kind=%s decision=%s",
                row["id"],
                reflection_entry_id,
                derivation_kind,
                decision,
            )
            return ReflectionDerivation.from_row(row)

    # ── get_derivation ───────────────────────────────────────────────────

    async def get_derivation(
        self,
        *,
        user_id: UUID,
        derivation_id: UUID,
    ) -> ReflectionDerivation | None:
        """Fetch a single derivation by ID, scoped to *user_id*."""
        _require_user_id(user_id)
        row = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.reflection_derivations
            WHERE id = $1 AND user_id = $2
            """,
            derivation_id,
            user_id,
        )
        if row is None:
            return None
        return ReflectionDerivation.from_row(row)

    # ── get_derivation_by_idempotency_key ────────────────────────────────

    async def get_derivation_by_idempotency_key(
        self,
        *,
        user_id: UUID,
        idempotency_key: str,
    ) -> ReflectionDerivation | None:
        """Look up a derivation by its idempotency_key, scoped to user."""
        _require_user_id(user_id)
        if not idempotency_key or not idempotency_key.strip():
            raise ValueError("idempotency_key must be a non-blank string")
        row = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.reflection_derivations
            WHERE idempotency_key = $1 AND user_id = $2
            """,
            idempotency_key,
            user_id,
        )
        if row is None:
            return None
        return ReflectionDerivation.from_row(row)

    # ── list_derivations_for_entry ───────────────────────────────────────

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
        Results ordered by ``created_at ASC``.
        """
        _require_user_id(user_id)

        conditions = ["reflection_entry_id = $1", "user_id = $2"]
        params: list[Any] = [reflection_entry_id, user_id]
        param_idx = 3

        if derivation_kind is not None:
            _validate_derivation_kind(derivation_kind)
            conditions.append(f"derivation_kind = ${param_idx}")
            params.append(derivation_kind)
            param_idx += 1

        if decision is not None:
            _validate_decision(decision)
            conditions.append(f"decision = ${param_idx}")
            params.append(decision)
            param_idx += 1

        where_clause = " AND ".join(conditions)
        sql = f"""
            SELECT *
            FROM mediator.reflection_derivations
            WHERE {where_clause}
            ORDER BY created_at ASC
            LIMIT ${param_idx}
        """
        params.append(limit)

        rows = await self._pool.fetch(sql, *params)
        return [ReflectionDerivation.from_row(r) for r in rows]

    # ── list_derivations_for_session ─────────────────────────────────────

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

        Joins through ``reflection_entries`` to find all derivations
        belonging to any entry in the session.  Ownership is verified
        on both the session and the derivations.

        Optionally filter by *derivation_kind* and/or *decision*.
        Results ordered by ``created_at ASC``.
        """
        _require_user_id(user_id)

        conditions = [
            "d.user_id = $1",
            "e.session_id = $2",
        ]
        params: list[Any] = [user_id, session_id]
        param_idx = 3

        if derivation_kind is not None:
            _validate_derivation_kind(derivation_kind)
            conditions.append(f"d.derivation_kind = ${param_idx}")
            params.append(derivation_kind)
            param_idx += 1

        if decision is not None:
            _validate_decision(decision)
            conditions.append(f"d.decision = ${param_idx}")
            params.append(decision)
            param_idx += 1

        where_clause = " AND ".join(conditions)
        sql = f"""
            SELECT d.*
            FROM mediator.reflection_derivations d
            JOIN mediator.reflection_entries e ON e.id = d.reflection_entry_id
            WHERE {where_clause}
            ORDER BY d.created_at ASC
            LIMIT ${param_idx}
        """
        params.append(limit)

        rows = await self._pool.fetch(sql, *params)
        return [ReflectionDerivation.from_row(r) for r in rows]

    # ── update_derivation_decision ───────────────────────────────────────

    async def update_derivation_decision(
        self,
        *,
        user_id: UUID,
        derivation_id: UUID,
        decision: str,
        applied_target_table: str | None = None,
        applied_target_id: UUID | None = None,
        processor_version: str | None = None,
    ) -> ReflectionDerivation:
        """Update the decision on an existing derivation.

        This is the sole mutation path for derivations: it transitions
        ``decision`` from ``deferred`` to a terminal value (``applied``,
        ``reinforced``, ``rejected``, ``superseded``) and records
        ``decided_at``.  When ``decision='applied'``, both
        ``applied_target_table`` and ``applied_target_id`` must be
        provided.

        Parameters that are not provided are left unchanged.
        """
        _require_user_id(user_id)
        _validate_decision(decision)

        if decision == "applied":
            if not applied_target_table or applied_target_id is None:
                raise DerivationDecisionError(
                    "decision='applied' requires both applied_target_table "
                    "and applied_target_id"
                )

        now = datetime.now(timezone.utc)

        # Build SET clause dynamically for optional fields.
        set_parts = [
            "decision = $3",
            "decided_at = $4",
        ]
        params: list[Any] = [derivation_id, user_id, decision, now]
        param_idx = 5

        if applied_target_table is not None:
            set_parts.append(f"applied_target_table = ${param_idx}")
            params.append(applied_target_table)
            param_idx += 1
        else:
            set_parts.append("applied_target_table = NULL")

        if applied_target_id is not None:
            set_parts.append(f"applied_target_id = ${param_idx}")
            params.append(applied_target_id)
            param_idx += 1
        else:
            set_parts.append("applied_target_id = NULL")

        if processor_version is not None:
            set_parts.append(f"processor_version = ${param_idx}")
            params.append(processor_version)
            param_idx += 1

        set_clause = ", ".join(set_parts)

        row = await self._pool.fetchrow(
            f"""
            UPDATE mediator.reflection_derivations
            SET {set_clause}
            WHERE id = $1 AND user_id = $2
            RETURNING *
            """,
            *params,
        )

        if row is None:
            existing = await self._pool.fetchrow(
                "SELECT id, user_id FROM mediator.reflection_derivations WHERE id = $1",
                derivation_id,
            )
            if existing is None:
                raise DerivationNotFoundError(
                    f"Derivation {derivation_id} not found"
                )
            raise DerivationNotFoundError(
                f"Derivation {derivation_id} not found for user {user_id}"
            )

        logger.info(
            "update_derivation_decision: derivation=%s decision=%s",
            derivation_id,
            decision,
        )
        return ReflectionDerivation.from_row(row)


# ── Admin / operator listing ────────────────────────────────────────────────


async def admin_list_sessions(
    pool: Any,
    *,
    status_filter: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return reflection sessions for the operator admin page.

    Returns only redaction-safe metadata columns — no payload text,
    encrypted bodies, or plaintext searchable content.  Each row is a
    flat dict suitable for HTML table rendering.

    Args:
        pool: An asyncpg pool or pool-like object.
        status_filter: Optional status to filter by (one of
            ``VALID_STATUSES``).
        limit: Maximum number of rows to return (default 100).

    Returns:
        A list of dicts, each representing one reflection session with
        aggregated entry and derivation counts plus embedding-coverage
        indicator.
    """
    conditions: list[str] = []
    params: list[Any] = []
    param_idx = 1

    if status_filter is not None:
        _validate_status(status_filter)
        conditions.append(f"rs.status = ${param_idx}")
        params.append(status_filter)
        param_idx += 1

    where_clause = ""
    if conditions:
        where_clause = "WHERE " + " AND ".join(conditions)

    sql = f"""
        SELECT
            rs.id,
            rs.user_id,
            rs.bot_id,
            rs.topic_id,
            rs.template_key,
            rs.temporal_scope,
            rs.phase,
            rs.status,
            rs.classification_source,
            rs.classification_confidence,
            rs.retry_count,
            rs.failure_class,
            rs.failure_reason,
            rs.last_error,
            rs.claimed_by,
            rs.claimed_at,
            rs.created_at,
            rs.finalized_at,
            rs.processed_at,
            rs.abandoned_at,
            rs.idle_finalize_at,
            rs.updated_at,
            rs.idempotency_key,
            COALESCE(
                (SELECT COUNT(*)
                 FROM mediator.reflection_entries re
                 WHERE re.session_id = rs.id
                   AND {_current_entry_condition('re')}),
                0
            ) AS entry_count,
            COALESCE(
                (SELECT COUNT(*)
                 FROM mediator.reflection_derivations rd
                 WHERE rd.reflection_entry_id IN (
                     SELECT re2.id
                     FROM mediator.reflection_entries re2
                     WHERE re2.session_id = rs.id
                       AND {_current_entry_condition('re2')}
                 )),
                0
            ) AS derivation_count,
            EXISTS (
                SELECT 1
                FROM mediator.reflection_entries re3
                WHERE re3.session_id = rs.id
                  AND {_current_entry_condition('re3')}
                  AND re3.plaintext_searchable IS NOT NULL
                  AND btrim(re3.plaintext_searchable) <> ''
            ) AS has_embeddable_entries
        FROM mediator.reflection_sessions rs
        {where_clause}
          {"AND" if where_clause else "WHERE"} {_session_source_alive_condition("rs")}
        ORDER BY rs.created_at DESC
        LIMIT ${param_idx}
    """
    params.append(limit)
    rows = await pool.fetch(sql, *params)
    return [dict(row) for row in rows]


# ── Module-level convenience ────────────────────────────────────────────────

__all__ = [
    "ReflectionSession",
    "ReflectionEntry",
    "ReflectionDerivation",
    "ReflectionStore",
    "SessionNotFoundError",
    "SessionNotCollectingError",
    "SessionNotFinalizingError",
    "SessionClaimConflictError",
    "SessionFinalizeConflictError",
    "EntryNotFoundError",
    "EntryRevisionConflictError",
    "EntryCorrectionError",
    "DerivationNotFoundError",
    "DerivationIdempotencyConflictError",
    "DerivationDecisionError",
    "VALID_STATUSES",
    "VALID_TEMPORAL_SCOPES",
    "VALID_PHASES",
    "VALID_FAILURE_CLASSES",
    "VALID_DERIVATION_KINDS",
    "VALID_ASSERTION_SOURCES",
    "VALID_DECISIONS",
]
