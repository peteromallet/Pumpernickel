"""Plan 5 decay housekeeping.

Per-topic policy (Sprint 3 decision, locked):
  Decay is scoped per-topic, not global.  A coach's career observations
  should not decay because a mediator hasn't reinforced them.  Each bot /
  job decides its own scope, and decay only touches artifact rows whose
  ``artifact_topics`` row matches the current scope's topic_id.  Global
  decay of untagged rows would silently corrupt cross-bot separation and
  is explicitly prohibited.  Multi-topic writes (and therefore multi-topic
  decay) are deferred to Sprint 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.bots.registry import get_relationship_topic_id
from app.services.scoring import RescoreReport, rescore_observations


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class DecayReport:
    themes_dormant: str
    themes_resolved_by_time: str
    observations_confidence_decayed: str
    observations_stale: str
    watch_items_expired: str
    rescore_report: RescoreReport


async def run_decay_housekeeping(
    pool: Any,
    *,
    now: datetime | None = None,
    scoring_client: Any | None = None,
    topic_id: UUID | None = None,
) -> DecayReport:
    """Apply time-based decay and rescore stale observation scores.

    Theme and observation decay are driven by reinforcement age. Observation
    decay never consults theme/message activity timestamps.
    """

    topic = topic_id or get_relationship_topic_id()
    if topic is None:
        raise RuntimeError("run_decay_housekeeping: no topic_id provided and relationship topic not available")
    now = now or _utc_now()
    themes_dormant = await pool.execute(
        """
        UPDATE themes
        SET status = 'dormant',
            updated_at = $1
        FROM artifact_topics at
        WHERE at.artifact_table = 'themes'
          AND at.artifact_id = themes.id
          AND at.topic_id = $2
          AND at.status = 'active'
          AND themes.status = 'active'
          AND COALESCE(themes.last_reinforced_at, themes.first_seen_at) <= $1::timestamptz - interval '6 weeks'
        """,
        now, topic,
    )
    themes_resolved = await pool.execute(
        """
        UPDATE themes
        SET status = 'resolved_by_time',
            updated_at = $1
        FROM artifact_topics at
        WHERE at.artifact_table = 'themes'
          AND at.artifact_id = themes.id
          AND at.topic_id = $2
          AND at.status = 'active'
          AND themes.status = 'dormant'
          AND themes.updated_at <= $1::timestamptz - interval '4 months'
        """,
        now, topic,
    )
    observations_stale = await pool.execute(
        """
        UPDATE observations
        SET status = 'stale'
        FROM artifact_topics at
        WHERE at.artifact_table = 'observations'
          AND at.artifact_id = observations.id
          AND at.topic_id = $2
          AND at.status = 'active'
          AND observations.status = 'active'
          AND COALESCE(observations.last_reinforced_at, observations.created_at) <= $1::timestamptz - interval '6 months'
        """,
        now, topic,
    )
    observations_confidence = await pool.execute(
        """
        UPDATE observations
        SET confidence = CASE observations.confidence
            WHEN 'high' THEN 'medium'
            WHEN 'medium' THEN 'low'
            ELSE observations.confidence
        END
        FROM artifact_topics at
        WHERE at.artifact_table = 'observations'
          AND at.artifact_id = observations.id
          AND at.topic_id = $2
          AND at.status = 'active'
          AND observations.status = 'active'
          AND COALESCE(observations.last_reinforced_at, observations.created_at) <= $1::timestamptz - interval '3 months'
          AND COALESCE(observations.last_reinforced_at, observations.created_at) > $1::timestamptz - interval '6 months'
          AND observations.confidence IN ('high', 'medium')
        """,
        now, topic,
    )
    watch_items_expired = await pool.execute(
        """
        UPDATE watch_items
        SET status = 'expired'
        FROM artifact_topics at
        WHERE at.artifact_table = 'watch_items'
          AND at.artifact_id = watch_items.id
          AND at.topic_id = $2
          AND at.status = 'active'
          AND watch_items.status = 'open'
          AND watch_items.due_at IS NOT NULL
          AND watch_items.addressed_at IS NULL
          AND watch_items.due_at <= $1::timestamptz - interval '30 days'
        """,
        now, topic,
    )
    rescore_report = await rescore_observations(pool, client=scoring_client)
    return DecayReport(
        themes_dormant=themes_dormant,
        themes_resolved_by_time=themes_resolved,
        observations_confidence_decayed=observations_confidence,
        observations_stale=observations_stale,
        watch_items_expired=watch_items_expired,
        rescore_report=rescore_report,
    )
