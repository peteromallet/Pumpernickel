"""Projection applicator for workout→commitment completion events.

This module bridges the pure matcher (workout_projection.py) with the
repository ledger primitives.  It is the only code path that creates
projection-owned events — every other code path treats manual log_event
testimony as immutable.

The applicator enforces five invariants:

1. **Disabled no-op**: When the feature flag is off this function is a
   pure no-op with zero side effects.
2. **First-time projection**: Exactly one projection-owned
   ``mediator.events`` row is created (``metric_key='workout'``,
   ``adherence_status='done'``) and linked from a new ledger row.
3. **Retry / concurrent replay**: An existing active ledger row is
   returned immediately — no duplicate event or ledger row is created.
4. **Revision / rematch**: When the caller requests a higher
   ``projection_version`` than the active ledger row, the old
   projection is superseded, its event is detached or deleted, and a
   fresh match is attempted.  A new event is created only when the
   matcher returns exactly one eligible match.
5. **Tombstone**: When ``is_tombstone=True``, any active projection is
   removed and its projection-owned event is deleted.  Manual
   ``log_event`` testimony is never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.services.health_sync.models import NormalizedWorkout
from app.services.health_sync.repository import HealthProjectionRecord, HealthSyncRepository
from app.services.health_sync.workout_projection import (
    ProjectionDecision,
    project_workout,
    reason_is_projecting,
)

# ── Wire constants ──────────────────────────────────────────────────────────

_MATCH_RULE = "workout_auto_projection"
_EVENT_NOTE = "Auto-projected from Withings workout"
_TOMBSTONE_NOTE = "Projection removed — source workout deleted"


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _commitment_id_from_existing(existing: HealthProjectionRecord) -> str | None:
    """Return the string form of *existing*'s commitment_id, or None."""
    if existing.commitment_id is None:
        return None
    return str(existing.commitment_id)


async def _create_projection(
    *,
    repository: HealthSyncRepository,
    workout: NormalizedWorkout,
    source_record_id: UUID,
    connection_id: UUID,
    user_id: UUID,
    commitments: list[dict[str, Any]],
    user_timezone: str | None,
    projection_version: int,
    supersedes_projection_id: UUID | None,
    executor: Any | None,
) -> HealthProjectionRecord | None:
    """Run the matcher and, when successful, create event + ledger row.

    Returns the new projection record or None when no match is found.
    """
    decision: ProjectionDecision = project_workout(
        workout,
        commitments=commitments,
        user_timezone=user_timezone,
        projection_version=projection_version,
    )

    if not reason_is_projecting(decision.reason):
        return None

    assert decision.matched is not None  # narrow for type checkers

    # Locate the matched commitment for topic_id.
    matched_cid = decision.matched.commitment_id
    matched_commitment: dict[str, Any] | None = None
    for c in commitments:
        if str(c.get("id", "")) == matched_cid:
            matched_commitment = c
            break

    if matched_commitment is None:
        return None  # rare race

    topic_id_raw = matched_commitment.get("topic_id")
    if topic_id_raw is None:
        return None

    topic_id = UUID(str(topic_id_raw)) if not isinstance(topic_id_raw, UUID) else topic_id_raw
    commitment_id = UUID(matched_cid)

    event = await repository.create_projection_event(
        commitment_id=commitment_id,
        user_id=user_id,
        topic_id=topic_id,
        bot_id="hector",
        metric_key="workout",
        adherence_status="done",
        observed_at=workout.started_at,
        note=_EVENT_NOTE,
        executor=executor,
    )

    event_id = event["id"]

    projection = await repository.insert_projection(
        source_record_id=source_record_id,
        connection_id=connection_id,
        user_id=user_id,
        event_id=event_id,
        commitment_id=commitment_id,
        projection_version=projection_version,
        projection_status="projected",
        match_rule=_MATCH_RULE,
        note=_EVENT_NOTE,
        decision_reason=decision.reason,
        matched_local_date=decision.matched.matched_local_date,
        supersedes_projection_id=supersedes_projection_id,
        projected_at=_utc_now(),
        executor=executor,
    )

    return projection


