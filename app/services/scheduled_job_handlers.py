"""Concrete scheduled job handlers for Plan 5."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import Settings, get_settings
from app.models.user import fetch_user_by_id
from app.services.agentic import run_agentic_job, run_agentic_turn
from app.services.checkins import schedule_checkin_record
from app.services.deletion import purge_expired_deletions
from app.services.scheduled_task_recurrence import next_occurrence_utc, recurrence_after_fire
from app.bots.registry import get_relationship_topic_id
from app.services.scope import InboundScope, scope_from_job_row
from app.services.topic_filter import join_artifact_topics

logger = logging.getLogger(__name__)


WEEKLY_REFLECTION_BRIEF = (
    "It's the weekly reflection touchpoint. Look across recent conversation, "
    "open themes, and watch items. If something is unresolved or you've noticed "
    "a pattern worth naming, gently surface it. If they've been quiet, reach out "
    "with something specific you noticed — not a generic 'how are you'. If there "
    "is truly nothing to surface and they are mid-flow in real life, you can stay "
    "silent this week. Do not recap statistics. If a later moment today would land "
    "better, you can schedule_task for later in the day with this same brief and "
    "skip sending now; the weekly recurrence will keep firing on Sundays."
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _zoneinfo(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        # obs N/A: startup/config tz lookup
        logger.warning("unknown user timezone %s; falling back to UTC", name)
        return ZoneInfo("UTC")


def next_weekly_reflection_at(timezone: str | None, *, now: datetime | None = None) -> datetime:
    """Next Sunday 09:00 in the user's local timezone, expressed in UTC."""
    now = now or _utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    zone = _zoneinfo(timezone or "UTC")
    local_now = now.astimezone(zone)
    days_ahead = (6 - local_now.weekday()) % 7
    candidate = datetime.combine(local_now.date() + timedelta(days=days_ahead), time(9, 0), zone)
    if candidate <= local_now:
        candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


class ScheduledJobHandlers:
    def __init__(self, pool: Any, *, settings: Settings | None = None) -> None:
        self.pool = pool
        self.settings = settings or get_settings()

    def as_dict(self) -> dict[str, Any]:
        return {
            "checkin": self.handle_checkin,
            "watch_item_due": self.handle_watch_item_due,
            "oob_review": self.handle_oob_review,
            "heartbeat": self.handle_heartbeat,
            "deferred_turn": self.handle_deferred_turn,
            "scheduled_task": self.handle_scheduled_task,
        }

    async def _user_and_scope_for_job(self, job: dict[str, Any]) -> tuple[Any | None, InboundScope | None]:
        try:
            scope = job.get("_scope") or scope_from_job_row(job)
        except ValueError as exc:
            logger.warning("skipping scheduled job %s with missing scope identity: %s", job.get("id"), exc)
            return None, None
        job["_scope"] = scope
        user = await fetch_user_by_id(self.pool, scope.user_id)
        return user, scope

    async def handle_checkin(self, job: dict[str, Any]) -> None:
        user, scope = await self._user_and_scope_for_job(job)
        if user is None or scope is None:
            return
        context = job.get("context") or {}
        metadata = {"kind": "checkin", "context": {**context, "delayed": bool(job.get("delayed"))}}
        await run_agentic_job(user, metadata, scope=scope)

    async def handle_watch_item_due(self, job: dict[str, Any]) -> None:
        user, scope = await self._user_and_scope_for_job(job)
        if user is None or scope is None:
            return
        context = job.get("context") or {}
        watch_item = await self._fetch_watch_item(context.get("watch_item_id"), topic_id=scope.topic_id)
        metadata = {
            "kind": "watch_item_due",
            "context": {
                **context,
                "watch_item": watch_item,
                "delayed": bool(job.get("delayed")),
            },
        }
        await run_agentic_job(user, metadata, scope=scope)

    async def handle_oob_review(self, job: dict[str, Any]) -> None:
        user, scope = await self._user_and_scope_for_job(job)
        if user is None or scope is None:
            return
        context = job.get("context") or {}
        await run_agentic_job(
            user,
            {
                "kind": "oob_review",
                "context": {**context, "delayed": bool(job.get("delayed"))},
            },
            scope=scope,
        )

    async def handle_heartbeat(self, job: dict[str, Any]) -> None:
        logger.info("scheduled heartbeat fired job_id=%s scheduled_for=%s", job["id"], job["scheduled_for"],
                     extra={"bot_id": job.get("bot_id"), "topic_id": job.get("topic_id")})
        await purge_expired_deletions(self.pool)

    async def handle_deferred_turn(self, job: dict[str, Any]) -> None:
        user, scope = await self._user_and_scope_for_job(job)
        if user is None or scope is None:
            return
        context = job.get("context") or {}
        message_ids = [UUID(value) for value in context.get("triggering_message_ids", [])]
        if message_ids:
            # Let _run_agentic handle claiming atomically via its pre-LLM
            # claim gate.  If the messages were already handled since the job
            # was scheduled, the claim gate will return zero claimed and
            # abort before any turn/LLM work.
            await run_agentic_turn(message_ids, user, scope=scope)

    async def handle_scheduled_task(self, job: dict[str, Any]) -> None:
        user, scope = await self._user_and_scope_for_job(job)
        if user is None or scope is None:
            return
        context = dict(job.get("context") or {})
        await run_agentic_job(
            user,
            {
                "kind": "scheduled_task",
                "context": _scheduled_task_trigger_context(job, context),
            },
            scope=scope,
        )

        current = await self.pool.fetchrow(
            """
            SELECT id, user_id, scheduled_for, context, status
            FROM scheduled_jobs
            WHERE id = $1
            """,
            job["id"],
        )
        if current is None:
            return
        current_context = dict(current.get("context") or {})
        control = current_context.get("scheduled_task_control") or {}
        if current.get("status") != "pending" or control.get("cancel_after_current_fire"):
            return

        recurrence = current_context.get("recurrence")
        next_scheduled_for = next_occurrence_utc(current["scheduled_for"], recurrence)
        next_recurrence = recurrence_after_fire(recurrence)
        if next_scheduled_for is None or next_recurrence is None:
            return

        next_context = {
            **current_context,
            "recurrence": next_recurrence,
            "source_job_id": str(job["id"]),
        }
        next_context.pop("scheduled_task_control", None)
        await self.pool.fetchrow(
            """
            INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
            SELECT $1, 'scheduled_task', $2, $3::jsonb, 'pending', $4, $5
            WHERE NOT EXISTS (
                SELECT 1
                FROM scheduled_jobs
                WHERE job_type = 'scheduled_task'
                  AND status = 'pending'
                  AND context->>'source_job_id' = $6
            )
            RETURNING id, scheduled_for
            """,
            current["user_id"],
            next_scheduled_for,
            next_context,
            scope.bot_id,
            scope.topic_id,
            str(job["id"]),
        )

    async def _fetch_watch_item(self, watch_item_id: Any, *, topic_id: UUID) -> dict[str, Any] | None:
        if watch_item_id is None:
            return None
        row = await self.pool.fetchrow(
            f"""
            SELECT w.id, w.owner_user_id, w.content, w.due_at, w.status
            FROM watch_items w
            {join_artifact_topics('w', '$2')}
            WHERE w.id = $1
            """,
            watch_item_id, topic_id,
        )
        return dict(row) if row is not None else None


