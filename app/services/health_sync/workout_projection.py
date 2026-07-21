"""Pure deterministic workout→commitment projection matcher.

This module implements the North Star safety rule: a workout can project
to exactly one compatible explicit Hector fitness commitment.  It is
intentionally free of database I/O and async so it can be tested and
composed without a connection pool.

All non-projecting outcomes return queryable reasons so callers can
log, surface, or act on them without string-matching internal messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping

from app.services.health_sync.models import (
    HECTOR_FITNESS_TAXONOMY_LABELS,
    NormalizedWorkout,
)


# ── Public result types ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ProjectionMatch:
    """A successful workout→commitment match.

    ``commitment_id`` is the string form of the matched commitment UUID.
    ``matched_local_date`` is the workout's local_date that fell on the
    commitment's eligible slot.
    """

    commitment_id: str
    matched_local_date: date


@dataclass(frozen=True, slots=True)
class ProjectionDecision:
    """The result of attempting to match a workout to a commitment.

    ``matched`` is ``None`` for every non-projecting outcome.
    ``reason`` is a stable, queryable string (never a free-form message).
    ``candidates_considered`` is the number of active Hector fitness
    commitments examined before reaching the decision.
    """

    matched: ProjectionMatch | None
    reason: str
    candidates_considered: int


# ── Stable reason constants ─────────────────────────────────────────────────

_REASON_MATCHED = "matched"
_REASON_NO_HECTOR_FITNESS_COMMITMENTS = "no_hector_fitness_commitments"
_REASON_ZERO_ACTIVE_COMMITMENTS = "zero_active_commitments"
_REASON_WRONG_BOT_TOPIC = "wrong_bot_or_topic"
_REASON_UNKNOWN_WORKOUT_TYPE = "unknown_workout_type"
_REASON_NO_LOCAL_DATE = "no_local_date"
_REASON_NO_ELIGIBLE_SLOT = "no_eligible_slot"
_REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS = "ambiguous_multiple_commitments"

# ── Wire constants ──────────────────────────────────────────────────────────

_HECTOR_BOT_ID = "hector"
_FITNESS_TOPIC_SLUG = "fitness"


# ── Slot eligibility helpers ────────────────────────────────────────────────


def _week_boundaries(for_date: date) -> tuple[date, date]:
    """Return (monday, sunday) for the ISO week containing *for_date*."""
    monday = for_date - timedelta(days=for_date.weekday())
    return monday, monday + timedelta(days=6)


def _date_in_range(d: date, start: date, end: date | None) -> bool:
    """True when *d* falls within [start, end]; None end means unbounded."""
    if d < start:
        return False
    if end is not None and d > end:
        return False
    return True


def _is_eligible_slot(
    commitment: Mapping[str, Any],
    local_date: date,
) -> bool:
    """Return True when *local_date* is an expected slot for *commitment*.

    Cadence semantics mirror ``app.services.commitments.compute_slots``
    but only tests membership for a single date — no full slot generation.
    """
    cadence: str = commitment.get("cadence", "custom")
    start_date: date | None = None
    end_date: date | None = None

    sd = commitment.get("start_date")
    ed = commitment.get("end_date")
    if isinstance(sd, str):
        start_date = date.fromisoformat(sd)
    elif isinstance(sd, date):
        start_date = sd
    if isinstance(ed, str):
        end_date = date.fromisoformat(ed)
    elif isinstance(ed, date):
        end_date = ed

    days_of_week: list[int] = commitment.get("days_of_week") or []

    if cadence == "daily":
        if start_date is None:
            return True  # unbounded daily
        return _date_in_range(local_date, start_date, end_date)

    elif cadence == "weekdays":
        monday, sunday = _week_boundaries(local_date)
        effective_start = max(start_date, monday) if start_date else monday
        effective_end = min(end_date, sunday) if end_date else sunday
        if local_date < effective_start or local_date > effective_end:
            return False
        return local_date.weekday() < 5  # Mon–Fri

    elif cadence == "weekly_count":
        # weekly_count has a single summary slot for the current week.
        # A workout is eligible if its local_date falls within the
        # ISO week that contains it AND the commitment is active (within
        # start/end bounds).
        monday, sunday = _week_boundaries(local_date)
        if start_date is not None and sunday < start_date:
            return False
        if end_date is not None and monday > end_date:
            return False
        return True  # any day in the week is eligible

    elif cadence == "custom_days":
        monday, sunday = _week_boundaries(local_date)
        effective_start = max(start_date, monday) if start_date else monday
        effective_end = min(end_date, sunday) if end_date else sunday
        if local_date < effective_start or local_date > effective_end:
            return False
        return local_date.weekday() in days_of_week

    elif cadence == "custom":
        if start_date is None:
            return True
        return _date_in_range(local_date, start_date, end_date)

    # Unknown cadence — be conservative and do not match.
    return False


# ── Public matcher ──────────────────────────────────────────────────────────


def project_workout(
    workout: NormalizedWorkout,
    *,
    commitments: list[dict[str, Any]],
    user_timezone: str | None = None,
    projection_version: int = 1,
) -> ProjectionDecision:
    """Attempt to match *workout* to exactly one active Hector fitness commitment.

    Args:
        workout: The normalized workout to project.
        commitments: Active commitments to match against.  The caller is
            expected to pre-filter to ``status='active'``, but this
            function additionally validates ``bot_id='hector'`` and
            ``topic_slug='fitness'`` for defense-in-depth.
        user_timezone: IANA timezone name for the user.  Not currently
            used by the pure matcher (slot eligibility is date-based),
            but accepted for forward compatibility with timezone-aware
            slot computation in future versions.
        projection_version: The projection version to stamp on the
            decision.  Not currently used by the pure matcher but
            accepted for forward compatibility.

    Returns:
        ``ProjectionDecision`` with ``matched`` set when exactly one
        compatible commitment has an eligible slot on the workout's
        ``local_date``.  Returns a queryable ``reason`` for every
        non-projecting case.
    """
    # Guard: workout must have a local_date.
    local_date = workout.local_date
    if local_date is None:
        return ProjectionDecision(
            matched=None,
            reason=_REASON_NO_LOCAL_DATE,
            candidates_considered=0,
        )

    # Guard: workout type must be in the Hector fitness taxonomy.
    if workout.workout_type not in HECTOR_FITNESS_TAXONOMY_LABELS:
        return ProjectionDecision(
            matched=None,
            reason=_REASON_UNKNOWN_WORKOUT_TYPE,
            candidates_considered=0,
        )

    # No commitments provided.
    if not commitments:
        return ProjectionDecision(
            matched=None,
            reason=_REASON_ZERO_ACTIVE_COMMITMENTS,
            candidates_considered=0,
        )

    # Filter to only Hector fitness commitments.
    hector_fitness: list[dict[str, Any]] = []
    for c in commitments:
        bot_id = c.get("bot_id", "")
        topic_slug = c.get("topic_slug", "")
        if bot_id == _HECTOR_BOT_ID and topic_slug == _FITNESS_TOPIC_SLUG:
            hector_fitness.append(c)

    if not hector_fitness:
        return ProjectionDecision(
            matched=None,
            reason=_REASON_NO_HECTOR_FITNESS_COMMITMENTS,
            candidates_considered=len(commitments),
        )

    # If all provided commitments had wrong bot/topic, note that.
    # (This can happen when the caller passes non-Hector commitments.)
    if len(hector_fitness) < len(commitments):
        # Some were filtered — but we still have Hector candidates.
        # The decision will be based on the filtered set.
        pass

    # Find commitments where the workout's local_date is an eligible slot.
    eligible: list[dict[str, Any]] = []
    for c in hector_fitness:
        if _is_eligible_slot(c, local_date):
            eligible.append(c)

    if len(eligible) == 0:
        return ProjectionDecision(
            matched=None,
            reason=_REASON_NO_ELIGIBLE_SLOT,
            candidates_considered=len(hector_fitness),
        )

    if len(eligible) > 1:
        return ProjectionDecision(
            matched=None,
            reason=_REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS,
            candidates_considered=len(hector_fitness),
        )

    # Exactly one eligible commitment.
    matched_commitment = eligible[0]
    return ProjectionDecision(
        matched=ProjectionMatch(
            commitment_id=str(matched_commitment.get("id", "")),
            matched_local_date=local_date,
        ),
        reason=_REASON_MATCHED,
        candidates_considered=len(hector_fitness),
    )


def reason_is_projecting(reason: str) -> bool:
    """Return True when *reason* indicates a successful projection match."""
    return reason == _REASON_MATCHED


__all__ = [
    "ProjectionDecision",
    "ProjectionMatch",
    "project_workout",
    "reason_is_projecting",
    # Reason constants exported for test assertions.
    "_REASON_AMBIGUOUS_MULTIPLE_COMMITMENTS",
    "_REASON_MATCHED",
    "_REASON_NO_ELIGIBLE_SLOT",
    "_REASON_NO_HECTOR_FITNESS_COMMITMENTS",
    "_REASON_NO_LOCAL_DATE",
    "_REASON_UNKNOWN_WORKOUT_TYPE",
    "_REASON_WRONG_BOT_TOPIC",
    "_REASON_ZERO_ACTIVE_COMMITMENTS",
]
