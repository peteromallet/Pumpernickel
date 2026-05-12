"""Solo-bot about_user/owner guards on artifact-write tools (S7 audit fix 1).

Verifies that for solo bots (participants_shape == "solo"), writes targeting
a user other than ctx.user.id are rejected for every user-targeting artifact-
write tool — creates and updates.  Dyad/mediator paths are unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.bots.base import BotSpec
from app.models.user import User
from app.services.tools import write_tools
from app.services.tools.write_tools import ToolCallRejected
from app.services.turn_context import TurnContext
from tests.conftest import FakePool
from tool_schemas import (
    AddDistillationInput,
    AddMemoryInput,
    AddOOBInput,
    AddWatchItemInput,
    AddressWatchItemInput,
    Confidence,
    DistillationSensitivity,
    DistillationVisibility,
    LiftOOBInput,
    LogObservationInput,
    OOBSeverity,
    SupersedeMemoryInput,
    UpdateMemoryInput,
    UpdateObservationInput,
)


def _solo_spec() -> BotSpec:
    return BotSpec(
        bot_id="coach",
        prompt_renderer=lambda *args, **kwargs: "system prompt",
        step_instructions={"respond": "stub"},
        participants_shape="solo",
        primary_topic_slug="career",
    )


def _dyad_spec() -> BotSpec:
    return BotSpec(
        bot_id="mediator",
        prompt_renderer=lambda *args, **kwargs: "system prompt",
        step_instructions={"respond": "stub"},
        participants_shape="dyad",
        primary_topic_slug="relationship",
    )


def _solo_ctx(pool: FakePool, spec: BotSpec, user: User) -> TurnContext:
    turn_id = uuid4()
    pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
    }
    return TurnContext(
        turn_id,
        pool,
        user,
        None,  # solo bots have no partner
        [uuid4()],
        bot_id=spec.bot_id,
        bot_spec=spec,
        participants_shape=spec.participants_shape,
        primary_topic_slug=spec.primary_topic_slug,
        current_step="record",
    )


def _make_user(name: str = "Coachee") -> User:
    return User(id=uuid4(), name=name, phone=f"+15555550{uuid4().int % 1000:03d}", timezone="UTC")


def _seed_memory(pool: FakePool, about_user_id):
    mid = uuid4()
    pool.memories[mid] = {
        "id": mid,
        "about_user_id": about_user_id,
        "content": "old",
        "related_theme_ids": [],
        "status": "active",
        "created_at": datetime.now(UTC),
        "last_referenced_at": None,
    }
    return mid


def _seed_observation(pool: FakePool, about_user_id):
    oid = uuid4()
    pool.observations[oid] = {
        "id": oid,
        "about_user_id": about_user_id,
        "content": "obs",
        "confidence": "medium",
        "significance": 3,
        "scoring_prompt_version": "v1",
        "related_theme_ids": [],
        "supporting_message_ids": [],
        "status": "active",
    }
    return oid


def _seed_watch_item(pool: FakePool, owner_user_id):
    wid = uuid4()
    pool.watch_items[wid] = {
        "id": wid,
        "owner_user_id": owner_user_id,
        "content": "watch",
        "due_at": None,
        "related_theme_ids": [],
        "status": "open",
    }
    return wid


def _seed_oob(pool: FakePool, owner_id):
    oid = uuid4()
    pool.out_of_bounds[oid] = {
        "id": oid,
        "owner_id": owner_id,
        "sensitive_core": "core",
        "shareable_context": None,
        "severity": "amber",
        "status": "active",
    }
    return oid


# ── Create-shaped writes ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_memory_rejects_other_user_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = AddMemoryInput(about_user_id=user_b.id, content="hi")
    with pytest.raises(ToolCallRejected):
        await write_tools.add_memory(ctx, args)


@pytest.mark.asyncio
async def test_add_memory_solo_null_rejected() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    # bypass pydantic by mutating the input directly
    args = AddMemoryInput(about_user_id=user_a.id, content="hi")
    object.__setattr__(args, "about_user_id", None)
    with pytest.raises(ToolCallRejected):
        await write_tools.add_memory(ctx, args)


@pytest.mark.asyncio
async def test_log_observation_rejects_other_user_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = LogObservationInput(
        content="obs", about_user_id=user_b.id, confidence=Confidence.medium, significance=3
    )
    with pytest.raises(ToolCallRejected):
        await write_tools.log_observation(ctx, args)


@pytest.mark.asyncio
async def test_add_watch_item_rejects_other_user_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = AddWatchItemInput(owner_user_id=user_b.id, content="watch")
    with pytest.raises(ToolCallRejected):
        await write_tools.add_watch_item(ctx, args)


@pytest.mark.asyncio
async def test_add_watch_item_solo_null_rejected() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = AddWatchItemInput(owner_user_id=user_a.id, content="watch")
    object.__setattr__(args, "owner_user_id", None)
    with pytest.raises(ToolCallRejected):
        await write_tools.add_watch_item(ctx, args)


@pytest.mark.asyncio
async def test_add_oob_rejects_other_user_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = AddOOBInput(owner_id=user_b.id, sensitive_core="x", severity=OOBSeverity.soft)
    with pytest.raises(ToolCallRejected):
        await write_tools.add_oob(ctx, args)


@pytest.mark.asyncio
async def test_add_oob_solo_null_rejected() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = AddOOBInput(owner_id=user_a.id, sensitive_core="x", severity=OOBSeverity.soft)
    object.__setattr__(args, "owner_id", None)
    with pytest.raises(ToolCallRejected):
        await write_tools.add_oob(ctx, args)


@pytest.mark.asyncio
async def test_add_distillation_rejects_other_user_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    # seed a memory link so evidence validation passes; the solo guard fires first
    mem_id = _seed_memory(pool, user_a.id)
    args = AddDistillationInput(
        content="x",
        confidence=Confidence.medium,
        sensitivity=DistillationSensitivity.medium,
        visibility=DistillationVisibility.private,
        source_user_ids=[user_b.id],
        related_memory_ids=[mem_id],
    )
    with pytest.raises(ToolCallRejected):
        await write_tools.add_distillation(ctx, args)


# ── Update-shaped writes ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_memory_rejects_other_owner_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    mem_id = _seed_memory(pool, user_b.id)  # owned by B
    args = UpdateMemoryInput(memory_id=mem_id, content="new content")
    with pytest.raises(ToolCallRejected):
        await write_tools.update_memory(ctx, args)


@pytest.mark.asyncio
async def test_supersede_memory_rejects_other_owner_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    mem_id = _seed_memory(pool, user_b.id)
    args = SupersedeMemoryInput(old_memory_id=mem_id, new_content="new")
    with pytest.raises(ToolCallRejected):
        await write_tools.supersede_memory(ctx, args)


@pytest.mark.asyncio
async def test_update_observation_rejects_other_owner_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    obs_id = _seed_observation(pool, user_b.id)
    args = UpdateObservationInput(observation_id=obs_id, content="new")
    with pytest.raises(ToolCallRejected):
        await write_tools.update_observation(ctx, args)


@pytest.mark.asyncio
async def test_address_watch_item_rejects_other_owner_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    wid = _seed_watch_item(pool, user_b.id)
    args = AddressWatchItemInput(watch_item_id=wid, addressing_note="done")
    with pytest.raises(ToolCallRejected):
        await write_tools.address_watch_item(ctx, args)


@pytest.mark.asyncio
async def test_lift_oob_rejects_other_owner_on_solo() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    user_b = _make_user("B")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    oid = _seed_oob(pool, user_b.id)
    args = LiftOOBInput(oob_id=oid)
    with pytest.raises(ToolCallRejected):
        await write_tools.lift_oob(ctx, args)


# ── Positive cases: solo bot with field == bound user_id succeeds ──────────


@pytest.mark.asyncio
async def test_solo_guard_allows_own_user_id_add_watch_item() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = AddWatchItemInput(owner_user_id=user_a.id, content="watch")
    result = await write_tools.add_watch_item(ctx, args)
    assert result.id is not None


@pytest.mark.asyncio
async def test_solo_guard_allows_own_user_id_add_oob() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    args = AddOOBInput(owner_id=user_a.id, sensitive_core="x", severity=OOBSeverity.soft)
    result = await write_tools.add_oob(ctx, args)
    assert result.id is not None


@pytest.mark.asyncio
async def test_solo_guard_allows_own_owner_update_memory() -> None:
    pool = FakePool()
    user_a = _make_user("A")
    ctx = _solo_ctx(pool, _solo_spec(), user_a)
    mem_id = _seed_memory(pool, user_a.id)
    args = UpdateMemoryInput(memory_id=mem_id, content="new")
    result = await write_tools.update_memory(ctx, args)
    assert result.id == mem_id


# ── Negative: dyad bot is unaffected by the guard ───────────────────────────


@pytest.mark.asyncio
async def test_dyad_bot_can_target_partner_on_add_watch_item() -> None:
    """Mediator (dyad) must NOT be blocked from setting owner_user_id to partner."""
    pool = FakePool()
    user_a = _make_user("A")
    partner = _make_user("B")
    spec = _dyad_spec()
    turn_id = uuid4()
    pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
    }
    ctx = TurnContext(
        turn_id,
        pool,
        user_a,
        partner,
        [uuid4()],
        bot_id=spec.bot_id,
        bot_spec=spec,
        participants_shape=spec.participants_shape,
        primary_topic_slug=spec.primary_topic_slug,
        current_step="record",
    )
    args = AddWatchItemInput(owner_user_id=partner.id, content="watch")
    result = await write_tools.add_watch_item(ctx, args)
    assert result.id is not None
