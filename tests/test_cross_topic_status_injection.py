"""Cross-topic status injection wired test (S6).

Verifies that:
1. build_hot_context (dyad) with allow_cross_topic_status_injection=True
   returns cross_topic_status containing the coach-authored career headline,
   and it appears in rendered output.
2. Solo bot path does NOT get cross-topic status injection (no flag, no field).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.services.hot_context import (
    build_hot_context,
    fetch_cross_topic_status,
    HotContext,
)
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
        onboarding_state="welcomed",
    )


@pytest.mark.asyncio
async def test_cross_topic_status_injection_dyad_flag_on() -> None:
    """Mediator dyad path sees coach-authored career status when flag is True."""
    pool = FakePool()
    career_id = uuid4()
    relationship_id = uuid4()
    coach_bot_id = "coach"
    mediator_bot_id = "mediator"

    pool.topics["career"] = {
        "id": career_id,
        "slug": "career",
        "display_name": "Career",
    }
    pool.topics["relationship"] = {
        "id": relationship_id,
        "slug": "relationship",
        "display_name": "Relationship",
    }

    user = _make_user()
    partner = _make_user(name="partner")
    for u in (user, partner):
        pool.users[u.id] = {
            "id": u.id,
            "name": u.name,
            "phone": u.phone,
            "timezone": u.timezone,
            "onboarding_state": "welcomed",
            "pacing_preferences": {},
            "style_notes": "",
        }

    dyad_id = uuid4()
    now = datetime.now(UTC)

    # Coach-authored topic_status in career (the "other" topic)
    coach_headline = "Making progress on promotion track"
    pool.topic_status[(career_id, user.id)] = {
        "id": uuid4(),
        "topic_id": career_id,
        "user_id": user.id,
        "headline": coach_headline,
        "body": "Working with mentor on case",
        "last_updated_at": now - timedelta(days=1),
        "recorded_by_bot_id": coach_bot_id,
    }
    # Also seed career topic_status scoped to dyad_id for the dyad query path
    pool.topic_status[(career_id, dyad_id)] = {
        "id": uuid4(),
        "topic_id": career_id,
        "dyad_id": dyad_id,
        "user_id": user.id,
        "headline": coach_headline,
        "body": "Working with mentor on case",
        "last_updated_at": now - timedelta(days=1),
        "recorded_by_bot_id": coach_bot_id,
    }

    # Mediator-authored topic_status in relationship (primary topic, should be excluded)
    pool.topic_status[(relationship_id, user.id)] = {
        "id": uuid4(),
        "topic_id": relationship_id,
        "user_id": user.id,
        "headline": "Relationship is strong",
        "body": "...",
        "last_updated_at": now - timedelta(hours=4),
        "recorded_by_bot_id": mediator_bot_id,
    }
    pool.topic_status[(relationship_id, dyad_id)] = {
        "id": uuid4(),
        "topic_id": relationship_id,
        "dyad_id": dyad_id,
        "user_id": user.id,
        "headline": "Relationship status (dyad)",
        "body": "Good",
        "last_updated_at": now - timedelta(hours=3),
        "recorded_by_bot_id": mediator_bot_id,
    }

    hc = await build_hot_context(
        pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        primary_topic_id=relationship_id,
        dyad_id=dyad_id,
        allow_cross_topic_status_injection=True,
    )
    assert isinstance(hc, HotContext)
    assert (
        len(hc.cross_topic_status) > 0
    ), "Expected non-empty cross_topic_status with flag=True"
    # Verify coach's career headline is present
    career_items = [
        item for item in hc.cross_topic_status if item.get("slug") == "career"
    ]
    assert len(career_items) > 0, "Expected career topic in cross_topic_status"
    assert any(
        coach_headline in item.get("headline", "") for item in career_items
    ), f"Expected coach headline '{coach_headline}' in cross_topic_status items: {career_items}"

    # Verify the status appears in rendered output
    from app.services.hot_context import render_hot_context

    rendered = render_hot_context(hc)
    assert (
        "Cross-topic status" in rendered
    ), f"Expected 'Cross-topic status' in rendered dyad prompt, got: ...{rendered[-200:]}"


@pytest.mark.asyncio
async def test_cross_topic_status_injection_dyad_flag_off() -> None:
    """build_hot_context with flag=False returns empty cross_topic_status."""
    pool = FakePool()
    career_id = uuid4()
    relationship_id = uuid4()
    coach_bot_id = "coach"

    pool.topics["career"] = {
        "id": career_id,
        "slug": "career",
        "display_name": "Career",
    }
    pool.topics["relationship"] = {
        "id": relationship_id,
        "slug": "relationship",
        "display_name": "Relationship",
    }

    user = _make_user()
    partner = _make_user(name="partner")
    for u in (user, partner):
        pool.users[u.id] = {
            "id": u.id,
            "name": u.name,
            "phone": u.phone,
            "timezone": u.timezone,
            "onboarding_state": "welcomed",
            "pacing_preferences": {},
            "style_notes": "",
        }

    dyad_id = uuid4()
    now = datetime.now(UTC)
    pool.topic_status[(career_id, user.id)] = {
        "id": uuid4(),
        "topic_id": career_id,
        "user_id": user.id,
        "headline": "Career progress",
        "body": "...",
        "last_updated_at": now - timedelta(days=1),
        "recorded_by_bot_id": coach_bot_id,
    }

    hc = await build_hot_context(
        pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        primary_topic_id=relationship_id,
        dyad_id=dyad_id,
        allow_cross_topic_status_injection=False,
    )
    assert (
        len(hc.cross_topic_status) == 0
    ), "Expected empty cross_topic_status with flag=False"


@pytest.mark.asyncio
async def test_solo_bot_no_cross_topic_status_injection() -> None:
    """Solo bot path does not get cross-topic status injection."""
    pool = FakePool()
    career_id = uuid4()
    coach_id = uuid4()
    coach_bot_id = "coach"

    pool.topics["career"] = {
        "id": career_id,
        "slug": "career",
        "display_name": "Career",
    }
    pool.topics["coach"] = {"id": coach_id, "slug": "coach", "display_name": "Coach"}

    user = _make_user()
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
        "pacing_preferences": {},
        "style_notes": "",
    }

    now = datetime.now(UTC)
    pool.topic_status[(career_id, user.id)] = {
        "id": uuid4(),
        "topic_id": career_id,
        "user_id": user.id,
        "headline": "Some other status",
        "body": "...",
        "last_updated_at": now - timedelta(days=1),
        "recorded_by_bot_id": coach_bot_id,
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
    # Solo HotContextSolo has no cross_topic_status field at all
    assert not hasattr(
        hc, "cross_topic_status"
    ), "HotContextSolo should not have cross_topic_status field"

    rendered = render_hot_context_solo(hc)
    assert (
        "Cross-topic status" not in rendered
    ), "Solo rendered prompt should not contain 'Cross-topic status'"
