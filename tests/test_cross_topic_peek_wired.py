"""Cross-topic peek wired test (S6).

Verifies that:
1. build_hot_context with allow_cross_topic_peek=True returns non-empty peek.
2. Flag False returns empty peek.
3. build_hot_context_solo flag True returns non-empty peek, AND the peek field
   survives the render_hot_context_solo copy constructor AND appears in the
   rendered solo prompt text.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.bots.registry import BotSpec, ReadScopes, WriteScopes
from app.services.hot_context import build_hot_context, HotContext
from app.services.hot_context_solo import (
    build_hot_context_solo,
    HotContextSolo,
    render_hot_context_solo,
)
from app.models.user import User
from tests.conftest import FakePool


def _make_user(user_id: UUID | None = None, name: str = "testuser") -> User:
    uid = user_id or uuid4()
    return User(
        id=uid,
        name=name,
        phone="+15551234567",
        timezone="America/New_York",
        cross_thread_sharing_default="opt_in",
        onboarding_state="welcomed",
    )


@pytest.mark.asyncio
async def test_cross_topic_peek_dyad_flag_on() -> None:
    """build_hot_context with allow_cross_topic_peek=True returns non-empty peek."""
    pool = FakePool()
    # Seed topics
    career_id = uuid4()
    relationship_id = uuid4()
    pool.topics["career"] = {"id": career_id, "slug": "career", "display_name": "Career"}
    pool.topics["relationship"] = {"id": relationship_id, "slug": "relationship", "display_name": "Relationship"}

    user = _make_user()
    partner = _make_user(name="partner")
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "cross_thread_sharing_default": "opt_in",
        "style_notes": "",
    }
    pool.users[partner.id] = {
        "id": partner.id, "name": partner.name, "phone": partner.phone,
        "timezone": partner.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "cross_thread_sharing_default": "opt_in",
        "style_notes": "",
    }

    dyad_id = uuid4()

    # Seed topic_status for career (the "other" topic) within the 14-day window
    now = datetime.now(UTC)
    pool.topic_status[(career_id, dyad_id)] = {
        "id": uuid4(), "topic_id": career_id, "dyad_id": dyad_id, "user_id": user.id,
        "headline": "Career progress", "body": "Working on promotion",
        "last_updated_at": now - timedelta(days=2),
    }
    # Also seed relationship topic_status (the primary topic, should be excluded from peek)
    pool.topic_status[(relationship_id, dyad_id)] = {
        "id": uuid4(), "topic_id": relationship_id, "dyad_id": dyad_id, "user_id": user.id,
        "headline": "Relationship status", "body": "Doing well",
        "last_updated_at": now - timedelta(days=1),
    }

    hc = await build_hot_context(
        pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        primary_topic_id=relationship_id,
        dyad_id=dyad_id,
        allow_cross_topic_peek=True,
    )
    assert isinstance(hc, HotContext)
    assert len(hc.cross_topic_peek) > 0, "Expected non-empty cross_topic_peek with flag=True"
    assert any(item["slug"] == "career" for item in hc.cross_topic_peek), (
        "Expected career topic in peek"
    )
    for item in hc.cross_topic_peek:
        assert "last_active_at" in item, f"Missing last_active_at in peek item: {item}"


@pytest.mark.asyncio
async def test_cross_topic_peek_dyad_flag_off() -> None:
    """build_hot_context with allow_cross_topic_peek=False returns empty peek."""
    pool = FakePool()
    career_id = uuid4()
    relationship_id = uuid4()
    pool.topics["career"] = {"id": career_id, "slug": "career", "display_name": "Career"}
    pool.topics["relationship"] = {"id": relationship_id, "slug": "relationship", "display_name": "Relationship"}

    user = _make_user()
    partner = _make_user(name="partner")
    for u in (user, partner):
        pool.users[u.id] = {
            "id": u.id, "name": u.name, "phone": u.phone,
            "timezone": u.timezone, "onboarding_state": "welcomed",
            "pacing_preferences": {}, "cross_thread_sharing_default": "opt_in",
            "style_notes": "",
        }

    dyad_id = uuid4()
    now = datetime.now(UTC)
    pool.topic_status[(career_id, dyad_id)] = {
        "id": uuid4(), "topic_id": career_id, "dyad_id": dyad_id, "user_id": user.id,
        "headline": "Career progress", "body": "...",
        "last_updated_at": now - timedelta(days=2),
    }

    hc = await build_hot_context(
        pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        primary_topic_id=relationship_id,
        dyad_id=dyad_id,
        allow_cross_topic_peek=False,
    )
    assert isinstance(hc, HotContext)
    assert len(hc.cross_topic_peek) == 0, "Expected empty cross_topic_peek with flag=False"


@pytest.mark.asyncio
async def test_cross_topic_peek_solo_survives_truncation_and_renders() -> None:
    """build_hot_context_solo with flag=True: peek field survives copy constructor AND renders."""
    pool = FakePool()
    career_id = uuid4()
    coach_id = uuid4()
    pool.topics["career"] = {"id": career_id, "slug": "career", "display_name": "Career"}
    pool.topics["coach"] = {"id": coach_id, "slug": "coach", "display_name": "Coach"}

    user = _make_user()
    pool.users[user.id] = {
        "id": user.id, "name": user.name, "phone": user.phone,
        "timezone": user.timezone, "onboarding_state": "welcomed",
        "pacing_preferences": {}, "cross_thread_sharing_default": "opt_in",
        "style_notes": "",
    }

    now = datetime.now(UTC)
    pool.topic_status[(career_id, user.id)] = {
        "id": uuid4(), "topic_id": career_id, "user_id": user.id,
        "headline": "Career progress", "body": "...",
        "last_updated_at": now - timedelta(days=2),
    }

    hc = await build_hot_context_solo(
        pool,
        user=user,
        triggering_message_ids=[],
        primary_topic_id=coach_id,
        bot_id="coach",
        allow_cross_topic_peek=True,
    )
    assert isinstance(hc, HotContextSolo)
    assert len(hc.cross_topic_peek) > 0, "Expected non-empty cross_topic_peek for solo with flag=True"

    # Verify the field survives the copy constructor in render_hot_context_solo
    rendered = render_hot_context_solo(hc)
    assert isinstance(rendered, str)
    assert "Cross-topic activity" in rendered, (
        f"Expected 'Cross-topic activity' in rendered solo prompt, got: ...{rendered[-200:]}"
    )
    assert "career" in rendered, (
        f"Expected 'career' in rendered solo prompt, got: ...{rendered[-200:]}"
    )