from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.config import get_settings
from app.services.recovery import recover_scheduled_jobs_on_startup
from app.services.scheduled_task_recurrence import (
    RecurrenceError,
    next_occurrence_utc,
    normalize_recurrence,
    recurrence_after_fire,
)
from app.services.scheduled_job_handlers import (
    ScheduledJobHandlers,
    WEEKLY_REFLECTION_BRIEF,
    next_weekly_reflection_at,
    seed_weekly_reflections,
)
from app.services.scheduled_jobs import ScheduledJobWorker, seed_heartbeat
from app.services import system_state

pytestmark = pytest.mark.anyio


def _scheduler_settings(**overrides):
    values = {
        "scheduler_batch_size": 10,
        "heartbeat_interval_hours": 24,
        "scheduler_poll_interval_s": 1,
        "default_seed_bot_id": "test-bot",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _job(pool, *, job_type: str, user_id=None, scheduled_for=None, status="pending", attempt_count=0, max_attempts=2):
    job_id = uuid4()
    pool.scheduled_jobs[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "job_type": job_type,
        "scheduled_for": scheduled_for or datetime.now(UTC),
        "context": {},
        "status": status,
        "attempt_count": attempt_count,
        "max_attempts": max_attempts,
        "delayed": False,
        "claimed_at": None,
        "claimed_by": None,
        "created_at": datetime.now(UTC),
        "bot_id": "test-bot",
        "topic_id": uuid4(),
    }
    return job_id


def _user(pool, *, timezone="UTC"):
    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": "Maya",
        "phone": "15555550100",
        "timezone": timezone,
        "onboarding_state": "welcomed",
    }
    return user_id


def test_scheduled_task_recurrence_daily_weekly_and_end_conditions():
    current = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)

    assert normalize_recurrence(None) is None
    assert normalize_recurrence({"type": "one_shot"}) is None
    assert next_occurrence_utc(current, {"type": "daily", "interval": 2}) == datetime(
        2026,
        5,
        7,
        9,
        30,
        tzinfo=UTC,
    )
    assert next_occurrence_utc(current, {"type": "hourly", "interval": 3}) == datetime(
        2026,
        5,
        5,
        12,
        30,
        tzinfo=UTC,
    )
    assert next_occurrence_utc(
        current,
        {"type": "weekly", "weekdays": [1, 4], "interval": 1},
    ) == datetime(2026, 5, 8, 9, 30, tzinfo=UTC)
    assert next_occurrence_utc(current, {"type": "daily", "cancelled": True}) is None
    assert next_occurrence_utc(current, {"type": "daily", "remaining_occurrences": 1}) is None
    assert next_occurrence_utc(
        current,
        {"type": "daily", "until": "2026-05-06T09:29:00+00:00"},
    ) is None
    assert recurrence_after_fire({"type": "daily", "remaining_occurrences": 3}) == {
        "version": 1,
        "type": "daily",
        "interval": 1,
        "remaining_occurrences": 2,
    }


def test_scheduled_task_recurrence_rejects_naive_datetimes():
    with pytest.raises(RecurrenceError, match="current_occurrence must include a timezone"):
        next_occurrence_utc(datetime(2026, 5, 5, 9, 30), {"type": "daily"})
    with pytest.raises(RecurrenceError, match="recurrence.until must include a timezone"):
        normalize_recurrence({"type": "daily", "until": "2026-05-06T09:30:00"})


async def test_worker_claims_concurrently_without_double_firing(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now)
    fired = []

    async def handler(job):
        fired.append(job["id"])

    settings = _scheduler_settings()
    worker_a = ScheduledJobWorker(fake_pool, handlers={"checkin": handler}, settings=settings, worker_id="a")
    worker_b = ScheduledJobWorker(fake_pool, handlers={"checkin": handler}, settings=settings, worker_id="b")

    await asyncio.gather(worker_a.run_due_once(now=now), worker_b.run_due_once(now=now))

    assert fired == [job_id]
    assert fake_pool.scheduled_jobs[job_id]["status"] == "fired"


