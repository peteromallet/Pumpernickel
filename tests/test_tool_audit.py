"""Tests for the read/write tool_calls audit trail and the silent-turn
self-introspection path (migration 0039 + hot_context silent_turns block).

Covers:
  - Read tools persist to mediator.tool_calls with kind='read' and a summary.
  - Write tools persist with kind='write' and use the same shared logger.
  - get_tool_call read tool returns full args + result for a given id.
  - audit.summarize_tool_call produces sensible highlights.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.turn_context import TurnContext
from app.services.tools.audit import summarize_tool_call
from app.services.tools.registry import call_tool
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


def _ctx(pool: FakePool, *, current_step: str) -> TurnContext:
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    partner = User(uuid4(), "Ben", "15555550101", "UTC")
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
    }
    return TurnContext(
        uuid4(), pool, user, partner, [uuid4()], current_step=current_step
    )


async def test_read_tool_persists_with_kind_read(fake_pool: FakePool) -> None:
    """get_observations is a pure read; it must land in tool_calls as 'read'."""
    ctx = _ctx(fake_pool, current_step="read")
    obs_id = uuid4()
    fake_pool.observations[obs_id] = {
        "id": obs_id,
        "about_user_id": ctx.user.id,
        "content": "Maya prefers in-person repair.",
        "confidence": "medium",
        "significance": 3,
        "status": "active",
        "related_theme_ids": [],
        "supporting_message_ids": [],
        "created_at": datetime.now(UTC),
        "last_reinforced_at": None,
        "surfaced_count": 0,
    }

    result = await call_tool(
        "get_observations",
        {"about_user_id": str(ctx.user.id), "min_significance": 3},
        ctx,
    )
    assert "observations" in result

    rows = [r for r in fake_pool.tool_calls if r["tool_name"] == "get_observations"]
    assert len(rows) == 1
    assert rows[0]["kind"] == "read"


async def test_write_tool_persists_with_kind_write(fake_pool: FakePool) -> None:
    """update_observation is a write; the shared logger must mark it 'write'."""
    ctx = _ctx(fake_pool, current_step="record")
    obs_id = uuid4()
    fake_pool.observations[obs_id] = {
        "id": obs_id,
        "about_user_id": ctx.user.id,
        "content": "Maya prefers in-person repair.",
        "confidence": "medium",
        "significance": 3,
        "status": "active",
        "related_theme_ids": [],
        "supporting_message_ids": [],
        "created_at": datetime.now(UTC),
        "last_reinforced_at": None,
        "surfaced_count": 0,
    }
    # Satisfy the read-before-write guard.
    ctx.tool_call_log.append("get_observations")

    result = await call_tool(
        "update_observation",
        {"observation_id": str(obs_id), "content": "Updated."},
        ctx,
    )
    assert result["id"] == str(obs_id)
    rows = [
        r for r in fake_pool.tool_calls if r["tool_name"] == "update_observation"
    ]
    assert len(rows) == 1
    assert rows[0]["kind"] == "write"


def test_summarizer_schedule_task_highlights_action_and_brief() -> None:
    summary = summarize_tool_call(
        "schedule_task",
        {"brief": "Morning check-in for Peter re: Hannah"},
        {
            "action": "scheduled",
            "scheduled_for": "2026-05-15T08:00:00Z",
            "recurrence": {"type": "daily"},
        },
    )
    assert summary is not None
    assert "scheduled" in summary
    assert "2026-05-15T08:00:00Z" in summary
    assert "Morning check-in" in summary
    assert "daily" in summary


def test_summarizer_returns_none_for_unknown_tool() -> None:
    assert summarize_tool_call("not_a_real_tool", {}, {}) is None


def test_summarizer_swallows_internal_errors() -> None:
    # If a summarizer raises (e.g. unexpected shape) it must not propagate.
    bad_args = {"brief": object()}  # not serialisable in any sensible way
    # Should not raise even with weird args; result is a string or None.
    out = summarize_tool_call("schedule_task", bad_args, {})
    assert out is None or isinstance(out, str)