async def _cleanup_projection_event(
    *,
    repository: HealthSyncRepository,
    projection: HealthProjectionRecord,
    user_id: UUID,
    executor: Any | None,
) -> None:
    """Delete the projection-owned event after verifying ownership.

    Manual events (not linked by any projection row) are never touched:
    ``find_projection_by_event`` returns None for them, and we skip the
    delete entirely.
    """
    if projection.event_id is None:
        return

    owned = await repository.find_projection_by_event(
        event_id=projection.event_id,
        executor=executor,
    )
    if owned is None:
        # Not projection-owned — leave it alone.
        return

    await repository.delete_projection_event(
        event_id=projection.event_id,
        user_id=user_id,
        executor=executor,
    )


async def apply_workout_projection(
    *,
    repository: HealthSyncRepository,
    workout: NormalizedWorkout,
    source_record_id: UUID,
    connection_id: UUID,
    user_id: UUID,
    commitments: list[dict[str, Any]],
    user_timezone: str | None = None,
    projection_version: int = 1,
    enabled: bool = False,
    executor: Any | None = None,
    is_tombstone: bool = False,
) -> HealthProjectionRecord | None:
    """Apply the workout projection, creating an event when matched.

    Args:
        repository: Repository with ledger primitives.
        workout: The normalized workout to project.
        source_record_id: ``health_source_records.id`` for this workout.
        connection_id: ``health_connections.id``.
        user_id: The owning user.
        commitments: Active commitments to match against.  The caller
            should pre-filter to ``status='active'``.
        user_timezone: IANA timezone for the user (forwarded to the
            pure matcher for future slot-computation compatibility).
        projection_version: Version to stamp on new ledger rows.  When
            greater than the existing active projection's version the
            applicator performs a revision/rematch cycle.
        enabled: Feature flag.  When ``False`` the function is a no-op.
        executor: Transaction connection (required inside a
            ``repository.transaction()`` block).
        is_tombstone: When ``True`` the source workout has been deleted
            and any active projection must be removed with its event
            cleaned up.

    Returns:
        The active projection record, or ``None`` when the feature is
        off, no match is found, the workout cannot be projected, or
        a tombstone clean-up was performed.
    """
    # ── 1. Disabled no-op ──────────────────────────────────────────────
    if not enabled:
        return None

    # ── 2. Lock the active projection row if it exists ─────────────────
    existing = await repository.find_active_projection(
        source_record_id=source_record_id,
        user_id=user_id,
        for_update=True,
        executor=executor,
    )

    # ── 3. Tombstone path ──────────────────────────────────────────────
    if is_tombstone:
        if existing is not None:
            await _cleanup_projection_event(
                repository=repository,
                projection=existing,
                user_id=user_id,
                executor=executor,
            )
            await repository.remove_projection(
                projection_id=existing.projection_id,
                user_id=user_id,
                now=_utc_now(),
                executor=executor,
            )
        return None

    # ── 4. Idempotent replay: same version, no change ──────────────────
    if existing is not None and existing.projection_version == projection_version:
        return existing

    # ── 5. Revision / rematch path ─────────────────────────────────────
    if existing is not None:
        # Supersede the old projection first.
        await repository.supersede_projection(
            existing_projection_id=existing.projection_id,
            user_id=user_id,
            now=_utc_now(),
            executor=executor,
        )
        await _cleanup_projection_event(
            repository=repository,
            projection=existing,
            user_id=user_id,
            executor=executor,
        )
        # Detach the event from the superseded row so future lookups
        # don't see a stale link.
        if existing.event_id is not None:
            await repository.detach_projection_event(
                projection_id=existing.projection_id,
                user_id=user_id,
                now=_utc_now(),
                executor=executor,
            )

        # Try to create a new projection for the revised workout.
        return await _create_projection(
            repository=repository,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=commitments,
            user_timezone=user_timezone,
            projection_version=projection_version,
            supersedes_projection_id=existing.projection_id,
            executor=executor,
        )

    # ── 6. First-time projection ───────────────────────────────────────
    return await _create_projection(
        repository=repository,
        workout=workout,
        source_record_id=source_record_id,
        connection_id=connection_id,
        user_id=user_id,
        commitments=commitments,
        user_timezone=user_timezone,
        projection_version=projection_version,
        supersedes_projection_id=None,
        executor=executor,
    )


__all__ = [
    "apply_workout_projection",
]