async def test_worker_retries_once_then_cancels_failed_job(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now)

    async def failing(_job):
        raise RuntimeError("boom")

    settings = _scheduler_settings()
    worker = ScheduledJobWorker(fake_pool, handlers={"checkin": failing}, settings=settings, worker_id="worker")

    first = await worker.run_due_once(now=now)
    assert first.retried == 1
    assert fake_pool.scheduled_jobs[job_id]["status"] == "pending"
    assert fake_pool.scheduled_jobs[job_id]["attempt_count"] == 1

    second = await worker.run_due_once(now=now + timedelta(minutes=11))
    assert second.cancelled == 1
    assert fake_pool.scheduled_jobs[job_id]["status"] == "cancelled"
    assert fake_pool.scheduled_jobs[job_id]["cancellation_reason"] == "handler error after retry"


async def test_worker_withholds_user_job_without_stored_scope_identity(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now)
    fake_pool.scheduled_jobs[job_id]["topic_id"] = None
    fired = []

    async def handler(job):
        fired.append(job["id"])

    worker = ScheduledJobWorker(
        fake_pool,
        handlers={"checkin": handler},
        settings=_scheduler_settings(),
        worker_id="missing-scope",
    )

    result = await worker.run_due_once(now=now)

    assert result.claimed == 1
    assert result.fired == 0
    assert fired == []
    assert fake_pool.scheduled_jobs[job_id]["status"] == "withheld"


async def test_worker_withholds_user_job_without_bot_identity(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    fake_pool.scheduled_jobs[job_id]["bot_id"] = None
    fired = []

    async def handler(job):
        fired.append(job["id"])

    worker = ScheduledJobWorker(
        fake_pool,
        handlers={"scheduled_task": handler},
        settings=_scheduler_settings(),
        worker_id="missing-bot-scope",
    )

    result = await worker.run_due_once(now=now)

    assert result.claimed == 1
    assert result.fired == 0
    assert fired == []
    assert fake_pool.scheduled_jobs[job_id]["status"] == "withheld"


async def test_scheduled_task_worker_dispatches_due_payload_and_marks_one_shot_fired(fake_pool, monkeypatch):
    now = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)
    user_id = _user(fake_pool)
    task_id = uuid4()
    job_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    fake_pool.scheduled_jobs[job_id]["context"] = {
        "task_id": str(task_id),
        "brief": "Prepare the due task.",
        "recurrence": None,
    }
    dispatched = []

    async def fake_run_agentic_job(user, metadata, *, scope):
        dispatched.append((user.id, metadata, scope))

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_run_agentic_job)
    settings = _scheduler_settings()
    worker = ScheduledJobWorker(
        fake_pool,
        handlers=ScheduledJobHandlers(fake_pool).as_dict(),
        settings=settings,
        worker_id="scheduled-task-worker",
    )

    result = await worker.run_due_once(now=now)

    assert result.claimed == 1
    assert result.fired == 1
    assert fake_pool.scheduled_jobs[job_id]["status"] == "fired"
    assert len(dispatched) == 1
    assert dispatched[0][:2] == (
        user_id,
        {
            "kind": "scheduled_task",
            "context": {
                "task_id": str(task_id),
                "brief": "Prepare the due task.",
                "recurrence": None,
                "job_id": str(job_id),
                "scheduled_for": now.isoformat(),
                "delayed": False,
            },
        },
    )
    assert dispatched[0][2].bot_id == fake_pool.scheduled_jobs[job_id]["bot_id"]
    assert dispatched[0][2].topic_id == fake_pool.scheduled_jobs[job_id]["topic_id"]
    assert not [
        job
        for job in fake_pool.scheduled_jobs.values()
        if job["job_type"] == "scheduled_task" and job.get("context", {}).get("source_job_id") == str(job_id)
    ]


async def test_scheduled_task_worker_failure_retries_without_reseed(fake_pool, monkeypatch):
    now = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    fake_pool.scheduled_jobs[job_id]["context"] = {
        "task_id": str(uuid4()),
        "brief": "This task fails before reseed.",
        "recurrence": {"type": "daily", "interval": 1},
    }

    async def fake_failure(_user, _metadata, *, scope):
        assert scope.bot_id == fake_pool.scheduled_jobs[job_id]["bot_id"]
        raise RuntimeError("agent failed")

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_failure)
    settings = _scheduler_settings()
    worker = ScheduledJobWorker(
        fake_pool,
        handlers=ScheduledJobHandlers(fake_pool).as_dict(),
        settings=settings,
        worker_id="scheduled-task-worker",
    )

    result = await worker.run_due_once(now=now)

    assert result.retried == 1
    assert fake_pool.scheduled_jobs[job_id]["status"] == "pending"
    assert fake_pool.scheduled_jobs[job_id]["attempt_count"] == 1
    assert not [
        job
        for job in fake_pool.scheduled_jobs.values()
        if job["job_type"] == "scheduled_task" and job.get("context", {}).get("source_job_id") == str(job_id)
    ]


