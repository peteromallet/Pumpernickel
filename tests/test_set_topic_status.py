"""S4 T18 — set_topic_status tests.

Covers:
* happy paths for scope='user' and scope='dyad' with ctx.bot_id='mediator'
* upsert returns the existing-row id on second call with same key
* Pydantic rejects body > 300 chars
* Pydantic rejects headline > 80 chars
* XOR rejection: scope='user' requires user_id, scope='dyad' requires ctx.dyad_id
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.bots.base import WriteScopes
from app.services.tools.write_tools import ToolCallRejected, set_topic_status
from tool_schemas import SetTopicStatusInput


class _StubPool:
    """Fakes the topic_status upsert + tool_calls insert.

    Stores rows by (topic_id, user_id|None, dyad_id|None) so the upsert behaves
    like the real ON CONFLICT semantics for the partial-unique indexes.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[UUID, UUID | None, UUID | None], dict[str, Any]] = {}
        self.tool_calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any]:
        compact = " ".join(sql.split())
        # set_topic_status user-scope INSERT
        if "INSERT INTO topic_status (topic_id, user_id, headline, body, last_updated_by_bot_id)" in compact:
            topic_id, user_id, headline, body, bot_id = args
            key = (topic_id, user_id, None)
            if key in self.rows:
                row = self.rows[key]
                row.update(headline=headline, body=body, last_updated_at=datetime.now(UTC), last_updated_by_bot_id=bot_id)
            else:
                row = {
                    "id": uuid4(),
                    "topic_id": topic_id,
                    "user_id": user_id,
                    "dyad_id": None,
                    "headline": headline,
                    "body": body,
                    "last_updated_at": datetime.now(UTC),
                    "last_updated_by_bot_id": bot_id,
                }
                self.rows[key] = row
            return row
        if "INSERT INTO topic_status (topic_id, dyad_id, headline, body, last_updated_by_bot_id)" in compact:
            topic_id, dyad_id, headline, body, bot_id = args
            key = (topic_id, None, dyad_id)
            if key in self.rows:
                row = self.rows[key]
                row.update(headline=headline, body=body, last_updated_at=datetime.now(UTC), last_updated_by_bot_id=bot_id)
            else:
                row = {
                    "id": uuid4(),
                    "topic_id": topic_id,
                    "user_id": None,
                    "dyad_id": dyad_id,
                    "headline": headline,
                    "body": body,
                    "last_updated_at": datetime.now(UTC),
                    "last_updated_by_bot_id": bot_id,
                }
                self.rows[key] = row
            return row
        raise AssertionError(f"unexpected fetchrow SQL: {compact[:120]}")

    async def execute(self, sql: str, *args: Any) -> None:
        compact = " ".join(sql.split())
        if compact.startswith("INSERT INTO tool_calls"):
            self.tool_calls.append((compact, args))
            return None
        raise AssertionError(f"unexpected execute SQL: {compact[:120]}")


def _mediator_ctx(pool: _StubPool, *, dyad_id: UUID | None = None) -> Any:
    """Mediator-shaped ctx with write_scopes={'relationship'}."""
    return type("Ctx", (), {
        "pool": pool,
        "bot_id": "mediator",
        "primary_topic_id": uuid4(),
        "primary_topic_slug": "relationship",
        "dyad_id": dyad_id,
        "turn_id": uuid4(),
        "write_scopes": WriteScopes(topics=frozenset({"relationship"}), require_reason_for_cross_topic=True),
    })()


@pytest.mark.asyncio
async def test_set_topic_status_user_scope_happy_path() -> None:
    pool = _StubPool()
    ctx = _mediator_ctx(pool)
    user_id = uuid4()
    out = await set_topic_status(
        ctx, SetTopicStatusInput(scope="user", user_id=user_id, headline="user h", body="user b"),
    )
    assert not out.is_error
    assert out.headline == "user h"
    assert out.body == "user b"
    assert out.status_id is not None
    assert out.updated_at is not None
    assert pool.tool_calls, "expected _log_tool_call to insert tool_calls row"


@pytest.mark.asyncio
async def test_set_topic_status_dyad_scope_happy_path() -> None:
    pool = _StubPool()
    dyad_id = uuid4()
    ctx = _mediator_ctx(pool, dyad_id=dyad_id)
    out = await set_topic_status(
        ctx, SetTopicStatusInput(scope="dyad", headline="dyad h", body="dyad b"),
    )
    assert not out.is_error
    assert out.headline == "dyad h"
    assert out.body == "dyad b"
    assert out.status_id is not None


@pytest.mark.asyncio
async def test_set_topic_status_upsert_on_second_call() -> None:
    pool = _StubPool()
    dyad_id = uuid4()
    ctx = _mediator_ctx(pool, dyad_id=dyad_id)
    first = await set_topic_status(
        ctx, SetTopicStatusInput(scope="dyad", headline="first", body="initial"),
    )
    second = await set_topic_status(
        ctx, SetTopicStatusInput(scope="dyad", headline="second", body="updated"),
    )
    assert first.status_id == second.status_id, "upsert must reuse the row id"
    assert second.headline == "second"
    assert second.body == "updated"


def test_pydantic_rejects_headline_over_80_chars() -> None:
    with pytest.raises(ValidationError):
        SetTopicStatusInput(scope="user", user_id=uuid4(), headline="x" * 81, body="")


def test_pydantic_rejects_body_over_300_chars() -> None:
    with pytest.raises(ValidationError):
        SetTopicStatusInput(scope="dyad", headline="ok", body="x" * 301)


@pytest.mark.asyncio
async def test_xor_user_scope_requires_user_id() -> None:
    pool = _StubPool()
    ctx = _mediator_ctx(pool)
    with pytest.raises(ToolCallRejected):
        await set_topic_status(
            ctx, SetTopicStatusInput(scope="user", user_id=None, headline="h", body="b"),
        )


@pytest.mark.asyncio
async def test_xor_dyad_scope_requires_ctx_dyad_id() -> None:
    pool = _StubPool()
    ctx = _mediator_ctx(pool, dyad_id=None)
    with pytest.raises(ToolCallRejected):
        await set_topic_status(
            ctx, SetTopicStatusInput(scope="dyad", headline="h", body="b"),
        )
