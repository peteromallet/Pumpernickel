"""SD-014: list_scheduled_checkins returns pending checkin rows scoped to
ctx.user.id AND ctx.bot_id only. A user with both mediator and Tante
Rosi check-ins sees only the current bot's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.tools import read_tools
from app.services.turn_context import TurnContext
from tool_schemas import ListScheduledCheckinsInput

pytestmark = pytest.mark.anyio


def _build_ctx(fake_pool, user, *, bot_id):
    fake_pool.users.setdefault(
        user.id,
        {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
        },
    )
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
    }
    return TurnContext(
        turn_id,
        fake_pool,
        user,
        None,
        [uuid4()],
        current_step="read",
        bot_id=bot_id,
        user_id=user.id,
        primary_topic_id=get_relationship_topic_id(),
    )


def _seed_checkin(pool, *, user_id, bot_id, scheduled_for, about_what=None):
    job_id = uuid4()
    pool.scheduled_jobs[job_id] = {
        "id": job_id,
        "user_id": user_id,
        "job_type": "checkin",
        "scheduled_for": scheduled_for,
        "context": {"about_what": about_what, "reason": "test reason"},
        "status": "pending",
        "bot_id": bot_id,
        "topic_id": get_relationship_topic_id(),
        "created_at": datetime.now(UTC),
    }
    return job_id


async def test_returns_only_current_user_and_bot_pending_checkins(fake_pool):
    user_a = User(uuid4(), "Maya", "15555550100", "UTC")
    user_b = User(uuid4(), "Ben", "15555550101", "UTC")
    mediator_a = _seed_checkin(
        fake_pool,
        user_id=user_a.id,
        bot_id="mediator",
        scheduled_for=datetime.now(UTC) + timedelta(hours=1),
        about_what="mediator-A",
    )
    _seed_checkin(
        fake_pool,
        user_id=user_a.id,
        bot_id="tante_rosi",
        scheduled_for=datetime.now(UTC) + timedelta(hours=2),
        about_what="tante-A",
    )
    _seed_checkin(
        fake_pool,
        user_id=user_b.id,
        bot_id="mediator",
        scheduled_for=datetime.now(UTC) + timedelta(hours=3),
        about_what="mediator-B",
    )
    ctx = _build_ctx(fake_pool, user_a, bot_id="mediator")
    result = await read_tools.list_scheduled_checkins(
        ctx, ListScheduledCheckinsInput()
    )
    assert len(result.checkins) == 1
    assert result.checkins[0].job_id == mediator_a
    assert result.checkins[0].about_what == "mediator-A"


async def test_excludes_non_pending_rows(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    _seed_checkin(
        fake_pool,
        user_id=user.id,
        bot_id="mediator",
        scheduled_for=datetime.now(UTC) + timedelta(hours=1),
    )
    cancelled_id = _seed_checkin(
        fake_pool,
        user_id=user.id,
        bot_id="mediator",
        scheduled_for=datetime.now(UTC) + timedelta(hours=2),
    )
    fake_pool.scheduled_jobs[cancelled_id]["status"] = "cancelled"
    ctx = _build_ctx(fake_pool, user, bot_id="mediator")
    result = await read_tools.list_scheduled_checkins(
        ctx, ListScheduledCheckinsInput()
    )
    assert len(result.checkins) == 1
    assert result.checkins[0].job_id != cancelled_id


async def test_empty_when_no_pending_rows(fake_pool):
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    ctx = _build_ctx(fake_pool, user, bot_id="mediator")
    result = await read_tools.list_scheduled_checkins(
        ctx, ListScheduledCheckinsInput()
    )
    assert result.checkins == []