async def test_pause_claims_only_heartbeat_and_preserves_user_jobs(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    checkin_id = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now)
    scheduled_task_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    heartbeat_id = _job(fake_pool, job_type="heartbeat", user_id=None, scheduled_for=now)
    fired = []

    async def checkin_handler(job):
        fired.append(("checkin", job["id"]))

    settings = _scheduler_settings()
    worker = ScheduledJobWorker(fake_pool, handlers={"checkin": checkin_handler}, settings=settings, worker_id="paused")
    await system_state.pause(fake_pool, user_id, now=now)

    result = await worker.run_due_once(now=now)

    assert result.skipped_paused is True
    assert fired == []
    assert fake_pool.scheduled_jobs[checkin_id]["status"] == "pending"
    assert fake_pool.scheduled_jobs[scheduled_task_id]["status"] == "pending"
    assert fake_pool.scheduled_jobs[heartbeat_id]["status"] == "fired"


async def test_pause_supersedes_pending_scheduled_tasks(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    scheduled_task_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    heartbeat_id = _job(fake_pool, job_type="heartbeat", user_id=None, scheduled_for=now)

    await system_state.supersede_pending_user_facing_jobs(fake_pool, now=now)

    assert fake_pool.scheduled_jobs[scheduled_task_id]["status"] == "superseded"
    assert fake_pool.scheduled_jobs[scheduled_task_id]["cancellation_reason"] == "global pause"
    assert fake_pool.scheduled_jobs[heartbeat_id]["status"] == "pending"


async def test_late_job_recovery_marks_stale_delayed_and_recent(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    stale = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now - timedelta(hours=25))
    stale_scheduled_task = _job(
        fake_pool,
        job_type="scheduled_task",
        user_id=user_id,
        scheduled_for=now - timedelta(hours=25),
    )
    delayed = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now - timedelta(hours=2))
    recent = _job(fake_pool, job_type="checkin", user_id=user_id, scheduled_for=now - timedelta(minutes=20))
    fake_pool.scheduled_jobs[stale]["claimed_at"] = now - timedelta(hours=1)
    fake_pool.scheduled_jobs[stale_scheduled_task]["claimed_at"] = now - timedelta(hours=1)
    fake_pool.scheduled_jobs[delayed]["claimed_at"] = now - timedelta(hours=1)
    fake_pool.scheduled_jobs[recent]["claimed_at"] = now - timedelta(hours=1)

    await recover_scheduled_jobs_on_startup(fake_pool, now=now)

    assert fake_pool.scheduled_jobs[stale]["status"] == "cancelled"
    assert fake_pool.scheduled_jobs[stale]["cancellation_reason"] == "too stale"
    assert fake_pool.scheduled_jobs[stale_scheduled_task]["status"] == "cancelled"
    assert fake_pool.scheduled_jobs[stale_scheduled_task]["cancellation_reason"] == "too stale"
    assert fake_pool.scheduled_jobs[delayed]["delayed"] is True
    assert fake_pool.scheduled_jobs[delayed]["claimed_at"] is None
    assert fake_pool.scheduled_jobs[recent]["status"] == "pending"
    assert fake_pool.scheduled_jobs[recent]["claimed_at"] is None


