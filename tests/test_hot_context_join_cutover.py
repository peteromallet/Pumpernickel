"""Property / frozen-fixture tests for the artifact_topics join cutover (T14).

Confirms that build_hot_context + render_hot_context output is byte-identical
before and after the Sprint 3 read-rewrites, given identical seeded data.

Off-limits rule: we create a new file rather than touching test_hot_context.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import UUID

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.hot_context import build_hot_context, render_hot_context

pytestmark = pytest.mark.anyio

# Fixed UUIDs matching the s2b capture script
USER_A_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-000000000001")
USER_B_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-000000000002")
MEM_ID    = UUID("cccccccc-cccc-4ccc-8ccc-000000000003")
THEME_ID  = UUID("dddddddd-dddd-4ddd-8ddd-000000000004")
WATCH_ID  = UUID("eeeeeeee-eeee-4eee-8eee-000000000005")
OBS_ID    = UUID("ffffffff-ffff-4fff-8fff-000000000006")
DIST_ID   = UUID("11111111-1111-4111-8111-000000000007")
OOB_ID    = UUID("22222222-2222-4222-8222-000000000008")

FROZEN_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)

# Captured from s2b-constraints-soak tip (13fb67e) via capture_hot_context.py
# with mocked datetime.now(UTC) → 2026-05-12T12:00:00+00:00.
_S2B_RENDERED_HOT_CONTEXT = (
    "## You\n"
    "- id: aaaaaaaa-aaaa-4aaa-8aaa-000000000001\n"
    "- name: Alice\n"
    "- timezone: America/New_York\n"
    "- onboarding_state: welcomed\n"
    "- sharing_default: opt_in\n"
    "- style_notes: \n"
    "\n"
    "## Your Partner\n"
    "- id: bbbbbbbb-bbbb-4bbb-8bbb-000000000002\n"
    "- name: Bob\n"
    "- timezone: America/Chicago\n"
    "- onboarding_state: welcomed\n"
    "- sharing_default: opt_in\n"
    "- style_notes: \n"
    "\n"
    "## Current time\n"
    "- now_utc: 2026-05-12T12:00:00+00:00\n"
    "- now_local: 2026-05-12T08:00:00-04:00\n"
    "- timezone: America/New_York\n"
    "- local_date: 2026-05-12\n"
    "- local_time: 08:00:00\n"
    "- local_weekday: Tuesday\n"
    "- local_day_bounds: 2026-05-12T00:00:00-04:00 to 2026-05-13T00:00:00-04:00"
    " (UTC 2026-05-12T04:00:00+00:00 to 2026-05-13T04:00:00+00:00)\n"
    "- one_month_from_now: local=2026-06-12T08:00:00-04:00"
    " utc=2026-06-12T12:00:00+00:00 local_date=2026-06-12\n"
    "- scheduling_note: Default to scheduling tool delay fields for simple"
    " duration phrases like 'in two hours', 'in 10 hours', or 'in two days'."
    " Use local_when for concrete local clock phrases like '9pm tonight' or"
    " 'Monday at 8'. Use absolute when only for exact timezone-aware instants."
    " For phrases like 'for the next month', use the"
    " one_month_from_now/local_date anchors rather than guessing.\n"
    "\n"
    "## Sharing defaults\n"
    "- current_user: opt_in\n"
    "- partner: opt_in\n"
    "\n"
    "## Conversation load\n"
    "- period: today\n"
    "- timezone: America/New_York\n"
    "- local_period_bounds: 2026-05-12T00:00:00-04:00 to 2026-05-13T00:00:00-04:00\n"
    "- utc_period_bounds: 2026-05-12T00:00:00+00:00 to 2026-05-13T00:00:00+00:00\n"
    "- total_messages: 0\n"
    "- inbound_messages: 0\n"
    "- outbound_messages: 0\n"
    "\n"
    "## Active OOB (severity)\n"
    "- id=22222222-2222-4222-8222-000000000008 firm"
    " owner=aaaaaaaa-aaaa-4aaa-8aaa-000000000001"
    " review=2026-08-10 08:00 New York (in 90 days;"
    " utc=2026-08-10T12:00:00+00:00) context=Alice's financial concerns\n"
    "\n"
    "## Active themes\n"
    "- id=dddddddd-dddd-4ddd-8ddd-000000000004"
    " last=2026-05-02 08:00 New York (10 days ago;"
    " utc=2026-05-02T12:00:00+00:00) Communication styles"
    " (active, neutral, stable): Alice and Bob have different"
    " communication preferences\n"
    "\n"
    "## Memories\n"
    "- id=cccccccc-cccc-4ccc-8ccc-000000000003"
    " time=5 days ago 08:00 New York (5 days ago;"
    " utc=2026-05-07T12:00:00+00:00)"
    " about=aaaaaaaa-aaaa-4aaa-8aaa-000000000001:"
    " Alice prefers direct communication\n"
    "\n"
    "## Open watch items\n"
    "- id=eeeeeeee-eeee-4eee-8eee-000000000005"
    " due=in 3 days 08:00 New York (in 3 days;"
    " utc=2026-05-15T12:00:00+00:00) Schedule weekly check-in call\n"
    "\n"
    "## High-significance observations\n"
    "- id=ffffffff-ffff-4fff-8fff-000000000006"
    " time=2026-04-27 08:00 New York (15 days ago;"
    " utc=2026-04-27T12:00:00+00:00) sig=4 confidence=high"
    " about=bbbbbbbb-bbbb-4bbb-8bbb-000000000002:"
    " Bob responds better to written communication than calls\n"
    "\n"
    "## Distillations\n"
    "- id=11111111-1111-4111-8111-000000000007"
    " time=2026-05-02 08:00 New York (10 days ago;"
    " utc=2026-05-02T12:00:00+00:00) display=full_content"
    " confidence=high sensitivity=low visibility=dyad_shareable"
    " sources=aaaaaaaa-aaaa-4aaa-8aaa-000000000001,"
    " bbbbbbbb-bbbb-4bbb-8bbb-000000000002: The couple works best"
    " when they communicate expectations clearly in writing\n"
    "- use get_distillations before adding or revising synthesized explanations.\n"
    "\n"
    "## Bridge candidates\n"
    "- none\n"
    "\n"
    "## Recent messages\n"
    "\n"
    "## New reactions since previous turn\n"
    "- none\n"
    "\n"
    "## Trigger\n"
    "- kind: inbound\n"
    "- triggering_message_ids: \n"
    "- time_since_last_message:"
)


def _seed_fixture(pool) -> tuple[User, User]:
    """Seed one artifact of each family + two users; return (user, partner)."""
    pool.users.setdefault(USER_A_ID, {
        "id": USER_A_ID, "name": "Alice", "phone": "15555550100",
        "timezone": "America/New_York", "style_notes": "", "onboarding_state": "welcomed",
        "cross_thread_sharing_default": "opt_in",
    })
    pool.users.setdefault(USER_B_ID, {
        "id": USER_B_ID, "name": "Bob", "phone": "15555550101",
        "timezone": "America/Chicago", "style_notes": "", "onboarding_state": "welcomed",
        "cross_thread_sharing_default": "opt_in",
    })

    user = User(id=USER_A_ID, name="Alice", phone="15555550100", timezone="America/New_York")
    partner = User(id=USER_B_ID, name="Bob", phone="15555550101", timezone="America/Chicago")

    pool.memories[MEM_ID] = {
        "id": MEM_ID, "about_user_id": USER_A_ID,
        "content": "Alice prefers direct communication",
        "related_theme_ids": [], "status": "active",
        "created_at": FROZEN_NOW - timedelta(days=30),
        "last_referenced_at": FROZEN_NOW - timedelta(days=5),
    }
    pool.themes[THEME_ID] = {
        "id": THEME_ID, "title": "Communication styles",
        "description": "Alice and Bob have different communication preferences",
        "status": "active", "sentiment": "neutral", "health": "stable",
        "last_reinforced_at": FROZEN_NOW - timedelta(days=10),
        "last_active_at": FROZEN_NOW - timedelta(days=3),
        "first_seen_at": FROZEN_NOW - timedelta(days=60),
    }
    pool.watch_items[WATCH_ID] = {
        "id": WATCH_ID, "owner_user_id": USER_A_ID,
        "content": "Schedule weekly check-in call",
        "due_at": FROZEN_NOW + timedelta(days=3), "status": "open",
        "created_at": FROZEN_NOW - timedelta(days=1),
        "related_theme_ids": [THEME_ID],
        "addressing_note": None, "addressed_at": None,
    }
    pool.observations[OBS_ID] = {
        "id": OBS_ID, "about_user_id": USER_B_ID,
        "content": "Bob responds better to written communication than calls",
        "confidence": "high", "significance": 4, "status": "active",
        "related_theme_ids": [THEME_ID],
        "last_reinforced_at": FROZEN_NOW - timedelta(days=15),
        "created_at": FROZEN_NOW - timedelta(days=45),
    }
    pool.distillations[DIST_ID] = {
        "id": DIST_ID,
        "content": "The couple works best when they communicate expectations clearly in writing",
        "confidence": "high", "status": "active",
        "sensitivity": "low", "visibility": "dyad_shareable",
        "shareable_summary": "Written communication helps clarify expectations",
        "source_user_ids": [USER_A_ID, USER_B_ID],
        "related_memory_ids": [MEM_ID],
        "related_observation_ids": [OBS_ID],
        "related_theme_ids": [THEME_ID],
        "supporting_message_ids": [],
        "revision_note": None, "revision_count": 0,
        "created_at": FROZEN_NOW - timedelta(days=20),
        "updated_at": FROZEN_NOW - timedelta(days=10),
    }
    pool.out_of_bounds[OOB_ID] = {
        "id": OOB_ID, "owner_id": USER_A_ID,
        "shareable_context": "Alice's financial concerns",
        "sensitive_core": "Alice's financial concerns (private)",
        "severity": "firm", "review_at": FROZEN_NOW + timedelta(days=90),
        "status": "active", "created_at": FROZEN_NOW - timedelta(days=10),
    }

    # Link every artifact to the relationship topic
    rel_topic = get_relationship_topic_id()
    for table, aid in [
        ("memories", MEM_ID),
        ("themes", THEME_ID),
        ("watch_items", WATCH_ID),
        ("observations", OBS_ID),
        ("distillations", DIST_ID),
        ("out_of_bounds", OOB_ID),
    ]:
        pool.link_topic(table, aid, rel_topic)

    return user, partner


async def test_hot_context_output_unchanged_post_join_cutover(fake_pool, monkeypatch):
    """Byte-equality check: s3 output matches the s2b frozen-fixture snapshot."""
    user, partner = _seed_fixture(fake_pool)

    # Mock datetime.now(UTC) everywhere the hot_context module touches time.
    # build_hot_context calls datetime.now(UTC) at line 189.
    # _duration_since calls datetime.now(UTC) at line 99.
    # The FakePool conversation_load handler also calls datetime.now(UTC).
    import app.services.hot_context as hcmod
    import tests.conftest as cfmod

    # Replace datetime.now on both modules
    frozen_dt = FROZEN_NOW

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen_dt

    orig_hc_dt = hcmod.datetime
    orig_cf_dt = cfmod.datetime
    hcmod.datetime = FrozenDateTime
    cfmod.datetime = FrozenDateTime

    try:
        hc = await build_hot_context(
            fake_pool, user, partner, [],
            primary_topic_id=get_relationship_topic_id(),
        )
        rendered = render_hot_context(hc)
    finally:
        hcmod.datetime = orig_hc_dt
        cfmod.datetime = orig_cf_dt

    # Byte-equality
    assert rendered == _S2B_RENDERED_HOT_CONTEXT, (
        f"Hot context output differs from s2b baseline.\n"
        f"--- Expected (s2b) ---\n{_S2B_RENDERED_HOT_CONTEXT!r}\n"
        f"--- Got (s3) ---\n{rendered!r}"
    )

    # List-length checks: each seeded artifact family has exactly one row
    assert len(hc.memories) == 1
    assert len(hc.active_themes) == 1
    assert len(hc.open_watch_items) == 1
    assert len(hc.observations) == 1
    assert len(hc.distillations) == 1
    assert len(hc.active_oob) == 1


async def test_build_hot_context_raises_when_topic_registry_unset(fake_pool, monkeypatch):
    """RuntimeError when get_relationship_topic_id() returns None and no
    primary_topic_id is passed."""
    user, partner = _seed_fixture(fake_pool)

# Force registry to return None.  build_hot_context imported
    # get_relationship_topic_id directly, so we must patch the reference
    # in hot_context, not in bots.registry.
    monkeypatch.setattr(
        "app.services.hot_context.get_relationship_topic_id",
        lambda: None,
    )

    with pytest.raises(RuntimeError, match="no primary_topic_id"):
        await build_hot_context(fake_pool, user, partner, [])