"""S4 T19 — negative / positive / legacy-None scope tests.

Three scenarios:

(1) Negative — coach-shaped ctx (read_scopes.topics={'career'},
    write_scopes.topics={'career'}, primary_topic_slug='relationship')
    calling get_memories / list_themes / get_oob returns is_error=True
    and error.startswith('scope_denied'); add_memory raises
    ToolCallRejected with 'write_scope_denied' in the error payload.

(2) Positive — mediator-shaped ctx (read_scopes.topics={'own'},
    primary_topic_slug='relationship') succeeds (gate returns None).

(3) Legacy — ctx with read_scopes=None / write_scopes=None: gate is
    None-permissive so get_memories / add_memory both proceed past the
    gate (None-permissive is required to preserve ~15 legacy fixtures).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.bots.base import ReadScopes, WriteScopes
from app.services.tools.read_tools import get_memories, get_oob, list_themes
from app.services.tools.scope_guard import check_read_scope, check_write_scope
from app.services.tools.write_tools import ToolCallRejected, add_memory
from tool_schemas import (
    AddMemoryInput,
    GetMemoriesInput,
    GetOOBInput,
    ListThemesInput,
    MemoryStatus,
)


def _coach_ctx(pool: Any) -> Any:
    """Coach-shaped: career topic, but primary_topic_slug='relationship' to
    exercise the deny path (a relationship-flavored ctx whose scopes only
    permit career)."""
    return SimpleNamespace(
        pool=pool,
        bot_id="coach",
        turn_id=uuid4(),
        user=SimpleNamespace(id=uuid4()),
        partner=SimpleNamespace(id=uuid4()),
        primary_topic_id=uuid4(),
        primary_topic_slug="relationship",
        read_scopes=ReadScopes(topics=frozenset({"career"})),
        write_scopes=WriteScopes(topics=frozenset({"career"})),
    )


def _mediator_ctx_shape() -> Any:
    return SimpleNamespace(
        pool=None,
        bot_id="mediator",
        primary_topic_id=uuid4(),
        primary_topic_slug="relationship",
        read_scopes=ReadScopes(topics=frozenset({"own"})),
        write_scopes=WriteScopes(topics=frozenset({"relationship"})),
    )


def _legacy_ctx() -> Any:
    return SimpleNamespace(
        pool=None,
        bot_id="legacy",
        primary_topic_id=uuid4(),
        primary_topic_slug="relationship",
        read_scopes=None,
        write_scopes=None,
    )


@pytest.mark.asyncio
async def test_negative_coach_read_denied_on_get_memories() -> None:
    ctx = _coach_ctx(pool=None)
    out = await get_memories(ctx, GetMemoriesInput(scope="own", status=MemoryStatus.active))
    assert out.is_error is True
    assert out.error is not None and out.error.startswith("scope_denied")
    assert out.memories == []


@pytest.mark.asyncio
async def test_negative_coach_read_denied_on_list_themes() -> None:
    ctx = _coach_ctx(pool=None)
    out = await list_themes(ctx, ListThemesInput(scope="own"))
    assert out.is_error is True
    assert out.error is not None and out.error.startswith("scope_denied")
    assert out.themes == []


@pytest.mark.asyncio
async def test_negative_coach_read_denied_on_get_oob() -> None:
    ctx = _coach_ctx(pool=None)
    out = await get_oob(ctx, GetOOBInput(scope="own"))
    assert out.is_error is True
    assert out.error is not None and out.error.startswith("scope_denied")
    assert out.entries == []


@pytest.mark.asyncio
async def test_negative_coach_write_denied_on_add_memory() -> None:
    ctx = _coach_ctx(pool=None)
    args = AddMemoryInput(about_user_id=None, content="x")
    with pytest.raises(ToolCallRejected) as exc:
        await add_memory(ctx, args)
    err = exc.value.result.get("error", "")
    assert "write_scope_denied" in err


def test_positive_mediator_read_scope_returns_none() -> None:
    ctx = _mediator_ctx_shape()
    assert check_read_scope(ctx, "own") is None


def test_positive_mediator_write_scope_returns_none() -> None:
    ctx = _mediator_ctx_shape()
    assert check_write_scope(ctx) is None


def test_legacy_none_read_scope_is_permissive() -> None:
    ctx = _legacy_ctx()
    assert check_read_scope(ctx, "own") is None


def test_legacy_none_write_scope_is_permissive() -> None:
    ctx = _legacy_ctx()
    assert check_write_scope(ctx) is None