async def test_scheduled_task_recovery_delayed_state_reaches_trigger_metadata(fake_pool, monkeypatch):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    user_id = _user(fake_pool)
    task_id = uuid4()
    delayed_job_id = _job(
        fake_pool,
        job_type="scheduled_task",
        user_id=user_id,
        scheduled_for=now - timedelta(hours=2),
    )
    fake_pool.scheduled_jobs[delayed_job_id]["context"] = {
        "task_id": str(task_id),
        "brief": "Run the delayed task.",
        "recurrence": None,
    }
    seen = []

    async def fake_run_agentic_job(_user, metadata, *, scope):
        seen.append((metadata, scope))

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_run_agentic_job)

    await recover_scheduled_jobs_on_startup(fake_pool, now=now)
    await ScheduledJobHandlers(fake_pool).handle_scheduled_task(fake_pool.scheduled_jobs[delayed_job_id])

    assert fake_pool.scheduled_jobs[delayed_job_id]["delayed"] is True
    assert fake_pool.scheduled_jobs[delayed_job_id]["context"]["delayed"] is True
    assert seen[0][0]["kind"] == "scheduled_task"
    assert seen[0][0]["context"]["job_id"] == str(delayed_job_id)
    assert seen[0][0]["context"]["task_id"] == str(task_id)
    assert seen[0][0]["context"]["delayed"] is True
    assert seen[0][1].bot_id == fake_pool.scheduled_jobs[delayed_job_id]["bot_id"]


async def test_checkin_always_runs_agentic_job(fake_pool, monkeypatch):
    user_id = _user(fake_pool)
    job_id = _job(fake_pool, job_type="checkin", user_id=user_id)
    ran = []

    async def fake_run_agentic_job(user, metadata, *, scope):
        ran.append((user.id, metadata, scope.bot_id, scope.topic_id))

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_run_agentic_job)

    await ScheduledJobHandlers(fake_pool).handle_checkin(fake_pool.scheduled_jobs[job_id])

    assert ran == [
        (
            user_id,
            {"kind": "checkin", "context": {"delayed": False}},
            fake_pool.scheduled_jobs[job_id]["bot_id"],
            fake_pool.scheduled_jobs[job_id]["topic_id"],
        )
    ]


async def test_scheduled_task_handler_dispatches_agentic_job_without_reseed_for_one_shot(fake_pool, monkeypatch):
    now = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)
    user_id = _user(fake_pool)
    task_id = uuid4()
    job_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    fake_pool.scheduled_jobs[job_id]["context"] = {
        "task_id": str(task_id),
        "brief": "Prepare the repair brief.",
        "recurrence": None,
    }
    ran = []

    async def fake_run_agentic_job(user, metadata, *, scope):
        ran.append((user.id, metadata, scope))

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_run_agentic_job)

    await ScheduledJobHandlers(fake_pool).handle_scheduled_task(fake_pool.scheduled_jobs[job_id])

    assert ran == [
        (
            user_id,
            {
                "kind": "scheduled_task",
                "context": {
                    "task_id": str(task_id),
                    "brief": "Prepare the repair brief.",
                    "recurrence": None,
                    "job_id": str(job_id),
                    "scheduled_for": now.isoformat(),
                    "delayed": False,
                },
            },
            fake_pool.scheduled_jobs[job_id]["_scope"],
        )
    ]
    assert [
        job
        for job in fake_pool.scheduled_jobs.values()
        if job["job_type"] == "scheduled_task" and job["status"] == "pending"
    ] == [fake_pool.scheduled_jobs[job_id]]


async def test_scheduled_task_handler_reseeds_recurring_success_idempotently(fake_pool, monkeypatch):
    now = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)
    user_id = _user(fake_pool)
    task_id = uuid4()
    job_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    fake_pool.scheduled_jobs[job_id]["context"] = {
        "task_id": str(task_id),
        "brief": "Send a daily repair brief.",
        "recurrence": {"type": "daily", "interval": 1, "remaining_occurrences": 3},
    }
    ran = []

    async def fake_run_agentic_job(user, metadata, *, scope):
        ran.append((user.id, metadata["context"]["job_id"], scope.bot_id))

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_run_agentic_job)
    handlers = ScheduledJobHandlers(fake_pool)

    await handlers.handle_scheduled_task(fake_pool.scheduled_jobs[job_id])
    await handlers.handle_scheduled_task(fake_pool.scheduled_jobs[job_id])

    next_rows = [
        job
        for job in fake_pool.scheduled_jobs.values()
        if job["job_type"] == "scheduled_task"
        and job["status"] == "pending"
        and job.get("context", {}).get("source_job_id") == str(job_id)
    ]
    assert ran == [(user_id, str(job_id), fake_pool.scheduled_jobs[job_id]["bot_id"]), (user_id, str(job_id), fake_pool.scheduled_jobs[job_id]["bot_id"])]
    assert len(next_rows) == 1
    assert next_rows[0]["scheduled_for"] == datetime(2026, 5, 6, 9, 30, tzinfo=UTC)
    assert next_rows[0]["context"]["task_id"] == str(task_id)
    assert next_rows[0]["context"]["brief"] == "Send a daily repair brief."
    assert next_rows[0]["context"]["recurrence"] == {
        "version": 1,
        "type": "daily",
        "interval": 1,
        "remaining_occurrences": 2,
    }