def _scheduled_task_trigger_context(job: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {
        **context,
        "job_id": str(job["id"]),
        "task_id": context.get("task_id"),
        "brief": context.get("brief"),
        "scheduled_for": job["scheduled_for"].isoformat()
        if isinstance(job.get("scheduled_for"), datetime)
        else job.get("scheduled_for"),
        "recurrence": context.get("recurrence"),
        "delayed": bool(job.get("delayed")),
    }


async def seed_weekly_reflection_for_user(
    pool: Any,
    user_id: UUID,
    *,
    timezone: str | None = None,
    now: datetime | None = None,
    bot_id: str,
    topic_id: UUID | None = None,
) -> Any | None:
    """Insert a Sunday weekly_reflection scheduled_task for this user if none is pending."""
    if topic_id is None:
        topic_id = get_relationship_topic_id()
    if timezone is None:
        timezone = await pool.fetchval("SELECT timezone FROM users WHERE id=$1", user_id)
    existing = await pool.fetchval(
        """
        SELECT 1 FROM scheduled_jobs
        WHERE user_id = $1
          AND job_type = 'scheduled_task'
          AND status = 'pending'
          AND context->>'kind' = 'weekly_reflection'
        LIMIT 1
        """,
        user_id,
    )
    if existing:
        return None
    scheduled_for = next_weekly_reflection_at(timezone, now=now)
    context = {
        "kind": "weekly_reflection",
        "task_id": str(uuid4()),
        "brief": WEEKLY_REFLECTION_BRIEF,
        "recurrence": {
            "version": 1,
            "type": "weekly",
            "interval": 1,
            "weekdays": [6],  # Sunday in Python weekday() convention
        },
    }
    return await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
        VALUES ($1, 'scheduled_task', $2, $3::jsonb, 'pending', $4, $5)
        RETURNING id, scheduled_for
        """,
        user_id,
        scheduled_for,
        context,
        bot_id,
        topic_id,
    )


async def seed_weekly_reflections(pool: Any, *, now: datetime | None = None) -> list[Any]:
    settings = get_settings()
    rows = await pool.fetch("SELECT id, timezone FROM users")
    inserted = []
    for row in rows:
        result = await seed_weekly_reflection_for_user(
            pool, row["id"], timezone=row["timezone"], now=now, bot_id=settings.default_seed_bot_id
        )
        if result is not None:
            inserted.append(result)
    return inserted


async def schedule_checkin_job(
    pool: Any,
    user_id: UUID,
    *,
    scheduled_for: datetime,
    context: dict[str, Any],
    bot_id: str,
    topic_id: UUID | None = None,
) -> Any:
    _old, row = await schedule_checkin_record(
        pool,
        user_id,
        scheduled_for=scheduled_for,
        context=context,
        bot_id=bot_id,
        topic_id=topic_id,
    )
    return row
