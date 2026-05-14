"""Multi-topic write tests (S6 T5).

Covers all stop-condition scenarios:
1. Default single-topic path → 1 artifact_topics row, primary slug, reason NULL.
2. Out-of-scope slug raises ToolCallRejected (not silent drop).
3. Cross-topic without reason raises ToolCallRejected.
4. Synthetic cross-topic with reason writes 2 rows with supplied reason.
5. Coach cannot escalate via topic_slugs=['career','relationship'].
6. create_bridge_candidate unchanged.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.models.user import User
from app.services.tools.scope_guard import ToolCallRejected as ScopeToolCallRejected
from app.services.tools.write_tools import (
    ToolCallRejected,
    add_memory,
)
from app.services.turn_context import TurnContext
from tests.conftest import FakePool


def _dummy_renderer(**kwargs: object) -> str:
    return "dummy system prompt"


_DUMMY_STEP_INSTRUCTIONS = {
    "read": "read step",
    "consult": "consult step",
    "respond": "respond step",
    "record": "record step",
    "schedule": "schedule step",
    "done": "done",
}


def _make_ctx(
    pool: FakePool,
    *,
    bot_id: str = "mediator",
    topic_slug: str = "relationship",
    write_scopes: WriteScopes | None = None,
) -> TurnContext:
    """Build a minimal TurnContext for tool calls with configurable scopes."""
    user_id = uuid4()
    tid = uuid4()

    # Ensure topic exists in FakePool
    pool.topics.setdefault(
        topic_slug, {"id": tid, "slug": topic_slug, "display_name": topic_slug.title()}
    )

    user = User(
        id=user_id,
        name="testuser",
        phone="+155****4567",
        timezone="America/New_York",
        onboarding_state="welcomed",
    )
    partner = User(
        id=uuid4(),
        name="partner",
        phone="+155****6543",
        timezone="America/New_York",
        onboarding_state="welcomed",
    )

    ctx = TurnContext(
        pool=pool,
        turn_id=uuid4(),
        user=user,
        partner=partner,
        triggering_message_ids=[],
        bot_id=bot_id,
        primary_topic_id=tid,
        primary_topic_slug=topic_slug,
        binding_id=uuid4(),
        write_scopes=write_scopes,
        read_scopes=ReadScopes(topics={"all"}),
        bot_spec=BotSpec(
            bot_id=bot_id,
            prompt_renderer=_dummy_renderer,
            step_instructions=_DUMMY_STEP_INSTRUCTIONS,
            display_name=bot_id,
            participants_shape="dyad",
            primary_topic_slug=topic_slug,
            read_scopes=ReadScopes(topics={"all"}),
            write_scopes=write_scopes or WriteScopes(topics={"all"}),
        ),
        trigger_metadata={"kind": "inbound"},
    )
    return ctx


# ── Test 1: Default single-topic path ────────────────────────────────────


@pytest.mark.asyncio
async def test_default_single_topic_produces_one_row_reason_null() -> None:
    """add_memory with no topic_slugs → 1 artifact_topics row, reason NULL."""
    pool = FakePool()
    career_id = uuid4()
    pool.topics["career"] = {
        "id": career_id,
        "slug": "career",
        "display_name": "Career",
    }
    ctx = _make_ctx(pool, bot_id="coach", topic_slug="career")

    from tool_schemas import AddMemoryInput

    args = AddMemoryInput(
        about_user_id=ctx.user.id, content="Test default single-topic"
    )
    pool.artifact_topics_rows.clear()

    result = await add_memory(ctx, args)
    assert result is not None
    assert result.id is not None

    # Verify exactly 1 artifact_topics row
    rows = pool.artifact_topics_rows
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0]["artifact_table"] == "memories"
    assert rows[0]["artifact_id"] == result.id
    assert rows[0]["topic_id"] == career_id
    assert rows[0]["reason"] is None


# ── Test 2: Out-of-scope slug raises ──────────────────────────────────────


@pytest.mark.asyncio
async def test_out_of_scope_slug_raises_not_silent_drop() -> None:
    """Coach with WriteScopes(topics={'career'}) calling add_memory with
    topic_slugs=['relationship'] → ToolCallRejected."""
    pool = FakePool()
    career_id = uuid4()
    relationship_id = uuid4()
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

    coach_scopes = WriteScopes(topics={"career"})
    ctx = _make_ctx(
        pool, bot_id="coach", topic_slug="career", write_scopes=coach_scopes
    )
    # Fix ctx.bot_spec.write_scopes too, so _assert_solo_about_user consistency
    ctx.bot_spec = BotSpec(
        bot_id="coach",
        prompt_renderer=_dummy_renderer,
        step_instructions=_DUMMY_STEP_INSTRUCTIONS,
        display_name="coach",
        participants_shape="dyad",
        primary_topic_slug="career",
        read_scopes=ReadScopes(topics={"all"}),
        write_scopes=coach_scopes,
    )

    from tool_schemas import AddMemoryInput

    args = AddMemoryInput(
        about_user_id=ctx.user.id,
        content="Should fail: relationship out of scope",
        topic_slugs=["relationship"],
    )

    with pytest.raises((ToolCallRejected, ScopeToolCallRejected)) as exc_info:
        await add_memory(ctx, args)

    err = str(exc_info.value)
    assert (
        "scope_denied" in err.lower()
        or "write_scope_denied" in err.lower()
        or "not in" in err.lower()
    ), f"expected scope-denied error, got: {err}"


# ── Test 3: Cross-topic without reason raises ─────────────────────────────


@pytest.mark.asyncio
async def test_cross_topic_without_reason_raises() -> None:
    """Synthetic WriteScopes(topics={'all'}), topic_slugs=['career','relationship'],
    reason=None → raises ToolCallRejected."""
    pool = FakePool()
    career_id = uuid4()
    relationship_id = uuid4()
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

    all_scopes = WriteScopes(topics={"all"})
    ctx = _make_ctx(
        pool, bot_id="mediator", topic_slug="relationship", write_scopes=all_scopes
    )
    ctx.bot_spec = BotSpec(
        bot_id="mediator",
        prompt_renderer=_dummy_renderer,
        step_instructions=_DUMMY_STEP_INSTRUCTIONS,
        display_name="mediator",
        participants_shape="dyad",
        primary_topic_slug="relationship",
        read_scopes=ReadScopes(topics={"all"}),
        write_scopes=all_scopes,
    )

    from tool_schemas import AddMemoryInput

    args = AddMemoryInput(
        about_user_id=ctx.user.id,
        content="Should fail: cross-topic without reason",
        topic_slugs=["career", "relationship"],
        reason=None,
    )

    with pytest.raises((ToolCallRejected, ScopeToolCallRejected)) as exc_info:
        await add_memory(ctx, args)

    err = str(exc_info.value)
    assert "reason" in err.lower(), f"expected reason-required error, got: {err}"


# ── Test 4: Synthetic cross-topic success ─────────────────────────────────


@pytest.mark.asyncio
async def test_cross_topic_success_writes_two_rows_with_reason() -> None:
    """WriteScopes(topics={'all'}), topic_slugs=['career','relationship'],
    reason='linking shared user' → succeeds, 2 artifact_topics rows."""
    pool = FakePool()
    career_id = uuid4()
    relationship_id = uuid4()
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

    all_scopes = WriteScopes(topics={"all"})
    ctx = _make_ctx(
        pool, bot_id="mediator", topic_slug="relationship", write_scopes=all_scopes
    )
    ctx.bot_spec = BotSpec(
        bot_id="mediator",
        prompt_renderer=_dummy_renderer,
        step_instructions=_DUMMY_STEP_INSTRUCTIONS,
        display_name="mediator",
        participants_shape="dyad",
        primary_topic_slug="relationship",
        read_scopes=ReadScopes(topics={"all"}),
        write_scopes=all_scopes,
    )

    from tool_schemas import AddMemoryInput

    args = AddMemoryInput(
        about_user_id=ctx.user.id,
        content="Cross-topic memory with reason",
        topic_slugs=["career", "relationship"],
        reason="linking shared user",
    )
    pool.artifact_topics_rows.clear()

    result = await add_memory(ctx, args)
    assert result is not None
    assert result.id is not None

    rows = pool.artifact_topics_rows
    assert len(rows) == 2, f"expected 2 rows, got {len(rows)}"

    topic_ids = {row["topic_id"] for row in rows}
    assert topic_ids == {career_id, relationship_id}

    for row in rows:
        assert row["artifact_table"] == "memories"
        assert row["artifact_id"] == result.id
        assert row["reason"] == "linking shared user"


# ── Test 5: Coach cannot escalate ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_coach_cannot_escalate_via_topic_slugs() -> None:
    """Coach WriteScopes(topics={'career'}) trying
    topic_slugs=['career','relationship'] → raises ToolCallRejected."""
    pool = FakePool()
    career_id = uuid4()
    relationship_id = uuid4()
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

    coach_scopes = WriteScopes(topics={"career"})
    ctx = _make_ctx(
        pool, bot_id="coach", topic_slug="career", write_scopes=coach_scopes
    )
    ctx.bot_spec = BotSpec(
        bot_id="coach",
        prompt_renderer=_dummy_renderer,
        step_instructions=_DUMMY_STEP_INSTRUCTIONS,
        display_name="coach",
        participants_shape="dyad",
        primary_topic_slug="career",
        read_scopes=ReadScopes(topics={"all"}),
        write_scopes=coach_scopes,
    )

    from tool_schemas import AddMemoryInput

    args = AddMemoryInput(
        about_user_id=ctx.user.id,
        content="Should fail: coach escalation attempt",
        topic_slugs=["career", "relationship"],
        reason="trying to escalate",
    )

    with pytest.raises((ToolCallRejected, ScopeToolCallRejected)) as exc_info:
        await add_memory(ctx, args)

    err = str(exc_info.value)
    assert (
        "scope_denied" in err.lower()
        or "write_scope_denied" in err.lower()
        or "not in" in err.lower()
    ), f"expected scope-denied error, got: {err}"


# ── Test 6: create_bridge_candidate unchanged ─────────────────────────────


def test_create_bridge_candidate_model_unchanged() -> None:
    """create_bridge_candidate Input model has NO topic_slugs or reason fields
    — bridges store topic_id directly, no artifact_topics linkage."""
    from tool_schemas import CreateBridgeCandidateInput

    # Verify the model fields
    fields = CreateBridgeCandidateInput.model_fields
    assert (
        "topic_slugs" not in fields
    ), "CreateBridgeCandidateInput must not have topic_slugs (single topic_id UUID column)"
    assert (
        "reason" not in fields
    ), "CreateBridgeCandidateInput must not have reason (no artifact_topics linkage)"

    # Verify required fields still present
    assert "source_user_id" in fields
    assert "target_user_id" in fields
    assert "kind" in fields
    assert "shareable_summary" in fields
