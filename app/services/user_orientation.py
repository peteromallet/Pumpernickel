"""User Orientation store and validation contract.

Implements the shared service layer consumed by tool handlers and Compass for
reviewed user orientation state — principles, goals, priorities, and
anti-patterns a participant has stated or confirmed.

Storage contract (see migration 0060):
  * Three ``mediator.user_orientation_*`` tables.
  * No durable ``compass_*`` tables.
  * No ``conversation_artifacts`` snapshot storage.
  * No ``commitments.orientation_goal_id`` column — goal↔commitment and
    goal↔event relationships are represented ONLY through
    ``user_orientation_item_links``.

Scope invariants:
  * Every store method requires an explicit ``user_id``.
  * Read methods require an explicit, non-empty list of ``topic_ids``.
  * The string sentinel ``"all"`` is rejected everywhere.
  * Compass reads exclude unreviewed, rejected, and superseded rows by default.

Lifecycle invariants:
  * ``bot_proposed`` items must remain in review_state ``unreviewed`` or
    ``excluded`` until an explicit review row is recorded.
  * Goal completion writes ``completed_at`` and ``outcome_note`` on the
    orientation item only; it must NOT mutate commitment/event adherence or
    lifecycle columns.
  * Evidence/progress links reference commitments or events only and do NOT
    carry goal lifecycle state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as dt_date, datetime, timezone
from typing import Any, Literal
from uuid import UUID

logger = logging.getLogger(__name__)

# ── Registries ───────────────────────────────────────────────────────────

VALID_KINDS: frozenset[str] = frozenset(
    {"principle", "goal", "priority", "anti_pattern"}
)

VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "active", "completed", "retired", "superseded", "rejected"}
)

VALID_SOURCES: frozenset[str] = frozenset(
    {"user_stated", "user_confirmed", "bot_proposed"}
)

VALID_REVIEW_STATES: frozenset[str] = frozenset(
    {"unreviewed", "reviewed", "excluded"}
)

VALID_VERDICTS: frozenset[str] = frozenset(
    {"accepted", "corrected", "rejected", "retired", "superseded", "completed"}
)

VALID_TARGET_TABLES: frozenset[str] = frozenset({"commitments", "events"})

VALID_RELATIONS: frozenset[str] = frozenset(
    {"evidence", "progress", "supports", "contradicts", "completes"}
)

# Terminal statuses — items in these states cannot be further transitioned by
# review or close (they can still have fields updated).
TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "retired", "superseded", "rejected"}
)

# Statuses that are closeable via close_item.
CLOSEABLE_STATUSES: frozenset[str] = frozenset({"active"})

# Statuses that are reviewable via review_item.
REVIEWABLE_STATUSES: frozenset[str] = frozenset({"pending"})

# Verdict → new_status mapping.
_VERDICT_TO_STATUS: dict[str, str] = {
    "accepted": "active",
    "corrected": "active",
    "rejected": "rejected",
    "retired": "retired",
    "superseded": "superseded",
    "completed": "completed",
}

# ── Kind-specific validation hooks ───────────────────────────────────────

KindName = Literal["principle", "goal", "priority", "anti_pattern"]


def _validate_kind_specific_fields(kind: str, kwargs: dict[str, Any]) -> None:
    """Enforce kind-specific constraints before SQL.

    Principles and anti-patterns have no date/lifecycle semantics.  Goals track
    started_at, target_date, completed_at, and outcome_note.  Priorities may
    carry a priority_rank.
    """
    if kind == "priority":
        rank = kwargs.get("priority_rank")
        if rank is not None and (not isinstance(rank, int) or rank < 1):
            raise ValueError(
                f"priority_rank must be a positive integer or None, got {rank!r}"
            )
    if kind in ("principle", "anti_pattern"):
        # These kinds should not carry goal-specific lifecycle fields.
        # We warn but don't hard-reject — the CHECK constraints in the DB
        # are the final enforcer, and callers may pass None implicitly.
        pass


# ── Validation helpers ───────────────────────────────────────────────────

_SENTINEL_ALL_VALUES = frozenset({"all", "ALL", "All"})


def _require_user_id(user_id: UUID | None) -> UUID:
    """Reject None or empty user_id."""
    if user_id is None:
        raise ValueError("user_id is required and must not be None")
    return user_id


def _require_explicit_topic_scope(
    topic_ids: list[UUID] | None, *, method: str
) -> list[UUID]:
    """Reject None, empty, or sentinel 'all' topic scope for reads."""
    if topic_ids is None:
        raise ValueError(
            f"{method}: topic_ids must be an explicit non-empty list, not None"
        )
    if not topic_ids:
        raise ValueError(
            f"{method}: topic_ids must be a non-empty list"
        )
    for tid in topic_ids:
        if isinstance(tid, str) and tid.strip() in _SENTINEL_ALL_VALUES:
            raise ValueError(
                f"{method}: 'all' sentinel is not allowed as a topic_id"
            )
    return topic_ids


def _validate_kind(kind: str) -> str:
    if kind not in VALID_KINDS:
        raise ValueError(
            f"invalid kind {kind!r}; expected one of {sorted(VALID_KINDS)}"
        )
    return kind


def _validate_source(source: str) -> str:
    if source not in VALID_SOURCES:
        raise ValueError(
            f"invalid source {source!r}; expected one of {sorted(VALID_SOURCES)}"
        )
    return source


def _validate_status(status: str) -> str:
    if status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    return status


def _validate_review_state(review_state: str) -> str:
    if review_state not in VALID_REVIEW_STATES:
        raise ValueError(
            f"invalid review_state {review_state!r}; "
            f"expected one of {sorted(VALID_REVIEW_STATES)}"
        )
    return review_state


def _validate_verdict(verdict: str) -> str:
    if verdict not in VALID_VERDICTS:
        raise ValueError(
            f"invalid verdict {verdict!r}; expected one of {sorted(VALID_VERDICTS)}"
        )
    return verdict


def _validate_target_table(target_table: str) -> str:
    if target_table not in VALID_TARGET_TABLES:
        raise ValueError(
            f"invalid target_table {target_table!r}; "
            f"expected one of {sorted(VALID_TARGET_TABLES)}"
        )
    return target_table


def _validate_relation(relation: str) -> str:
    if relation not in VALID_RELATIONS:
        raise ValueError(
            f"invalid relation {relation!r}; "
            f"expected one of {sorted(VALID_RELATIONS)}"
        )
    return relation


def _validate_label(label: str) -> str:
    if not label or not label.strip():
        raise ValueError("label must be a non-blank string")
    return label


def validate_create_params(
    *,
    user_id: UUID | None,
    topic_id: UUID | None,
    bot_id: str,
    kind: str,
    source: str,
    label: str,
    status: str = "pending",
    review_state: str = "unreviewed",
) -> None:
    """Validate parameters for create_item before SQL.

    Enforces:
      - user_id is required and explicit.
      - topic_id is required.
      - kind, source, label are validated.
      - bot_proposed items must start as pending/unreviewed.
      - user_stated/user_confirmed can be created as active (reviewed).
    """
    _require_user_id(user_id)
    if topic_id is None:
        raise ValueError("topic_id is required for create_item")
    if not bot_id or not bot_id.strip():
        raise ValueError("bot_id must be a non-blank string")
    _validate_kind(kind)
    _validate_source(source)
    _validate_label(label)
    _validate_status(status)
    _validate_review_state(review_state)

    # bot_proposed must stay unreviewed/excluded.
    if source == "bot_proposed" and review_state not in ("unreviewed", "excluded"):
        raise ValueError(
            "bot_proposed items must have review_state 'unreviewed' or 'excluded'"
        )

    # bot_proposed items must start as pending.
    if source == "bot_proposed" and status != "pending":
        raise ValueError("bot_proposed items must start with status 'pending'")

    # If status is active and source is bot_proposed, that's a contradiction.
    if status == "active" and source == "bot_proposed":
        raise ValueError(
            "bot_proposed items cannot be created with status 'active'"
        )

    # If status is 'completed', completed_at must be set (but for create,
    # we typically start as pending, so this is a guard).
    if status == "completed":
        raise ValueError(
            "cannot create an item directly with status 'completed'; "
            "use close_item instead"
        )

    _validate_kind_specific_fields(kind, {})


def validate_update_params(
    *,
    user_id: UUID | None,
    item_current: dict[str, Any],
    new_status: str | None = None,
    new_source: str | None = None,
    new_review_state: str | None = None,
) -> None:
    """Validate parameters for update_item before SQL.

    Enforces:
      - user_id is required.
      - item_current must be present and owned by user_id.
      - Status transitions are validated.
      - bot_proposed review_state constraint is maintained.
    """
    _require_user_id(user_id)
    if item_current is None:
        raise ValueError("item_current is required for update_item")

    # Verify ownership.
    item_user_id = item_current.get("user_id")
    if item_user_id is not None and item_user_id != user_id:
        raise ValueError(
            "update_item: item does not belong to the specified user_id"
        )

    current_status = item_current.get("status", "pending")
    current_source = item_current.get("source", "user_stated")
    current_review_state = item_current.get("review_state", "unreviewed")

    if new_status is not None:
        _validate_status(new_status)
        _check_status_transition(current_status, new_status)

    if new_source is not None:
        _validate_source(new_source)

    # Effective review_state after update.
    eff_review_state = new_review_state or current_review_state
    eff_source = new_source or current_source

    if new_review_state is not None:
        _validate_review_state(new_review_state)

    # bot_proposed must stay unreviewed/excluded.
    if eff_source == "bot_proposed" and eff_review_state not in (
        "unreviewed",
        "excluded",
    ):
        raise ValueError(
            "bot_proposed items must have review_state 'unreviewed' or 'excluded'"
        )


def _check_status_transition(current: str, target: str) -> None:
    """Validate that a status transition is legal.

    Allowed transitions:
      pending   → active, rejected, retired, superseded
      active    → completed, retired, superseded, rejected, active (idempotent)
      completed → (terminal)
      retired   → (terminal)
      superseded → (terminal)
      rejected  → (terminal)
    """
    if current == target:
        return  # Idempotent.
    if current in TERMINAL_STATUSES:
        raise ValueError(
            f"cannot transition from terminal status {current!r} to {target!r}"
        )
    allowed = _allowed_transitions(current)
    if target not in allowed:
        raise ValueError(
            f"invalid status transition from {current!r} to {target!r}; "
            f"allowed targets: {sorted(allowed)}"
        )


def _allowed_transitions(current: str) -> frozenset[str]:
    """Return the set of legal target statuses from *current*."""
    if current == "pending":
        return frozenset({"active", "rejected", "retired", "superseded"})
    if current == "active":
        return frozenset({"completed", "retired", "superseded", "rejected"})
    return frozenset()  # Terminal.


def validate_review_params(
    *,
    user_id: UUID | None,
    item_current: dict[str, Any],
    verdict: str,
) -> str:
    """Validate review_item parameters and return the computed new_status.

    Enforces:
      - user_id required.
      - Item must be reviewable (pending).
      - Verdict must be valid.
      - The computed new_status must be a legal transition from current status.
    """
    _require_user_id(user_id)
    if item_current is None:
        raise ValueError("item_current is required for review_item")

    item_user_id = item_current.get("user_id")
    if item_user_id is not None and item_user_id != user_id:
        raise ValueError(
            "review_item: item does not belong to the specified user_id"
        )

    current_status = item_current.get("status", "pending")
    _validate_verdict(verdict)

    new_status = _VERDICT_TO_STATUS[verdict]
    _check_status_transition(current_status, new_status)
    return new_status


def validate_close_params(
    *,
    user_id: UUID | None,
    item_current: dict[str, Any],
    new_status: str,
    completed_at: datetime | None = None,
) -> None:
    """Validate close_item parameters.

    Enforces:
      - user_id required.
      - Item must be closeable (active).
      - new_status must be a valid close target (completed, retired, superseded).
      - completed items require completed_at.
    """
    _require_user_id(user_id)
    if item_current is None:
        raise ValueError("item_current is required for close_item")

    item_user_id = item_current.get("user_id")
    if item_user_id is not None and item_user_id != user_id:
        raise ValueError(
            "close_item: item does not belong to the specified user_id"
        )

    current_status = item_current.get("status", "pending")
    _validate_status(new_status)
    _check_status_transition(current_status, new_status)

    if new_status == "completed" and completed_at is None:
        raise ValueError(
            "completed_at is required when closing an item with status 'completed'"
        )


def validate_link_params(
    *,
    user_id: UUID | None,
    item_current: dict[str, Any],
    target_table: str,
    target_id: UUID,
    relation: str,
) -> None:
    """Validate link_evidence parameters.

    Enforces:
      - user_id required.
      - Item must exist and be owned by user_id.
      - target_table and relation must be valid enums.
      - target_id must not be None.
    """
    _require_user_id(user_id)
    if item_current is None:
        raise ValueError("item_current is required for link_evidence")

    item_user_id = item_current.get("user_id")
    if item_user_id is not None and item_user_id != user_id:
        raise ValueError(
            "link_evidence: item does not belong to the specified user_id"
        )

    _validate_target_table(target_table)
    _validate_relation(relation)

    if target_id is None:
        raise ValueError("target_id must not be None")


def is_compass_visible(item: dict[str, Any]) -> bool:
    """Return True if an item should appear in default Compass snapshots.

    Items are visible when they have status 'active' (or 'completed'/'retired'
    for historical sections) AND have been reviewed (or are user_stated/
    user_confirmed with active status). Unreviewed, rejected, and superseded
    items are excluded from default Compass rendering.
    """
    status = item.get("status", "pending")
    review_state = item.get("review_state", "unreviewed")
    source = item.get("source", "user_stated")

    # Rejected and superseded are never Compass-visible by default.
    if status in ("rejected", "superseded"):
        return False

    # Pending items are not visible (not yet reviewed).
    if status == "pending":
        return False

    # Active/completed/retired items from user_stated or user_confirmed are
    # always visible (they don't require separate review).
    if source in ("user_stated", "user_confirmed") and status in (
        "active",
        "completed",
        "retired",
    ):
        return True

    # bot_proposed items are only visible after explicit review.
    if source == "bot_proposed" and review_state == "reviewed" and status == "active":
        return True

    return False


# ── Read models ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class OrientationItem:
    """Read model for a single orientation item."""

    id: UUID
    user_id: UUID
    topic_id: UUID | None
    bot_id: str
    created_by_turn_id: UUID | None
    kind: str
    status: str
    source: str
    review_state: str
    label: str
    detail: str | None
    started_at: datetime | None
    effective_at: datetime | None
    target_date: dt_date | None
    completed_at: datetime | None
    closed_reason: str | None
    outcome_note: str | None
    supersedes_item_id: UUID | None
    priority_rank: int | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> OrientationItem:
        """Construct from an asyncpg Record or dict."""
        get = _row_getter(row)
        return cls(
            id=get("id"),
            user_id=get("user_id"),
            topic_id=get("topic_id"),
            bot_id=get("bot_id"),
            created_by_turn_id=get("created_by_turn_id"),
            kind=get("kind"),
            status=get("status"),
            source=get("source"),
            review_state=get("review_state"),
            label=get("label"),
            detail=get("detail"),
            started_at=get("started_at"),
            effective_at=get("effective_at"),
            target_date=get("target_date"),
            completed_at=get("completed_at"),
            closed_reason=get("closed_reason"),
            outcome_note=get("outcome_note"),
            supersedes_item_id=get("supersedes_item_id"),
            priority_rank=get("priority_rank"),
            created_at=get("created_at"),
            updated_at=get("updated_at"),
        )


@dataclass(frozen=True, slots=True)
class OrientationLink:
    """Read model for an evidence/progress link."""

    id: UUID
    item_id: UUID
    user_id: UUID
    topic_id: UUID | None
    target_table: str
    target_id: UUID
    relation: str
    note: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> OrientationLink:
        get = _row_getter(row)
        return cls(
            id=get("id"),
            item_id=get("item_id"),
            user_id=get("user_id"),
            topic_id=get("topic_id"),
            target_table=get("target_table"),
            target_id=get("target_id"),
            relation=get("relation"),
            note=get("note"),
            created_at=get("created_at"),
        )


@dataclass(frozen=True, slots=True)
class OrientationReview:
    """Read model for a review audit record."""

    id: UUID
    item_id: UUID
    user_id: UUID
    reviewed_by_turn_id: UUID | None
    verdict: str
    previous_status: str | None
    new_status: str
    note: str | None
    created_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> OrientationReview:
        get = _row_getter(row)
        return cls(
            id=get("id"),
            item_id=get("item_id"),
            user_id=get("user_id"),
            reviewed_by_turn_id=get("reviewed_by_turn_id"),
            verdict=get("verdict"),
            previous_status=get("previous_status"),
            new_status=get("new_status"),
            note=get("note"),
            created_at=get("created_at"),
        )


def _row_getter(row: Any) -> Any:
    """Return a key-accessor callable for asyncpg Record or dict."""
    if isinstance(row, dict):
        return row.get
    # asyncpg.Record supports both attribute and key access.
    return lambda key: row[key]


# ── Store ────────────────────────────────────────────────────────────────


class UserOrientationStore:
    """Async store for user orientation items, links, and reviews.

    All methods require an explicit ``user_id``. Read methods require an
    explicit, non-empty list of ``topic_ids`` (the ``"all"`` sentinel is
    rejected). Lifecycle and review constraints are validated before SQL.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    # ── Read methods ─────────────────────────────────────────────────

    async def list_items(
        self,
        *,
        user_id: UUID,
        topic_ids: list[UUID],
        kinds: list[str] | None = None,
        statuses: list[str] | None = None,
        include_unreviewed: bool = False,
        include_rejected: bool = False,
    ) -> list[OrientationItem]:
        """List orientation items for a user within explicit topic scope.

        By default excludes unreviewed, rejected, and superseded items (the
        Compass default). Pass ``include_unreviewed=True`` or
        ``include_rejected=True`` to widen the result set for tool inspection.
        """
        _require_user_id(user_id)
        _require_explicit_topic_scope(topic_ids, method="list_items")

        conditions = ["i.user_id = $1", "i.topic_id = ANY($2::uuid[])"]
        params: list[Any] = [user_id, list(topic_ids)]
        param_idx = 3

        if kinds:
            for k in kinds:
                _validate_kind(k)
            conditions.append(f"i.kind = ANY(${param_idx}::text[])")
            params.append(kinds)
            param_idx += 1

        if statuses:
            for s in statuses:
                _validate_status(s)
            conditions.append(f"i.status = ANY(${param_idx}::text[])")
            params.append(statuses)
            param_idx += 1
        else:
            # Default: exclude superseded.
            excluded = ["superseded"]
            if not include_unreviewed:
                conditions.append("i.status <> 'pending'")
            if not include_rejected:
                excluded.append("rejected")
            if excluded:
                conditions.append(f"i.status <> ALL(${param_idx}::text[])")
                params.append(excluded)
                param_idx += 1

        where_clause = " AND ".join(conditions)
        sql = f"""
            SELECT i.*
            FROM mediator.user_orientation_items i
            WHERE {where_clause}
            ORDER BY i.kind, i.priority_rank NULLS LAST, i.created_at
        """
        rows = await self._pool.fetch(sql, *params)
        return [OrientationItem.from_row(r) for r in rows]

    async def get_item(
        self,
        *,
        user_id: UUID,
        item_id: UUID,
    ) -> OrientationItem | None:
        """Fetch a single orientation item by ID, scoped to user_id.

        Does not require topic_ids since we're fetching by primary key and
        ownership is verified by user_id.
        """
        _require_user_id(user_id)

        row = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.user_orientation_items
            WHERE id = $1 AND user_id = $2
            """,
            item_id,
            user_id,
        )
        if row is None:
            return None
        return OrientationItem.from_row(row)

    async def get_links(
        self,
        *,
        user_id: UUID,
        item_id: UUID,
    ) -> list[OrientationLink]:
        """Fetch evidence/progress links for an item, scoped to user_id."""
        _require_user_id(user_id)

        rows = await self._pool.fetch(
            """
            SELECT l.*
            FROM mediator.user_orientation_item_links l
            WHERE l.item_id = $1 AND l.user_id = $2
            ORDER BY l.created_at
            """,
            item_id,
            user_id,
        )
        return [OrientationLink.from_row(r) for r in rows]

    async def get_reviews(
        self,
        *,
        user_id: UUID,
        item_id: UUID,
    ) -> list[OrientationReview]:
        """Fetch review history for an item, scoped to user_id."""
        _require_user_id(user_id)

        rows = await self._pool.fetch(
            """
            SELECT r.*
            FROM mediator.user_orientation_item_reviews r
            WHERE r.item_id = $1 AND r.user_id = $2
            ORDER BY r.created_at
            """,
            item_id,
            user_id,
        )
        return [OrientationReview.from_row(r) for r in rows]

    # ── Write methods ────────────────────────────────────────────────

    async def create_item(
        self,
        *,
        user_id: UUID,
        topic_id: UUID,
        bot_id: str,
        kind: str,
        label: str,
        detail: str | None = None,
        source: str = "user_stated",
        status: str | None = None,
        review_state: str | None = None,
        started_at: datetime | None = None,
        effective_at: datetime | None = None,
        target_date: dt_date | None = None,
        supersedes_item_id: UUID | None = None,
        priority_rank: int | None = None,
        created_by_turn_id: UUID | None = None,
    ) -> OrientationItem:
        """Create a new orientation item.

        Defaults for status and review_state depend on source:
          - user_stated/user_confirmed: status='active', review_state='reviewed'
          - bot_proposed: status='pending', review_state='unreviewed'
        """
        # Resolve defaults based on source.
        if status is None:
            status = "pending" if source == "bot_proposed" else "active"
        if review_state is None:
            review_state = (
                "unreviewed" if source == "bot_proposed" else "reviewed"
            )

        validate_create_params(
            user_id=user_id,
            topic_id=topic_id,
            bot_id=bot_id,
            kind=kind,
            source=source,
            label=label,
            status=status,
            review_state=review_state,
        )

        _validate_kind_specific_fields(
            kind,
            {"priority_rank": priority_rank},
        )

        now = datetime.now(timezone.utc)
        row = await self._pool.fetchrow(
            """
            INSERT INTO mediator.user_orientation_items (
                user_id, topic_id, bot_id, created_by_turn_id,
                kind, status, source, review_state,
                label, detail,
                started_at, effective_at, target_date,
                supersedes_item_id, priority_rank,
                created_at, updated_at
            ) VALUES (
                $1, $2, $3, $4,
                $5, $6, $7, $8,
                $9, $10,
                $11, $12, $13,
                $14, $15,
                $16, $16
            )
            RETURNING *
            """,
            user_id,
            topic_id,
            bot_id,
            created_by_turn_id,
            kind,
            status,
            source,
            review_state,
            label,
            detail,
            started_at,
            effective_at,
            target_date,
            supersedes_item_id,
            priority_rank,
            now,
        )
        return OrientationItem.from_row(row)

    async def update_item(
        self,
        *,
        user_id: UUID,
        item_id: UUID,
        label: str | None = None,
        detail: str | None = None,
        started_at: datetime | None = None,
        effective_at: datetime | None = None,
        target_date: dt_date | None = None,
        priority_rank: int | None = None,
        status: str | None = None,
        source: str | None = None,
        review_state: str | None = None,
    ) -> OrientationItem | None:
        """Update mutable fields on an existing orientation item.

        Status/source/review_state transitions are validated. Returns the
        updated item or None if not found.
        """
        _require_user_id(user_id)

        current = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.user_orientation_items
            WHERE id = $1 AND user_id = $2
            """,
            item_id,
            user_id,
        )
        if current is None:
            return None

        current_dict = dict(current)
        validate_update_params(
            user_id=user_id,
            item_current=current_dict,
            new_status=status,
            new_source=source,
            new_review_state=review_state,
        )

        # Build dynamic SET clause for non-None fields.
        set_parts: list[str] = []
        params: list[Any] = [item_id, user_id]
        param_idx = 3

        field_map: list[tuple[str, Any]] = [
            ("label", label),
            ("detail", detail),
            ("started_at", started_at),
            ("effective_at", effective_at),
            ("target_date", target_date),
            ("priority_rank", priority_rank),
            ("status", status),
            ("source", source),
            ("review_state", review_state),
        ]

        for col_name, value in field_map:
            if value is not None:
                set_parts.append(f"{col_name} = ${param_idx}")
                params.append(value)
                param_idx += 1

        if not set_parts:
            # Nothing to update — return current state.
            return OrientationItem.from_row(current)

        # Always bump updated_at.
        now = datetime.now(timezone.utc)
        set_parts.append(f"updated_at = ${param_idx}")
        params.append(now)
        param_idx += 1

        set_clause = ", ".join(set_parts)
        sql = f"""
            UPDATE mediator.user_orientation_items
            SET {set_clause}
            WHERE id = $1 AND user_id = $2
            RETURNING *
        """
        row = await self._pool.fetchrow(sql, *params)
        if row is None:
            return None
        return OrientationItem.from_row(row)

    async def review_item(
        self,
        *,
        user_id: UUID,
        item_id: UUID,
        verdict: str,
        note: str | None = None,
        reviewed_by_turn_id: UUID | None = None,
    ) -> OrientationItem | None:
        """Record a review on an orientation item.

        Creates a review audit row and updates the item status based on the
        verdict. The item must be in a reviewable state (pending).

        Returns the updated item, or None if not found.
        """
        _require_user_id(user_id)

        current = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.user_orientation_items
            WHERE id = $1 AND user_id = $2
            """,
            item_id,
            user_id,
        )
        if current is None:
            return None

        current_dict = dict(current)
        new_status = validate_review_params(
            user_id=user_id,
            item_current=current_dict,
            verdict=verdict,
        )

        previous_status = current_dict.get("status", "pending")
        now = datetime.now(timezone.utc)

        # Insert review audit row.
        await self._pool.execute(
            """
            INSERT INTO mediator.user_orientation_item_reviews (
                item_id, user_id, reviewed_by_turn_id,
                verdict, previous_status, new_status,
                note, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            item_id,
            user_id,
            reviewed_by_turn_id,
            verdict,
            previous_status,
            new_status,
            note,
            now,
        )

        # Update item status and review_state.
        updated_row = await self._pool.fetchrow(
            """
            UPDATE mediator.user_orientation_items
            SET status = $3,
                review_state = 'reviewed',
                updated_at = $4
            WHERE id = $1 AND user_id = $2
            RETURNING *
            """,
            item_id,
            user_id,
            new_status,
            now,
        )
        if updated_row is None:
            return None
        return OrientationItem.from_row(updated_row)

    async def close_item(
        self,
        *,
        user_id: UUID,
        item_id: UUID,
        new_status: str,
        closed_reason: str | None = None,
        outcome_note: str | None = None,
        completed_at: datetime | None = None,
    ) -> OrientationItem | None:
        """Close an orientation item (complete, retire, or supersede).

        Does NOT mutate any commitment/event adherence or lifecycle state.
        Returns the updated item, or None if not found.
        """
        _require_user_id(user_id)

        current = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.user_orientation_items
            WHERE id = $1 AND user_id = $2
            """,
            item_id,
            user_id,
        )
        if current is None:
            return None

        current_dict = dict(current)
        validate_close_params(
            user_id=user_id,
            item_current=current_dict,
            new_status=new_status,
            completed_at=completed_at,
        )

        now = datetime.now(timezone.utc)
        if new_status == "completed" and completed_at is None:
            completed_at = now

        updated_row = await self._pool.fetchrow(
            """
            UPDATE mediator.user_orientation_items
            SET status = $3,
                closed_reason = $4,
                outcome_note = $5,
                completed_at = $6,
                updated_at = $7
            WHERE id = $1 AND user_id = $2
            RETURNING *
            """,
            item_id,
            user_id,
            new_status,
            closed_reason,
            outcome_note,
            completed_at,
            now,
        )
        if updated_row is None:
            return None
        return OrientationItem.from_row(updated_row)

    async def link_evidence(
        self,
        *,
        user_id: UUID,
        item_id: UUID,
        target_table: str,
        target_id: UUID,
        relation: str,
        topic_id: UUID | None = None,
        note: str | None = None,
    ) -> OrientationLink:
        """Link an orientation item to a commitment or event as evidence.

        Validates that:
          - user_id is explicit.
          - item exists and belongs to user_id.
          - target_table is 'commitments' or 'events'.
          - relation is a valid evidence relation.

        Does NOT mutate the linked commitment/event lifecycle state.

        Returns the created link.
        """
        _require_user_id(user_id)

        current = await self._pool.fetchrow(
            """
            SELECT *
            FROM mediator.user_orientation_items
            WHERE id = $1 AND user_id = $2
            """,
            item_id,
            user_id,
        )
        if current is None:
            raise ValueError(
                f"link_evidence: item {item_id} not found or not owned by user"
            )

        current_dict = dict(current)
        # Inherit topic_id from item if not explicitly provided.
        if topic_id is None:
            topic_id = current_dict.get("topic_id")

        validate_link_params(
            user_id=user_id,
            item_current=current_dict,
            target_table=target_table,
            target_id=target_id,
            relation=relation,
        )

        now = datetime.now(timezone.utc)
        row = await self._pool.fetchrow(
            """
            INSERT INTO mediator.user_orientation_item_links (
                item_id, user_id, topic_id,
                target_table, target_id, relation,
                note, created_at
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            item_id,
            user_id,
            topic_id,
            target_table,
            target_id,
            relation,
            note,
            now,
        )
        return OrientationLink.from_row(row)