async def test_scheduled_task_handler_honors_current_task_cancel_and_failure_no_reseed(fake_pool, monkeypatch):
    now = datetime(2026, 5, 5, 9, 30, tzinfo=UTC)
    user_id = _user(fake_pool)
    cancelled_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    failed_id = _job(fake_pool, job_type="scheduled_task", user_id=user_id, scheduled_for=now)
    for job_id, brief in ((cancelled_id, "cancel this task"), (failed_id, "fail this task")):
        fake_pool.scheduled_jobs[job_id]["context"] = {
            "task_id": str(uuid4()),
            "brief": brief,
            "recurrence": {"type": "daily", "interval": 1},
        }

    async def fake_cancel_after_current(_user, metadata, *, scope):
        assert scope.bot_id == fake_pool.scheduled_jobs[cancelled_id]["bot_id"]
        job_id = uuid4()
        for candidate_id, job in fake_pool.scheduled_jobs.items():
            if str(candidate_id) == metadata["context"]["job_id"]:
                job_id = candidate_id
                break
        fake_pool.scheduled_jobs[job_id].setdefault("context", {})["scheduled_task_control"] = {
            "cancel_after_current_fire": True
        }

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_cancel_after_current)
    await ScheduledJobHandlers(fake_pool).handle_scheduled_task(fake_pool.scheduled_jobs[cancelled_id])

    async def fake_failure(_user, _metadata, *, scope):
        assert scope.bot_id == fake_pool.scheduled_jobs[failed_id]["bot_id"]
        raise RuntimeError("agent failed")

    monkeypatch.setattr("app.services.scheduled_job_handlers.run_agentic_job", fake_failure)
    with pytest.raises(RuntimeError, match="agent failed"):
        await ScheduledJobHandlers(fake_pool).handle_scheduled_task(fake_pool.scheduled_jobs[failed_id])

    assert not [
        job
        for job in fake_pool.scheduled_jobs.values()
        if job["job_type"] == "scheduled_task" and job.get("context", {}).get("source_job_id") in {str(cancelled_id), str(failed_id)}
    ]


async def test_seed_weekly_reflections_targets_sunday_and_is_idempotent(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)  # Thursday
    user_id = _user(fake_pool, timezone="Europe/Berlin")

    expected = next_weekly_reflection_at("Europe/Berlin", now=now)
    await seed_weekly_reflections(fake_pool, now=now)
    await seed_weekly_reflections(fake_pool, now=now)

    weekly = [
        job
        for job in fake_pool.scheduled_jobs.values()
        if job["job_type"] == "scheduled_task"
        and job["context"].get("kind") == "weekly_reflection"
    ]
    assert len(weekly) == 1
    assert weekly[0]["user_id"] == user_id
    assert weekly[0]["scheduled_for"] == expected
    assert weekly[0]["context"]["brief"] == WEEKLY_REFLECTION_BRIEF
    assert weekly[0]["context"]["recurrence"] == {
        "version": 1,
        "type": "weekly",
        "interval": 1,
        "weekdays": [6],
    }

    seed_settings = _scheduler_settings()
    await seed_heartbeat(fake_pool, settings=seed_settings, now=now)
    await seed_heartbeat(fake_pool, settings=seed_settings, now=now)
    assert len([job for job in fake_pool.scheduled_jobs.values() if job["job_type"] == "heartbeat"]) == 1


async def test_heartbeat_purges_expired_deletions(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": uuid4(),
        "recipient_id": None,
        "content": "delete me",
        "processing_state": "processed",
        "sent_at": now - timedelta(days=2),
        "charge": "routine",
        "whatsapp_message_id": "wa-delete",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": now - timedelta(hours=25),
    }

    await ScheduledJobHandlers(fake_pool).handle_heartbeat({"id": uuid4(), "scheduled_for": now})

    assert fake_pool.messages[message_id]["content"] == "[deleted]"
