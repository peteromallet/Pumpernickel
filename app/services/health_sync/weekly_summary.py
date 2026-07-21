"""Pure read-only weekly health digest generator.

Exposes ``generate_weekly_digest()`` — a stateless async function that reads
existing health read models and returns a structured ``WeeklyHealthDigest``.
Nothing calls this automatically.  A caller (tool, CLI, admin helper) must
explicitly invoke it with a user id and pool.  It never sends messages,
schedules jobs, or mutates state.

The digest lives behind ``health_weekly_summary_enabled``, which defaults to
``False``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.services.health_sync.read_models import (
    ConnectionFreshness,
    SleepRollingResult,
    WeeklyWorkoutSummaryResult,
    WeightResult,
    get_connection_freshness,
    get_sleep_rolling_7d,
    get_weekly_workout_summary,
    get_weight,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WeeklyHealthDigest:
    """Combined weekly health picture for one user / connection.

    Every field that carries health values is ``None`` or empty when no
    data exists.  The digest intentionally excludes tokens, device ids,
    external_user_id, raw payloads, cursor state, OAuth timestamps, and
    projection internal ids.  It includes only derived, user-facing
    health summaries already exposed through the health read models.
    """

    connection_active: bool = False
    freshness: ConnectionFreshness | None = None

    weight: WeightResult | None = None
    sleep: SleepRollingResult | None = None
    workouts: WeeklyWorkoutSummaryResult | None = None

    generated_at_utc: datetime | None = None


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


async def generate_weekly_digest(
    *,
    user_id: UUID,
    pool: Any,
) -> WeeklyHealthDigest:
    """Return a combined weekly health picture for *user_id*.

    Reads existing health read models (weight, sleep, workout) and packages
    them into a single ``WeeklyHealthDigest``.  Returns an empty digest when
    the flag is disabled, the user has no active Withings connection, or no
    health data exists.

    This is a pure generator: no writes, no side-effects, no messages.
    """
    settings = get_settings()
    if not settings.health_weekly_summary_enabled:
        return WeeklyHealthDigest()

    # ── Locate the active Withings connection ──────────────────────────
    conn_row = await _find_active_connection(user_id=user_id, pool=pool)
    if conn_row is None:
        return WeeklyHealthDigest()

    connection_id = UUID(str(conn_row["id"]))

    # ── Read all available summaries in parallel ────────────────────────
    freshness = await get_connection_freshness(
        connection_id=connection_id, user_id=user_id, pool=pool
    )

    weight: WeightResult | None = None
    sleep: SleepRollingResult | None = None
    workouts: WeeklyWorkoutSummaryResult | None = None

    try:
        weight = await get_weight(user_id=user_id, pool=pool)
    except Exception:
        logger.debug("weight read failed for digest", exc_info=True)

    try:
        sleep = await get_sleep_rolling_7d(user_id=user_id, pool=pool)
    except Exception:
        logger.debug("sleep read failed for digest", exc_info=True)

    try:
        workouts = await get_weekly_workout_summary(user_id=user_id, pool=pool)
    except Exception:
        logger.debug("workout read failed for digest", exc_info=True)

    # ── Only return a digest when at least one domain has data ──────────
    has_weight = weight is not None and weight.latest is not None
    has_sleep = sleep is not None and sleep.nights_with_data > 0
    has_workouts = workouts is not None and workouts.days_with_workouts > 0

    if not has_weight and not has_sleep and not has_workouts:
        return WeeklyHealthDigest(
            connection_active=True,
            freshness=freshness,
        )

    return WeeklyHealthDigest(
        connection_active=True,
        freshness=freshness,
        weight=weight if has_weight else None,
        sleep=sleep if has_sleep else None,
        workouts=workouts if has_workouts else None,
        generated_at_utc=datetime.utcnow(),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _find_active_connection(
    *,
    user_id: UUID,
    pool: Any,
) -> dict[str, Any] | None:
    """Return the user's active (non-deleted) Withings connection, if any."""
    return await pool.fetchrow(
        """\
        SELECT id
        FROM mediator.health_connections
        WHERE user_id = $1
          AND provider = 'withings'
          AND deleted_at IS NULL
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        user_id,
    )


__all__ = [
    "WeeklyHealthDigest",
    "generate_weekly_digest",
]
