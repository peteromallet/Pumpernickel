"""Focused orientation tool schema and handler tests (T10).

Covers import/registry presence for all seven tool pairs, read/write scope
behavior, single-topic enforcement, cross-topic reason requirements,
invalid enum rejection, and store error propagation.

Tests are local to the orientation tool surface; they mock the store layer
so assertions exercise handler logic without requiring a real database.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.bots.base import ReadScopes, WriteScopes
from app.services.tools.scope_guard import ToolCallRejected as ScopeToolCallRejected


# ── Helpers ────────────────────────────────────────────────────────────────


def _mediator_ctx(
    pool: Any = None,
    *,
    user_id: UUID | None = None,
    topic_id: UUID | None = None,
    topic_slug: str = "relationship",
    read_topics: frozenset[str] | None = None,
    write_topics: frozenset[str] | None = None,
) -> SimpleNamespace:
    """Construct a mediator-shaped TurnContext-like object for handler tests."""
    uid = user_id or uuid4()
    tid = topic_id or uuid4()
    return SimpleNamespace(
        pool=pool,
        bot_id="mediator",
        turn_id=uuid4(),
        user=SimpleNamespace(id=uid),
        partner=SimpleNamespace(id=uuid4()),
        primary_topic_id=tid,
        primary_topic_slug=topic_slug,
        read_scopes=ReadScopes(topics=read_topics
                               if read_topics is not None
                               else frozenset({"own", topic_slug})),
        write_scopes=WriteScopes(topics=write_topics
                                 if write_topics is not None
                                 else frozenset({"own", topic_slug})),
    )


def _mock_pool_with_topic(slug: str = "relationship",
                          topic_id: UUID | None = None) -> MagicMock:
    """Return a mock pool with fetch for topic resolution."""
    pool = MagicMock()
    tid = topic_id or uuid4()
    async def _fetch(_query, *args):
        return [SimpleNamespace(id=tid, slug=slug)]
    pool.fetch = _fetch
    pool.fetchrow = AsyncMock(return_value=None)
    return pool


def _make_topic_row(topic_id: UUID, slug: str) -> MagicMock:
    """Return a MagicMock that supports subscript access for topic resolution."""
    row = MagicMock()
    row.__getitem__ = lambda self, key: {
        "id": topic_id, "slug": slug,
    }[key]
    return row


# ── T10.1: Schema imports — all 14 schemas import cleanly ──────────────────


class TestOrientationSchemaImports:
    """Verify all orientation Input/Output schemas import from tool_schemas."""

    def test_all_input_schemas_importable(self) -> None:
        from tool_schemas import (
            ListOrientationItemsInput,
            GetOrientationItemInput,
            CreateOrientationItemInput,
            UpdateOrientationItemInput,
            ReviewOrientationItemInput,
            CloseOrientationItemInput,
            LinkOrientationEvidenceInput,
        )
        # Instantiate minimal valid payloads to confirm model shapes.
        item_id = uuid4()
        assert ListOrientationItemsInput(scope="own").scope == "own"
        assert GetOrientationItemInput(item_id=item_id).item_id == item_id
        assert CreateOrientationItemInput(
            kind="principle", label="Be honest",
        ).kind.value == "principle"
        assert UpdateOrientationItemInput(item_id=item_id).item_id == item_id
        assert ReviewOrientationItemInput(item_id=item_id, verdict="accepted")
        assert CloseOrientationItemInput(item_id=item_id, new_status="retired")
        assert LinkOrientationEvidenceInput(
            item_id=item_id,
            target_table="commitments",
            target_id=uuid4(),
            relation="evidence",
        )

    def test_all_output_schemas_importable(self) -> None:
        from tool_schemas import (
            ListOrientationItemsOutput,
            GetOrientationItemOutput,
            CreateOrientationItemOutput,
            UpdateOrientationItemOutput,
            ReviewOrientationItemOutput,
            CloseOrientationItemOutput,
            LinkOrientationEvidenceOutput,
        )
        # Light smoke: instantiate with minimal fields.
        assert ListOrientationItemsOutput().is_error is False
        assert GetOrientationItemOutput().item is None
        assert CreateOrientationItemOutput(
            id=uuid4(), kind="principle", status="active",
            source="user_stated", review_state="reviewed", label="x",
        )
        assert UpdateOrientationItemOutput(
            id=uuid4(), kind="goal", status="active",
            review_state="reviewed", label="y",
        )
        assert ReviewOrientationItemOutput(
            id=uuid4(), verdict="accepted", status="active",
        )
        assert CloseOrientationItemOutput(
            id=uuid4(), status="completed",
        )
        assert LinkOrientationEvidenceOutput(
            id=uuid4(), item_id=uuid4(),
            target_table="commitments", target_id=uuid4(),
            relation="evidence", created_at=datetime.now(timezone.utc),
        )

    def test_orientation_item_row_schema(self) -> None:
        from tool_schemas import OrientationItemRow
        row = OrientationItemRow(
            id=uuid4(),
            user_id=uuid4(),
            bot_id="mediator",
            kind="principle",
            status="active",
            source="user_stated",
            review_state="reviewed",
            label="Test",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        assert row.kind.value == "principle"
        assert row.label == "Test"


# ── T10.2: Registry presence — all 7 tool names in all registries ──────────


class TestOrientationRegistryPresence:
    """Verify all seven orientation tools appear in TOOL_REGISTRY, TOOL_DISPATCH,
    TOOL_DESCRIPTIONS, READ_PHASE_TOOLS, WRITE_PHASE_TOOLS, _SELF_LOGGING_TOOLS,
    ARTIFACT_READ_TOOLS, and ARTIFACT_WRITE_TOOLS."""

    ORIENTATION_TOOLS = {
        "list_orientation_items",
        "get_orientation_item",
        "create_orientation_item",
        "update_orientation_item",
        "review_orientation_item",
        "close_orientation_item",
        "link_orientation_evidence",
    }

    ORIENTATION_READ_TOOLS = {
        "list_orientation_items",
        "get_orientation_item",
    }

    ORIENTATION_WRITE_TOOLS = {
        "create_orientation_item",
        "update_orientation_item",
        "review_orientation_item",
        "close_orientation_item",
        "link_orientation_evidence",
    }

    def test_all_in_tool_registry(self) -> None:
        from tool_schemas import TOOL_REGISTRY
        for name in self.ORIENTATION_TOOLS:
            assert name in TOOL_REGISTRY, f"{name} missing from TOOL_REGISTRY"

    def test_all_in_tool_dispatch(self) -> None:
        from app.services.tools.registry import TOOL_DISPATCH
        for name in self.ORIENTATION_TOOLS:
            assert name in TOOL_DISPATCH, f"{name} missing from TOOL_DISPATCH"

    def test_all_in_tool_descriptions(self) -> None:
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        for name in self.ORIENTATION_TOOLS:
            assert name in TOOL_DESCRIPTIONS, f"{name} missing from TOOL_DESCRIPTIONS"

    def test_read_tools_in_read_phase(self) -> None:
        from app.services.tools.registry import READ_PHASE_TOOLS
        for name in self.ORIENTATION_READ_TOOLS:
            assert name in READ_PHASE_TOOLS, (
                f"{name} missing from READ_PHASE_TOOLS"
            )

    def test_write_tools_in_write_phase(self) -> None:
        from app.services.tools.registry import WRITE_PHASE_TOOLS
        for name in self.ORIENTATION_WRITE_TOOLS:
            assert name in WRITE_PHASE_TOOLS, (
                f"{name} missing from WRITE_PHASE_TOOLS"
            )

    def test_write_tools_in_self_logging(self) -> None:
        from app.services.tools.registry import _SELF_LOGGING_TOOLS
        for name in self.ORIENTATION_WRITE_TOOLS:
            assert name in _SELF_LOGGING_TOOLS, (
                f"{name} missing from _SELF_LOGGING_TOOLS"
            )

    def test_list_orientation_items_in_artifact_read_tools(self) -> None:
        from app.services.tools.scope_guard import ARTIFACT_READ_TOOLS
        assert "list_orientation_items" in ARTIFACT_READ_TOOLS

    def test_write_tools_in_artifact_write_tools(self) -> None:
        from app.services.tools.scope_guard import ARTIFACT_WRITE_TOOLS
        for name in self.ORIENTATION_WRITE_TOOLS:
            assert name in ARTIFACT_WRITE_TOOLS, (
                f"{name} missing from ARTIFACT_WRITE_TOOLS"
            )

    def test_tool_descriptions_distinguish_from_other_primitives(self) -> None:
        """Tool descriptions must clearly distinguish orientation from memory,
        observations, distillations, commitments/events, and OOB."""
        from app.services.tools.registry import TOOL_DESCRIPTIONS

        create_desc = TOOL_DESCRIPTIONS["create_orientation_item"]
        # Should mention it's distinct from other knowledge primitives.
        lower = create_desc.lower()
        assert "memories" in lower
        assert "observations" in lower or "learned" in lower
        assert "distillations" in lower or "tentative" in lower
        assert "commitments" in lower or "events" in lower or "tracked" in lower
        assert "oob" in lower or "boundaries" in lower

    def test_list_orientation_items_description_mentions_all_not_allowed(self) -> None:
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        desc = TOOL_DESCRIPTIONS["list_orientation_items"]
        assert "'all' is not allowed" in desc or "all' is not allowed" in desc

    def test_create_description_mentions_bot_proposed_review(self) -> None:
        from app.services.tools.registry import TOOL_DESCRIPTIONS
        desc = TOOL_DESCRIPTIONS["create_orientation_item"]
        assert "bot_proposed" in desc
        assert "review" in desc.lower()

    def test_registry_has_exactly_seven_orientation_tools(self) -> None:
        """Verify exactly seven orientation tools exist in TOOL_DISPATCH."""
        from app.services.tools.registry import TOOL_DISPATCH
        found = [n for n in TOOL_DISPATCH if n.endswith("_orientation_item")
                 or n.endswith("_orientation_evidence")
                 or n == "list_orientation_items"
                 or n == "get_orientation_item"]
        assert len(found) == 7, f"Expected 7 orientation tools, found {len(found)}: {found}"


# ── T10.3: Invalid enum rejection ──────────────────────────────────────────


class TestOrientationEnumRejection:
    """Verify orientation schemas reject invalid enum values at Pydantic level."""

    def test_invalid_kind_rejected(self) -> None:
        from tool_schemas import CreateOrientationItemInput
        with pytest.raises(ValidationError):
            CreateOrientationItemInput(kind="not_a_kind", label="bad")

    def test_invalid_source_rejected(self) -> None:
        from tool_schemas import CreateOrientationItemInput
        with pytest.raises(ValidationError):
            CreateOrientationItemInput(
                kind="principle", label="x", source="invalid_source",
            )

    def test_invalid_status_rejected(self) -> None:
        from tool_schemas import UpdateOrientationItemInput
        with pytest.raises(ValidationError):
            UpdateOrientationItemInput(item_id=uuid4(), status="bogus")

    def test_invalid_review_state_rejected(self) -> None:
        from tool_schemas import UpdateOrientationItemInput
        with pytest.raises(ValidationError):
            UpdateOrientationItemInput(item_id=uuid4(), review_state="bogus")

    def test_invalid_verdict_rejected(self) -> None:
        from tool_schemas import ReviewOrientationItemInput
        with pytest.raises(ValidationError):
            ReviewOrientationItemInput(item_id=uuid4(), verdict="bogus")

    def test_invalid_target_table_rejected(self) -> None:
        from tool_schemas import LinkOrientationEvidenceInput
        with pytest.raises(ValidationError):
            LinkOrientationEvidenceInput(
                item_id=uuid4(),
                target_table="bogus",
                target_id=uuid4(),
                relation="evidence",
            )

    def test_invalid_relation_rejected(self) -> None:
        from tool_schemas import LinkOrientationEvidenceInput
        with pytest.raises(ValidationError):
            LinkOrientationEvidenceInput(
                item_id=uuid4(),
                target_table="commitments",
                target_id=uuid4(),
                relation="bogus",
            )

    def test_close_invalid_new_status_rejected(self) -> None:
        from tool_schemas import CloseOrientationItemInput
        with pytest.raises(ValidationError):
            CloseOrientationItemInput(item_id=uuid4(), new_status="pending")

    def test_list_invalid_scope_rejected(self) -> None:
        from tool_schemas import ListOrientationItemsInput
        with pytest.raises(ValidationError):
            ListOrientationItemsInput(scope="bogus_value")

    def test_priority_without_rank_rejected(self) -> None:
        from tool_schemas import CreateOrientationItemInput
        with pytest.raises(ValidationError, match="priority items require a priority_rank"):
            CreateOrientationItemInput(kind="priority", label="Prio without rank")

    def test_principle_with_priority_rank_rejected(self) -> None:
        from tool_schemas import CreateOrientationItemInput
        with pytest.raises(
            ValidationError,
            match="principles and anti-patterns must not set priority_rank",
        ):
            CreateOrientationItemInput(
                kind="principle", label="P", priority_rank=5,
            )

    def test_close_completed_without_completed_at_rejected(self) -> None:
        from tool_schemas import CloseOrientationItemInput
        with pytest.raises(
            ValidationError,
            match="completed_at is required when closing with status 'completed'",
        ):
            CloseOrientationItemInput(item_id=uuid4(), new_status="completed")

    def test_close_with_completed_at_but_not_completed_rejected(self) -> None:
        from tool_schemas import CloseOrientationItemInput
        with pytest.raises(
            ValidationError,
            match="completed_at must not be set unless new_status is 'completed'",
        ):
            CloseOrientationItemInput(
                item_id=uuid4(),
                new_status="retired",
                completed_at=datetime.now(timezone.utc),
            )

    def test_orientation_kind_enum_values_match_storage(self) -> None:
        from tool_schemas import OrientationKind
        from app.services.user_orientation import VALID_KINDS
        schema_values = {item.value for item in OrientationKind}
        assert schema_values == set(VALID_KINDS)

    def test_orientation_status_enum_values_match_storage(self) -> None:
        from tool_schemas import OrientationStatus
        from app.services.user_orientation import VALID_STATUSES
        schema_values = {item.value for item in OrientationStatus}
        assert schema_values == set(VALID_STATUSES)

    def test_orientation_source_enum_values_match_storage(self) -> None:
        from tool_schemas import OrientationSource
        from app.services.user_orientation import VALID_SOURCES
        schema_values = {item.value for item in OrientationSource}
        assert schema_values == set(VALID_SOURCES)

    def test_orientation_review_state_enum_values_match_storage(self) -> None:
        from tool_schemas import OrientationReviewState
        from app.services.user_orientation import VALID_REVIEW_STATES
        schema_values = {item.value for item in OrientationReviewState}
        assert schema_values == set(VALID_REVIEW_STATES)

    def test_orientation_verdict_enum_values_match_storage(self) -> None:
        from tool_schemas import OrientationVerdict
        from app.services.user_orientation import VALID_VERDICTS
        schema_values = {item.value for item in OrientationVerdict}
        assert schema_values == set(VALID_VERDICTS)

    def test_orientation_target_table_enum_values_match_storage(self) -> None:
        from tool_schemas import OrientationTargetTable
        from app.services.user_orientation import VALID_TARGET_TABLES
        schema_values = {item.value for item in OrientationTargetTable}
        assert schema_values == set(VALID_TARGET_TABLES)

    def test_orientation_relation_enum_values_match_storage(self) -> None:
        from tool_schemas import OrientationRelation
        from app.services.user_orientation import VALID_RELATIONS
        schema_values = {item.value for item in OrientationRelation}
        assert schema_values == set(VALID_RELATIONS)


# ── T10.4: Read scope behavior ─────────────────────────────────────────────


class TestOrientationReadScopeBehavior:
    """Verify orientation read handlers enforce read scope and reject 'all'."""

    @pytest.mark.asyncio
    async def test_list_orientation_items_rejects_all_scope(self) -> None:
        """list_orientation_items with scope='all' returns is_error=True."""
        from app.services.tools.read_tools import list_orientation_items
        from tool_schemas import ListOrientationItemsInput

        ctx = _mediator_ctx()
        args = ListOrientationItemsInput(scope="all")
        result = await list_orientation_items(ctx, args)
        assert result.is_error is True
        assert result.error is not None
        assert "all" in result.error.lower()
        assert result.items == []

    @pytest.mark.asyncio
    async def test_list_orientation_items_own_scope_resolves(self) -> None:
        """scope='own' with valid primary_topic_id passes through."""
        from app.services.tools.read_tools import (
            _resolve_orientation_topic_ids,
        )
        ctx = _mediator_ctx()
        result = _resolve_orientation_topic_ids(ctx, "own")
        assert result == [ctx.primary_topic_id]

    @pytest.mark.asyncio
    async def test_resolve_rejects_own_without_primary_topic(self) -> None:
        """scope='own' with primary_topic_id=None raises ToolCallRejected."""
        from app.services.tools.read_tools import (
            _resolve_orientation_topic_ids,
        )
        from app.services.tools.write_tools import ToolCallRejected

        ctx = _mediator_ctx()
        ctx.primary_topic_id = None
        with pytest.raises(ToolCallRejected) as exc:
            _resolve_orientation_topic_ids(ctx, "own")
        err = exc.value.result
        assert err.get("error_code") == "scope_denied"
        assert "primary_topic_id is None" in err.get("reason", "")

    @pytest.mark.asyncio
    async def test_resolve_rejects_invalid_uuid_scope(self) -> None:
        """Non-UUID scope string that is not 'own'/'all' raises ToolCallRejected."""
        from app.services.tools.read_tools import (
            _resolve_orientation_topic_ids,
        )
        from app.services.tools.write_tools import ToolCallRejected

        ctx = _mediator_ctx()
        with pytest.raises(ToolCallRejected) as exc:
            _resolve_orientation_topic_ids(ctx, "not-a-uuid")
        err = exc.value.result
        assert err.get("error_code") == "invalid_topic_id"

    @pytest.mark.asyncio
    async def test_resolve_accepts_explicit_uuid_scope(self) -> None:
        """Explicit UUID scope string is accepted."""
        from app.services.tools.read_tools import (
            _resolve_orientation_topic_ids,
        )
        ctx = _mediator_ctx()
        explicit = uuid4()
        result = _resolve_orientation_topic_ids(ctx, str(explicit))
        assert result == [explicit]

    @pytest.mark.asyncio
    async def test_get_orientation_item_not_found(self) -> None:
        """get_orientation_item returns None item when item not found."""
        from app.services.tools.read_tools import get_orientation_item
        from tool_schemas import GetOrientationItemInput

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)
        ctx = _mediator_ctx(pool=pool)
        args = GetOrientationItemInput(item_id=uuid4())
        result = await get_orientation_item(ctx, args)
        assert result.is_error is False
        assert result.item is None


# ── T10.5: Write scope and cross-topic behavior ────────────────────────────


class TestOrientationWriteScopeAndCrossTopic:
    """Verify orientation write handlers enforce write scope and cross-topic
    reason requirements."""

    @pytest.mark.asyncio
    async def test_create_enforces_write_scope(self) -> None:
        """create_orientation_item raises ToolCallRejected when write scope denied."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            create_orientation_item,
        )
        from tool_schemas import CreateOrientationItemInput

        # Coach-shaped: write_scopes only allow 'career' but primary is 'relationship'.
        ctx = _mediator_ctx(
            write_topics=frozenset({"career"}),
        )
        args = CreateOrientationItemInput(kind="principle", label="Be honest")
        with pytest.raises(ToolCallRejected) as exc:
            await create_orientation_item(ctx, args)
        assert "write_scope_denied" in exc.value.result.get("error", "")

    @pytest.mark.asyncio
    async def test_update_enforces_write_scope(self) -> None:
        """update_orientation_item raises ToolCallRejected when write scope denied."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            update_orientation_item,
        )
        from tool_schemas import UpdateOrientationItemInput

        ctx = _mediator_ctx(
            write_topics=frozenset({"career"}),
        )
        args = UpdateOrientationItemInput(item_id=uuid4(), label="Changed")
        with pytest.raises(ToolCallRejected) as exc:
            await update_orientation_item(ctx, args)
        assert "write_scope_denied" in exc.value.result.get("error", "")

    @pytest.mark.asyncio
    async def test_review_enforces_write_scope(self) -> None:
        from app.services.tools.write_tools import (
            ToolCallRejected,
            review_orientation_item,
        )
        from tool_schemas import ReviewOrientationItemInput

        ctx = _mediator_ctx(
            write_topics=frozenset({"career"}),
        )
        args = ReviewOrientationItemInput(item_id=uuid4(), verdict="accepted")
        with pytest.raises(ToolCallRejected) as exc:
            await review_orientation_item(ctx, args)
        assert "write_scope_denied" in exc.value.result.get("error", "")

    @pytest.mark.asyncio
    async def test_close_enforces_write_scope(self) -> None:
        from app.services.tools.write_tools import (
            ToolCallRejected,
            close_orientation_item,
        )
        from tool_schemas import CloseOrientationItemInput

        ctx = _mediator_ctx(
            write_topics=frozenset({"career"}),
        )
        args = CloseOrientationItemInput(item_id=uuid4(), new_status="retired")
        with pytest.raises(ToolCallRejected) as exc:
            await close_orientation_item(ctx, args)
        assert "write_scope_denied" in exc.value.result.get("error", "")

    @pytest.mark.asyncio
    async def test_link_enforces_write_scope(self) -> None:
        from app.services.tools.write_tools import (
            ToolCallRejected,
            link_orientation_evidence,
        )
        from tool_schemas import LinkOrientationEvidenceInput

        ctx = _mediator_ctx(
            write_topics=frozenset({"career"}),
        )
        args = LinkOrientationEvidenceInput(
            item_id=uuid4(),
            target_table="commitments",
            target_id=uuid4(),
            relation="evidence",
        )
        with pytest.raises(ToolCallRejected) as exc:
            await link_orientation_evidence(ctx, args)
        assert "write_scope_denied" in exc.value.result.get("error", "")

    @pytest.mark.asyncio
    async def test_update_cross_topic_requires_reason(self) -> None:
        """When the item's topic differs from primary, a non-empty reason is required."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            update_orientation_item,
        )
        from tool_schemas import UpdateOrientationItemInput

        other_topic_id = uuid4()
        user_id = uuid4()
        item_id = uuid4()

        now = datetime.now(timezone.utc)

        # Mock store to return an item on a different topic.
        pool = MagicMock()

        def _make_item_dict(**overrides: Any) -> dict[str, Any]:
            return {
                "id": item_id, "user_id": user_id,
                "topic_id": other_topic_id, "bot_id": "mediator",
                "created_by_turn_id": None,
                "kind": "principle", "status": "active",
                "source": "user_stated", "review_state": "reviewed",
                "label": "Old label", "detail": None,
                "started_at": None, "effective_at": None,
                "target_date": None, "completed_at": None,
                "closed_reason": None, "outcome_note": None,
                "supersedes_item_id": None, "priority_rank": None,
                "created_at": now, "updated_at": now,
                **overrides,
            }

        call_count = [0]

        async def _fetchrow(_query, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_item_dict()
            return None  # update_item returns None (will trigger error)

        pool.fetchrow = _fetchrow
        pool.fetch = AsyncMock(return_value=[])
        pool.execute = AsyncMock()

        ctx = _mediator_ctx(pool=pool, user_id=user_id,
                            topic_slug="relationship",
                            write_topics=frozenset({"own", "relationship", "other"}))

        # No reason → should be rejected for cross-topic.
        args_no_reason = UpdateOrientationItemInput(
            item_id=item_id,
            label="Updated",
            reason=None,
        )
        with pytest.raises((ToolCallRejected, ScopeToolCallRejected)) as exc:
            await update_orientation_item(ctx, args_no_reason)
        err_str = str(exc.value)
        assert "cross_topic" in err_str.lower() or "cross topic" in err_str.lower()

    @pytest.mark.asyncio
    async def test_update_cross_topic_with_reason_allowed(self) -> None:
        """Cross-topic update with a non-empty reason proceeds past the gate."""
        from app.services.tools.write_tools import update_orientation_item
        from tool_schemas import UpdateOrientationItemInput

        other_topic_id = uuid4()
        item_id = uuid4()
        user_id = uuid4()
        now = datetime.now(timezone.utc)

        # Mock store to return an item on a different topic, then succeed update.
        pool = MagicMock()
        call_count = [0]

        def _make_item(label: str) -> dict[str, Any]:
            return {
                "id": item_id, "user_id": user_id,
                "topic_id": other_topic_id, "bot_id": "mediator",
                "created_by_turn_id": None,
                "kind": "principle", "status": "active",
                "source": "user_stated", "review_state": "reviewed",
                "label": label, "detail": None,
                "started_at": None, "effective_at": None,
                "target_date": None, "completed_at": None,
                "closed_reason": None, "outcome_note": None,
                "supersedes_item_id": None, "priority_rank": None,
                "created_at": now, "updated_at": now,
            }

        async def _fetchrow(_query, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_item("Old label")
            return _make_item("Updated label")

        pool.fetchrow = _fetchrow
        pool.fetch = AsyncMock(return_value=[])
        pool.execute = AsyncMock()

        ctx = _mediator_ctx(pool=pool, user_id=user_id,
                            topic_slug="relationship",
                            write_topics=frozenset({"own", "relationship", "other"}))

        args = UpdateOrientationItemInput(
            item_id=item_id,
            label="Updated",
            reason="Updating from a different topic for valid reasons",
        )
        result = await update_orientation_item(ctx, args)
        assert result.action == "updated"
        assert result.label == "Updated label"


# ── T10.6: Single-topic enforcement ────────────────────────────────────────


class TestOrientationSingleTopicEnforcement:
    """Verify create_orientation_item uses single topic_id (first resolved slug)."""

    @pytest.mark.asyncio
    async def test_create_orientation_item_uses_single_topic(self) -> None:
        """create_orientation_item resolves exactly one topic_id and passes it to store."""
        from app.services.tools.write_tools import create_orientation_item
        from tool_schemas import CreateOrientationItemInput

        topic_id = uuid4()
        user_id = uuid4()
        item_id = uuid4()
        now = datetime.now(timezone.utc)

        pool = MagicMock()
        # Topic resolution
        async def _fetch(_query, *args):
            row = MagicMock()
            row.__getitem__ = lambda self, key: {"slug": "relationship", "id": topic_id}[key]
            return [row]
        pool.fetch = _fetch

        # Store create_item returns successful item.
        async def _fetchrow(_query, *args):
            return {
                "id": item_id,
                "user_id": user_id,
                "topic_id": topic_id,
                "bot_id": "mediator",
                "created_by_turn_id": None,
                "kind": "principle",
                "status": "active",
                "source": "user_stated",
                "review_state": "reviewed",
                "label": "Be honest",
                "detail": None,
                "started_at": None,
                "effective_at": None,
                "target_date": None,
                "completed_at": None,
                "closed_reason": None,
                "outcome_note": None,
                "supersedes_item_id": None,
                "priority_rank": None,
                "created_at": now,
                "updated_at": now,
            }
        pool.fetchrow = _fetchrow
        pool.execute = AsyncMock()

        ctx = _mediator_ctx(pool=pool, user_id=user_id)
        args = CreateOrientationItemInput(kind="principle", label="Be honest")
        result = await create_orientation_item(ctx, args)
        assert result.action == "created"
        assert result.kind == "principle"
        assert result.label == "Be honest"


# ── T10.7: Store error propagation ─────────────────────────────────────────


class TestOrientationStoreErrorPropagation:
    """Verify handlers propagate store errors correctly."""

    @pytest.mark.asyncio
    async def test_update_item_not_found(self) -> None:
        """update_orientation_item raises ToolCallRejected when item not found."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            update_orientation_item,
        )
        from tool_schemas import UpdateOrientationItemInput

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)  # Item not found
        ctx = _mediator_ctx(pool=pool)
        args = UpdateOrientationItemInput(item_id=uuid4(), label="Changed")
        with pytest.raises(ToolCallRejected) as exc:
            await update_orientation_item(ctx, args)
        assert "not found" in exc.value.result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_review_item_not_found(self) -> None:
        """review_orientation_item raises ToolCallRejected when item not found."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            review_orientation_item,
        )
        from tool_schemas import ReviewOrientationItemInput

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)
        ctx = _mediator_ctx(pool=pool)
        args = ReviewOrientationItemInput(item_id=uuid4(), verdict="accepted")
        with pytest.raises(ToolCallRejected) as exc:
            await review_orientation_item(ctx, args)
        assert "not found" in exc.value.result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_close_item_not_found(self) -> None:
        """close_orientation_item raises ToolCallRejected when item not found."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            close_orientation_item,
        )
        from tool_schemas import CloseOrientationItemInput

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)
        ctx = _mediator_ctx(pool=pool)
        args = CloseOrientationItemInput(item_id=uuid4(), new_status="retired")
        with pytest.raises(ToolCallRejected) as exc:
            await close_orientation_item(ctx, args)
        assert "not found" in exc.value.result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_link_evidence_item_not_found(self) -> None:
        """link_orientation_evidence raises ToolCallRejected when item not found."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            link_orientation_evidence,
        )
        from tool_schemas import LinkOrientationEvidenceInput

        pool = MagicMock()
        pool.fetchrow = AsyncMock(return_value=None)
        ctx = _mediator_ctx(pool=pool)
        args = LinkOrientationEvidenceInput(
            item_id=uuid4(),
            target_table="commitments",
            target_id=uuid4(),
            relation="evidence",
        )
        with pytest.raises(ToolCallRejected) as exc:
            await link_orientation_evidence(ctx, args)
        assert "not found" in exc.value.result.get("error", "").lower()

    @pytest.mark.asyncio
    async def test_create_with_missing_topic_slugs_raises(self) -> None:
        """create_orientation_item with a non-existent topic slug raises."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            create_orientation_item,
        )
        from tool_schemas import CreateOrientationItemInput

        pool = MagicMock()
        # Topic resolution returns empty (slug not found)
        pool.fetch = AsyncMock(return_value=[])
        ctx = _mediator_ctx(pool=pool, topic_slug="nonexistent")
        args = CreateOrientationItemInput(
            kind="principle",
            label="Test",
            topic_slugs=["nonexistent"],
        )
        with pytest.raises((ToolCallRejected, ScopeToolCallRejected)):
            await create_orientation_item(ctx, args)

    @pytest.mark.asyncio
    async def test_update_store_returns_none_after_update(self) -> None:
        """If store.update_item returns None, handler raises ToolCallRejected."""
        from app.services.tools.write_tools import (
            ToolCallRejected,
            update_orientation_item,
        )
        from tool_schemas import UpdateOrientationItemInput

        item_id = uuid4()
        user_id = uuid4()
        now = datetime.now(timezone.utc)

        pool = MagicMock()
        call_count = [0]

        async def _fetchrow(_query, *args):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "id": item_id, "user_id": user_id,
                    "topic_id": uuid4(), "bot_id": "mediator",
                    "created_by_turn_id": None,
                    "kind": "principle", "status": "active",
                    "source": "user_stated", "review_state": "reviewed",
                    "label": "Old", "detail": None,
                    "started_at": None, "effective_at": None,
                    "target_date": None, "completed_at": None,
                    "closed_reason": None, "outcome_note": None,
                    "supersedes_item_id": None, "priority_rank": None,
                    "created_at": now, "updated_at": now,
                }
            return None

        pool.fetchrow = _fetchrow
        ctx = _mediator_ctx(pool=pool, user_id=user_id)
        args = UpdateOrientationItemInput(item_id=item_id, label="Updated")
        with pytest.raises((ToolCallRejected, ScopeToolCallRejected)) as exc:
            await update_orientation_item(ctx, args)
        err_str = str(exc.value)
        assert "failed" in err_str.lower() or "cross_topic" in err_str.lower()

    @pytest.mark.asyncio
    async def test_create_positive_write_scope_passes(self) -> None:
        """create_orientation_item with matching write scope succeeds."""
        from app.services.tools.write_tools import create_orientation_item
        from tool_schemas import CreateOrientationItemInput

        topic_id = uuid4()
        user_id = uuid4()
        item_id = uuid4()
        now = datetime.now(timezone.utc)

        pool = MagicMock()
        # Topic resolution
        pool.fetch = AsyncMock(return_value=[
            _make_topic_row(topic_id, "relationship"),
        ])
        # Store create_item returns successful item.
        pool.fetchrow = AsyncMock(return_value={
            "id": item_id, "user_id": user_id,
            "topic_id": topic_id, "bot_id": "mediator",
            "created_by_turn_id": None,
            "kind": "principle", "status": "active",
            "source": "user_stated", "review_state": "reviewed",
            "label": "Be honest", "detail": None,
            "started_at": None, "effective_at": None,
            "target_date": None, "completed_at": None,
            "closed_reason": None, "outcome_note": None,
            "supersedes_item_id": None, "priority_rank": None,
            "created_at": now, "updated_at": now,
        })
        pool.execute = AsyncMock()

        ctx = _mediator_ctx(pool=pool, user_id=user_id,
                            write_topics=frozenset({"own", "relationship"}))
        args = CreateOrientationItemInput(kind="principle", label="Be honest")
        result = await create_orientation_item(ctx, args)
        assert result.action == "created"
        assert result.label == "Be honest"


# ── T10.8: Scope guard unit tests for orientation tools ────────────────────


class TestOrientationScopeGuard:
    """Direct unit tests on scope guard functions as they apply to orientation."""

    def test_check_read_scope_allows_own_when_in_topics(self) -> None:
        from app.services.tools.scope_guard import check_read_scope
        ctx = _mediator_ctx(read_topics=frozenset({"own"}))
        assert check_read_scope(ctx, "own") is None

    def test_check_read_scope_denies_when_slug_not_in_topics(self) -> None:
        from app.services.tools.scope_guard import check_read_scope
        ctx = _mediator_ctx(read_topics=frozenset({"career"}),
                            topic_slug="relationship")
        err = check_read_scope(ctx, "own")
        assert err is not None
        assert "scope_denied" in err

    def test_check_write_scope_allows_own_when_in_topics(self) -> None:
        from app.services.tools.scope_guard import check_write_scope
        ctx = _mediator_ctx(write_topics=frozenset({"own"}))
        assert check_write_scope(ctx) is None

    def test_check_write_scope_allows_explicit_slug(self) -> None:
        from app.services.tools.scope_guard import check_write_scope
        ctx = _mediator_ctx(write_topics=frozenset({"relationship"}))
        assert check_write_scope(ctx) is None

    def test_check_write_scope_none_permissive(self) -> None:
        from app.services.tools.scope_guard import check_write_scope
        ctx = _mediator_ctx()
        ctx.write_scopes = None
        assert check_write_scope(ctx) is None

    def test_resolve_write_topic_slugs_defaults_to_primary(self) -> None:
        from app.services.tools.scope_guard import resolve_write_topic_slugs
        ctx = _mediator_ctx()
        slugs = resolve_write_topic_slugs(ctx, None)
        assert slugs == [ctx.primary_topic_slug]

    def test_resolve_write_topic_slugs_deduplicates_own(self) -> None:
        from app.services.tools.scope_guard import resolve_write_topic_slugs
        ctx = _mediator_ctx(topic_slug="relationship",
                            write_topics=frozenset({"own", "relationship"}))
        slugs = resolve_write_topic_slugs(ctx, ["own", "relationship"])
        assert slugs == ["relationship"]

    def test_require_reason_for_cross_topic_raises_when_no_reason(self) -> None:
        from app.services.tools.scope_guard import (
            ToolCallRejected,
            require_reason_for_cross_topic,
        )
        with pytest.raises(ToolCallRejected):
            require_reason_for_cross_topic(
                ["other_topic"], "primary_topic", None,
            )

    def test_require_reason_for_cross_topic_passes_with_reason(self) -> None:
        from app.services.tools.scope_guard import require_reason_for_cross_topic
        # Should not raise
        require_reason_for_cross_topic(
            ["other_topic"], "primary_topic", "updating cross-topic for valid reason",
        )

    def test_require_reason_for_cross_topic_same_topic_no_reason_needed(self) -> None:
        from app.services.tools.scope_guard import require_reason_for_cross_topic
        # Should not raise — same topic
        require_reason_for_cross_topic(
            ["primary_topic"], "primary_topic", None,
        )
