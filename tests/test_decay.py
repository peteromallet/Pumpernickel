"""Tests for decay per-topic scoping (T17).

Confirms that run_decay_housekeeping only touches rows whose artifact_topics
row matches the current scope's topic_id, and that passing an explicit
topic_id scopes correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.services.decay import run_decay_housekeeping

pytestmark = pytest.mark.anyio

RELATIONSHIP_TOPIC = UUID("00000000-0000-4000-8000-000000000001")
OTHER_TOPIC = UUID("00000000-0000-4000-8000-000000000002")


async def test_decay_housekeeping_transitions_against_synthetic_time(fake_pool):
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)
    active_old = uuid4()
    dormant_old = uuid4()
    active_recent = uuid4()
    fake_pool.themes[active_old] = {
        "id": active_old,
        "status": "active",
        "last_reinforced_at": now - timedelta(weeks=7),
        "first_seen_at": now - timedelta(weeks=10),
        "updated_at": now - timedelta(weeks=7),
    }
    fake_pool.themes[dormant_old] = {
        "id": dormant_old,
        "status": "dormant",
        "last_reinforced_at": now - timedelta(days=200),
        "first_seen_at": now - timedelta(days=220),
        "updated_at": now - timedelta(days=130),
    }
    fake_pool.themes[active_recent] = {
        "id": active_recent,
        "status": "active",
        "last_reinforced_at": now - timedelta(days=10),
        "first_seen_at": now - timedelta(days=10),
        "updated_at": now - timedelta(days=10),
    }

    decays_to_low = uuid4()
    goes_stale = uuid4()
    stays_high = uuid4()
    fake_pool.observations[decays_to_low] = {
        "id": decays_to_low,
        "content": "old but not stale",
        "status": "active",
        "confidence": "medium",
        "significance": 3,
        "created_at": now - timedelta(days=100),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.observations[goes_stale] = {
        "id": goes_stale,
        "content": "stale",
        "status": "active",
        "confidence": "high",
        "significance": 4,
        "created_at": now - timedelta(days=200),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.observations[stays_high] = {
        "id": stays_high,
        "content": "fresh",
        "status": "active",
        "confidence": "high",
        "significance": 5,
        "created_at": now - timedelta(days=20),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }

    expired_watch = uuid4()
    fresh_watch = uuid4()
    fake_pool.watch_items[expired_watch] = {
        "id": expired_watch,
        "owner_user_id": uuid4(),
        "content": "expired",
        "status": "open",
        "due_at": now - timedelta(days=31),
        "addressed_at": None,
    }
    fake_pool.watch_items[fresh_watch] = {
        "id": fresh_watch,
        "owner_user_id": uuid4(),
        "content": "fresh",
        "status": "open",
        "due_at": now - timedelta(days=5),
        "addressed_at": None,
    }

    report = await run_decay_housekeeping(fake_pool, now=now)

    assert fake_pool.themes[active_old]["status"] == "dormant"
    assert fake_pool.themes[dormant_old]["status"] == "resolved_by_time"
    assert fake_pool.themes[active_recent]["status"] == "active"
    assert fake_pool.observations[decays_to_low]["confidence"] == "low"
    assert fake_pool.observations[goes_stale]["status"] == "stale"
    assert fake_pool.observations[stays_high]["confidence"] == "high"
    assert fake_pool.watch_items[expired_watch]["status"] == "expired"
    assert fake_pool.watch_items[fresh_watch]["status"] == "open"
    assert report.rescore_report.scanned == 0


async def test_decay_only_touches_rows_for_current_topic(fake_pool):
    """Decay with default topic (relationship) only mutates relationship-linked rows.

    Other-topic rows should remain untouched even if they meet the decay criteria.
    """
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)

    # --- Relationship-linked artifacts (should be mutated) ---
    rel_theme = uuid4()
    fake_pool.themes[rel_theme] = {
        "id": rel_theme, "status": "active",
        "last_reinforced_at": now - timedelta(weeks=7),
        "first_seen_at": now - timedelta(weeks=10),
        "updated_at": now - timedelta(weeks=7),
    }
    fake_pool.link_topic("themes", rel_theme, RELATIONSHIP_TOPIC)

    rel_obs = uuid4()
    fake_pool.observations[rel_obs] = {
        "id": rel_obs, "content": "old obs", "status": "active",
        "confidence": "high", "significance": 3,
        "created_at": now - timedelta(days=130),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.link_topic("observations", rel_obs, RELATIONSHIP_TOPIC)

    rel_watch = uuid4()
    fake_pool.watch_items[rel_watch] = {
        "id": rel_watch, "owner_user_id": uuid4(), "content": "expired watch",
        "status": "open", "due_at": now - timedelta(days=31),
        "addressed_at": None,
    }
    fake_pool.link_topic("watch_items", rel_watch, RELATIONSHIP_TOPIC)

    # --- Other-topic-linked artifacts (should NOT be mutated) ---
    # Same timestamps so they also meet decay criteria — the topic filter
    # is the only discriminant.
    other_theme = uuid4()
    fake_pool.themes[other_theme] = {
        "id": other_theme, "status": "active",
        "last_reinforced_at": now - timedelta(weeks=7),
        "first_seen_at": now - timedelta(weeks=10),
        "updated_at": now - timedelta(weeks=7),
    }
    fake_pool.link_topic("themes", other_theme, OTHER_TOPIC)

    other_obs = uuid4()
    fake_pool.observations[other_obs] = {
        "id": other_obs, "content": "old obs other", "status": "active",
        "confidence": "high", "significance": 3,
        "created_at": now - timedelta(days=130),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.link_topic("observations", other_obs, OTHER_TOPIC)

    other_watch = uuid4()
    fake_pool.watch_items[other_watch] = {
        "id": other_watch, "owner_user_id": uuid4(), "content": "expired other",
        "status": "open", "due_at": now - timedelta(days=31),
        "addressed_at": None,
    }
    fake_pool.link_topic("watch_items", other_watch, OTHER_TOPIC)

    # Run decay with default topic (relationship, resolved from registry)
    await run_decay_housekeeping(fake_pool, now=now)

    # Relationship-linked rows must be mutated
    assert fake_pool.themes[rel_theme]["status"] == "dormant", "rel theme should decay to dormant"
    assert fake_pool.observations[rel_obs]["confidence"] == "medium", "rel obs should decay confidence"
    assert fake_pool.watch_items[rel_watch]["status"] == "expired", "rel watch should expire"

    # Other-topic rows must be untouched
    assert fake_pool.themes[other_theme]["status"] == "active", "other theme must not decay"
    assert fake_pool.observations[other_obs]["confidence"] == "high", "other obs must not decay"
    assert fake_pool.watch_items[other_watch]["status"] == "open", "other watch must not expire"


async def test_decay_scopes_when_explicit_topic_passed(fake_pool):
    """Passing topic_id=OTHER_TOPIC only mutates other-topic rows."""
    now = datetime(2026, 4, 30, 12, 0, tzinfo=UTC)

    # Relationship-linked (should NOT mutate)
    rel_theme = uuid4()
    fake_pool.themes[rel_theme] = {
        "id": rel_theme, "status": "active",
        "last_reinforced_at": now - timedelta(weeks=7),
        "first_seen_at": now - timedelta(weeks=10),
        "updated_at": now - timedelta(weeks=7),
    }
    fake_pool.link_topic("themes", rel_theme, RELATIONSHIP_TOPIC)

    rel_obs = uuid4()
    fake_pool.observations[rel_obs] = {
        "id": rel_obs, "content": "rel obs", "status": "active",
        "confidence": "high", "significance": 3,
        "created_at": now - timedelta(days=130),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.link_topic("observations", rel_obs, RELATIONSHIP_TOPIC)

    rel_watch = uuid4()
    fake_pool.watch_items[rel_watch] = {
        "id": rel_watch, "owner_user_id": uuid4(), "content": "rel watch",
        "status": "open", "due_at": now - timedelta(days=31),
        "addressed_at": None,
    }
    fake_pool.link_topic("watch_items", rel_watch, RELATIONSHIP_TOPIC)

    # Other-topic (SHOULD mutate — we pass this topic explicitly)
    other_theme = uuid4()
    fake_pool.themes[other_theme] = {
        "id": other_theme, "status": "active",
        "last_reinforced_at": now - timedelta(weeks=7),
        "first_seen_at": now - timedelta(weeks=10),
        "updated_at": now - timedelta(weeks=7),
    }
    fake_pool.link_topic("themes", other_theme, OTHER_TOPIC)

    other_obs = uuid4()
    fake_pool.observations[other_obs] = {
        "id": other_obs, "content": "other obs", "status": "active",
        "confidence": "high", "significance": 3,
        "created_at": now - timedelta(days=130),
        "last_reinforced_at": None,
        "scoring_prompt_version": "v1",
    }
    fake_pool.link_topic("observations", other_obs, OTHER_TOPIC)

    other_watch = uuid4()
    fake_pool.watch_items[other_watch] = {
        "id": other_watch, "owner_user_id": uuid4(), "content": "other watch",
        "status": "open", "due_at": now - timedelta(days=31),
        "addressed_at": None,
    }
    fake_pool.link_topic("watch_items", other_watch, OTHER_TOPIC)

    # Run decay with OTHER_TOPIC explicitly
    await run_decay_housekeeping(fake_pool, now=now, topic_id=OTHER_TOPIC)

    # Relationship-linked rows must be untouched
    assert fake_pool.themes[rel_theme]["status"] == "active", "rel theme must not decay"
    assert fake_pool.observations[rel_obs]["confidence"] == "high", "rel obs must not decay"
    assert fake_pool.watch_items[rel_watch]["status"] == "open", "rel watch must not expire"

    # Other-topic rows must be mutated
    assert fake_pool.themes[other_theme]["status"] == "dormant", "other theme should decay"
    assert fake_pool.observations[other_obs]["confidence"] == "medium", "other obs should decay confidence"
    assert fake_pool.watch_items[other_watch]["status"] == "expired", "other watch should expire"