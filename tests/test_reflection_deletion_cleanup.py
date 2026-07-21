"""Focused regression tests for reflection deletion / retention cleanup."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from app.routers.admin import (
    distillations as admin_distillations,
    memories as admin_memories,
    observations as admin_observations,
)
from app.services.deletion import (
    cleanup_deleted_reflection_state,
    purge_expired_deletions,
)
from app.services.hot_context_solo import _fetch_reflections_digest
from app.services.reflections_normalization_bridge import _fetch_message_texts
from app.services.reflections import ReflectionStore, admin_list_sessions
from app.services.user_orientation import is_compass_visible


class _FakePool:
    def __init__(
        self,
        *,
        fetch_results: list[object] | None = None,
        fetchrow_result: object = None,
        execute_result: str = "OK",
    ) -> None:
        self.fetch_results = list(fetch_results or [])
        self.fetchrow_result = fetchrow_result
        self.execute_result = execute_result
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []
        self.fetchrow_calls: list[tuple[str, tuple[object, ...]]] = []
        self.execute_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        if self.fetch_results:
            return self.fetch_results.pop(0)
        return []

    async def fetchrow(self, sql: str, *args):
        self.fetchrow_calls.append((sql, args))
        return self.fetchrow_result

    async def execute(self, sql: str, *args):
        self.execute_calls.append((sql, args))
        return self.execute_result


class _FetchOnlyPool:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = list(rows)
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, *args):
        self.fetch_calls.append((sql, args))
        return list(self.rows)


@pytest.mark.anyio
async def test_cleanup_deleted_reflection_state_updates_all_surfaces() -> None:
    pool = _FakePool()
    message_id = uuid4()

    await cleanup_deleted_reflection_state(pool, message_ids=[message_id, message_id])

    sql_blob = "\n".join(sql for sql, _ in pool.execute_calls)
    assert "UPDATE mediator.reflection_sessions" in sql_blob
    assert "UPDATE mediator.reflection_entries" in sql_blob
    assert "plaintext_searchable = NULL" in sql_blob
    assert "UPDATE mediator.reflection_derivations" in sql_blob
    assert "supporting_message_ids = ARRAY[]::uuid[]" in sql_blob
    assert "UPDATE memories" in sql_blob
    assert "UPDATE observations" in sql_blob
    assert "UPDATE distillations" in sql_blob
    assert "UPDATE mediator.user_orientation_items" in sql_blob
    assert "DELETE FROM mediator.content_embeddings" in sql_blob
    assert "UPDATE mediator.embed_jobs" in sql_blob

    first_args = pool.execute_calls[0][1]
    assert list(first_args[0]) == [message_id]


@pytest.mark.anyio
async def test_purge_expired_deletions_runs_reflection_cleanup_before_message_rewrite() -> None:
    message_id = uuid4()
    pool = _FakePool(fetch_results=[[{"id": message_id}]])

    with patch(
        "app.services.deletion.cleanup_deleted_reflection_state",
        new=AsyncMock(),
    ) as cleanup_mock:
        result = await purge_expired_deletions(pool)

    cleanup_mock.assert_awaited_once_with(pool, message_ids=[message_id])
    assert result == "OK"
    assert len(pool.fetch_calls) == 1
    assert len(pool.execute_calls) == 1
    assert "content='[deleted]'" in pool.execute_calls[0][0]


@pytest.mark.anyio
async def test_store_visible_entry_query_requires_live_processed_source_messages() -> None:
    pool = _FakePool(fetch_results=[[]], fetchrow_result=None)
    store = ReflectionStore(pool)

    await store.list_entries(user_id=uuid4(), visible_only=True)
    await store.get_entry(user_id=uuid4(), entry_id=uuid4(), visible_only=True)

    list_sql = pool.fetch_calls[0][0]
    get_sql = pool.fetchrow_calls[0][0]

    for sql in (list_sql, get_sql):
        assert "plaintext_searchable IS NOT NULL" in sql
        assert "rs.status = 'processed'" in sql
        assert "source_messages.deleted_at IS NOT NULL" in sql


@pytest.mark.anyio
async def test_admin_list_sessions_excludes_deleted_source_sessions() -> None:
    pool = _FakePool(fetch_results=[[]])

    await admin_list_sessions(pool, status_filter="processed", limit=5)

    sql = pool.fetch_calls[0][0]
    assert "opened.deleted_at IS NOT NULL" in sql
    assert "source_messages.deleted_at IS NOT NULL" in sql
    assert "rs.status = $1" in sql


@pytest.mark.anyio
async def test_admin_memories_excludes_invalidated_reflection_tombstones() -> None:
    pool = _FetchOnlyPool(
        [
            {
                "id": uuid4(),
                "about_user_id": uuid4(),
                "content": "Live memory",
                "status": "active",
                "supersedes_memory_id": None,
                "created_at": datetime.now(timezone.utc),
                "last_referenced_at": None,
            },
            {
                "id": uuid4(),
                "about_user_id": uuid4(),
                "content": "Deleted reflection memory",
                "status": "invalidated",
                "supersedes_memory_id": None,
                "created_at": datetime.now(timezone.utc),
                "last_referenced_at": None,
            },
        ]
    )

    html = await admin_memories(pool, None, None, None)

    assert "Live memory" in html
    assert "Deleted reflection memory" not in html
    assert "status <> 'invalidated'" in pool.fetch_calls[0][0]


@pytest.mark.anyio
async def test_admin_memories_status_filter_does_not_resurrect_invalidated_tombstones() -> None:
    pool = _FetchOnlyPool(
        [
            {
                "id": uuid4(),
                "about_user_id": uuid4(),
                "content": "Deleted reflection memory",
                "status": "invalidated",
                "supersedes_memory_id": None,
                "created_at": datetime.now(timezone.utc),
                "last_referenced_at": None,
            }
        ]
    )

    html = await admin_memories(pool, None, None, "invalidated")

    assert "Deleted reflection memory" not in html


@pytest.mark.anyio
async def test_admin_observations_excludes_stale_reflection_tombstones() -> None:
    pool = _FetchOnlyPool(
        [
            {
                "id": uuid4(),
                "about_user_id": uuid4(),
                "content": "Live observation",
                "confidence": "high",
                "significance": 5,
                "status": "active",
                "supporting_message_ids": [],
                "created_at": datetime.now(timezone.utc),
                "last_reinforced_at": None,
            },
            {
                "id": uuid4(),
                "about_user_id": uuid4(),
                "content": "Deleted reflection observation",
                "confidence": "high",
                "significance": 5,
                "status": "stale",
                "supporting_message_ids": [],
                "created_at": datetime.now(timezone.utc),
                "last_reinforced_at": None,
            },
        ]
    )

    html = await admin_observations(pool, None)

    assert "Live observation" in html
    assert "Deleted reflection observation" not in html
    assert "status = 'active'" in pool.fetch_calls[0][0]


@pytest.mark.anyio
async def test_admin_distillations_excludes_invalidated_reflection_tombstones() -> None:
    pool = _FetchOnlyPool(
        [
            {
                "id": uuid4(),
                "content": "Live distillation",
                "shareable_summary": "Live summary",
                "confidence": "medium",
                "status": "active",
                "sensitivity": "medium",
                "visibility": "private",
                "source_user_ids": [],
                "related_memory_ids": [],
                "related_observation_ids": [],
                "related_theme_ids": [],
                "supporting_message_ids": [],
                "supersedes_distillation_id": None,
                "superseded_by_distillation_id": None,
                "revision_note": None,
                "revision_count": 0,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "revised_at": None,
                "retired_at": None,
            },
            {
                "id": uuid4(),
                "content": "Deleted reflection distillation",
                "shareable_summary": "Deleted reflection summary",
                "confidence": "medium",
                "status": "invalidated",
                "sensitivity": "medium",
                "visibility": "private",
                "source_user_ids": [],
                "related_memory_ids": [],
                "related_observation_ids": [],
                "related_theme_ids": [],
                "supporting_message_ids": [],
                "supersedes_distillation_id": None,
                "superseded_by_distillation_id": None,
                "revision_note": None,
                "revision_count": 0,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "revised_at": None,
                "retired_at": None,
            },
        ]
    )

    html = await admin_distillations(pool, None, None, None)

    assert "Live distillation" in html
    assert "Deleted reflection distillation" not in html
    assert "Deleted reflection summary" not in html
    assert "status <> 'invalidated'" in pool.fetch_calls[0][0]


@pytest.mark.anyio
async def test_admin_distillations_status_filter_does_not_resurrect_invalidated_tombstones() -> None:
    pool = _FetchOnlyPool(
        [
            {
                "id": uuid4(),
                "content": "Deleted reflection distillation",
                "shareable_summary": "Deleted reflection summary",
                "confidence": "medium",
                "status": "invalidated",
                "sensitivity": "medium",
                "visibility": "private",
                "source_user_ids": [],
                "related_memory_ids": [],
                "related_observation_ids": [],
                "related_theme_ids": [],
                "supporting_message_ids": [],
                "supersedes_distillation_id": None,
                "superseded_by_distillation_id": None,
                "revision_note": None,
                "revision_count": 0,
                "created_at": datetime.now(timezone.utc),
                "updated_at": datetime.now(timezone.utc),
                "revised_at": None,
                "retired_at": None,
            }
        ]
    )

    html = await admin_distillations(pool, None, None, "invalidated")

    assert "Deleted reflection distillation" not in html
    assert "Deleted reflection summary" not in html


def test_compass_visibility_hides_source_message_deleted_tombstones() -> None:
    assert not is_compass_visible(
        {
            "status": "retired",
            "review_state": "reviewed",
            "source": "user_stated",
            "closed_reason": "source_message_deleted",
        }
    )


@pytest.mark.anyio
async def test_hot_context_reflection_digest_requires_embeddable_live_entries() -> None:
    pool = _FakePool(fetch_results=[[]])

    await _fetch_reflections_digest(
        pool,
        user_id=uuid4(),
        bot_id="superpom",
        topic_id=uuid4(),
    )

    sql = pool.fetch_calls[0][0]
    assert "e.plaintext_searchable IS NOT NULL" in sql
    assert "btrim(e.plaintext_searchable) <> ''" in sql
    assert "s.status = 'processed'" in sql


@pytest.mark.anyio
async def test_normalization_bridge_excludes_deleted_source_messages() -> None:
    pool = _FakePool(fetch_results=[[]])

    await _fetch_message_texts(pool, [uuid4()])

    sql = pool.fetch_calls[0][0]
    assert "deleted_at IS NULL" in sql
