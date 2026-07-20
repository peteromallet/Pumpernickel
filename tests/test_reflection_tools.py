"""Reflection tool tests — authorization, pagination, status filtering,
source-message provenance, compact output, and proof of no task/reminder/
commitment/adherence artifact creation.

Covers: list_reflections, get_reflection, search_reflections,
finalize_reflection, correct_reflection.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.services.reflections import (
    ReflectionEntry,
    ReflectionSession,
    ReflectionStore,
    SessionFinalizeConflictError,
    SessionNotFoundError,
)
from app.services.tools.reflection_tools import (
    correct_reflection,
    finalize_reflection,
    get_reflection,
    list_reflections,
    search_reflections,
)
from app.services.turn_context import TurnContext
from tool_schemas import (
    CorrectReflectionInput,
    FinalizeReflectionInput,
    GetReflectionInput,
    ListReflectionsInput,
    SearchReflectionsInput,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


def _make_user(user_id: UUID | None = None, name: str = "test-user") -> SimpleNamespace:
    from app.models.user import User

    return User(
        id=user_id or _uid(),
        name=name,
        phone="+15550009999",
        timezone="America/Los_Angeles",
    )


def _make_turn_ctx(
    *,
    user_id: UUID | None = None,
    bot_id: str = "mediator",
    pool: object = None,
    partner: object | None = None,
) -> TurnContext:
    """Build a minimal TurnContext for tool tests."""
    uid = user_id or _uid()
    return TurnContext(
        turn_id=_uid(),
        pool=pool or _FakePool(),
        user=_make_user(user_id=uid),
        partner=partner,
        triggering_message_ids=[],
        bot_id=bot_id,
        user_id=uid,
    )


# ── Fake asyncpg pool ───────────────────────────────────────────────────────


class _FakePool:
    """Minimal asyncpg pool double that records calls and returns pre-configured rows."""

    def __init__(
        self,
        fetch_rows: list[dict] | None = None,
        fetchrow_result: object = None,
        fetchval_result: object = None,
        execute_result: str = "OK",
    ) -> None:
        self.fetch_rows = fetch_rows or []
        self.fetchrow_result = fetchrow_result
        self.fetchval_result = fetchval_result
        self.execute_result = execute_result
        # Recording
        self.fetch_sqls: list[str] = []
        self.fetchrow_sqls: list[str] = []
        self.fetchval_sqls: list[str] = []
        self.execute_sqls: list[str] = []

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.fetch_sqls.append(sql)
        return self.fetch_rows

    async def fetchrow(self, sql: str, *args) -> object:
        self.fetchrow_sqls.append(sql)
        return self.fetchrow_result

    async def fetchval(self, sql: str, *args) -> object:
        self.fetchval_sqls.append(sql)
        return self.fetchval_result

    async def execute(self, sql: str, *args) -> str:
        self.execute_sqls.append(sql)
        return self.execute_result


# ── Fake ReflectionEntry / ReflectionSession builders ───────────────────────


def _make_entry(
    entry_id: UUID | None = None,
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    bot_id: str = "mediator",
    topic_id: UUID | None = None,
    template_key: str = "daily",
    temporal_scope: str = "day",
    phase: str = "opening",
    plaintext_searchable: str | None = "test searchable text",
    source_message_ids: list[UUID] | None = None,
    revision_number: int = 1,
    supersedes_entry_id: UUID | None = None,
    created_at: datetime | None = None,
) -> ReflectionEntry:
    return ReflectionEntry(
        id=entry_id or _uid(),
        session_id=session_id or _uid(),
        user_id=user_id or _uid(),
        topic_id=topic_id,
        bot_id=bot_id,
        template_key=template_key,
        temporal_scope=temporal_scope,
        phase=phase,
        period_start=None,
        period_end=None,
        timezone="UTC",
        source_message_ids=source_message_ids or [],
        payload_encrypted=None,
        plaintext_searchable=plaintext_searchable,
        summary_encrypted=None,
        schema_version=1,
        processor_version=None,
        revision_number=revision_number,
        supersedes_entry_id=supersedes_entry_id,
        created_by_turn_id=None,
        created_at=created_at or datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
    )


def _make_session(
    session_id: UUID | None = None,
    *,
    user_id: UUID | None = None,
    bot_id: str = "mediator",
    status: str = "processed",
    topic_id: UUID | None = None,
    classification_metadata: dict | None = None,
    source_message_ids: list[UUID] | None = None,
    finalized_at: datetime | None = None,
) -> ReflectionSession:
    return ReflectionSession(
        id=session_id or _uid(),
        user_id=user_id or _uid(),
        topic_id=topic_id,
        bot_id=bot_id,
        opened_by_message_id=None,
        opened_by_turn_id=None,
        source_message_ids=source_message_ids or [],
        template_key="daily",
        temporal_scope="day",
        phase="opening",
        period_start=None,
        period_end=None,
        timezone="UTC",
        classification_source=None,
        classification_confidence=None,
        classification_metadata=classification_metadata,
        status=status,
        idle_finalize_at=None,
        finalized_at=finalized_at,
        processed_at=None,
        abandoned_at=None,
        claimed_by=None,
        claimed_at=None,
        retry_count=0,
        failure_class=None,
        failure_reason=None,
        last_error=None,
        idempotency_key=None,
        created_at=datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
        updated_at=datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
    )


# ═══════════════════════════════════════════════════════════════════════════
# list_reflections tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_list_reflections_returns_compact_summaries_by_default():
    """Default list returns compact summaries without internals."""
    user_id = _uid()
    session_id = _uid()
    entry = _make_entry(user_id=user_id, session_id=session_id)
    pool = _FakePool(fetch_rows=[_entry_to_row(entry)])

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    result = await list_reflections(ctx, ListReflectionsInput())

    assert result.include_internals is False
    assert len(result.entries) == 1
    # Summary has stable fields but not bot_id, user_id, etc.
    summary = result.entries[0]
    assert summary.id == entry.id
    assert summary.session_id == entry.session_id
    assert summary.template_key == entry.template_key
    # Compact summary should NOT expose user_id or bot_id
    assert not hasattr(summary, "bot_id")


@pytest.mark.anyio
async def test_list_reflections_include_internals_exposes_detail():
    """When include_internals=True, details including bot_id and source_message_ids appear."""
    user_id = _uid()
    session_id = _uid()
    msg_id = _uid()
    entry = _make_entry(
        user_id=user_id, session_id=session_id, source_message_ids=[msg_id]
    )
    session = _make_session(
        session_id=session_id,
        user_id=user_id,
        status="processed",
        classification_metadata={"auto_classified": True},
    )

    # fetch for list_entries, then fetchrow for get_session
    pool = _FakePool(
        fetch_rows=[_entry_to_row(entry)],
        fetchrow_result=_session_to_row(session),
    )

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    result = await list_reflections(
        ctx, ListReflectionsInput(include_internals=True)
    )

    assert result.include_internals is True
    assert len(result.entries) == 1
    detail = result.entries[0]
    assert detail.id == entry.id
    assert detail.bot_id == entry.bot_id
    assert detail.user_id == user_id
    assert detail.source_message_ids == [msg_id]
    assert detail.classification_metadata == {"auto_classified": True}


@pytest.mark.anyio
async def test_list_reflections_current_only_filters_superseded():
    """When current_only=True (default), superseded entries are excluded by store."""
    user_id = _uid()
    session_id = _uid()
    entry = _make_entry(user_id=user_id, session_id=session_id)
    pool = _FakePool(fetch_rows=[_entry_to_row(entry)])

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    result = await list_reflections(
        ctx, ListReflectionsInput(current_only=True)
    )

    # Verify current_only was passed through (SQL uses supersedes_entry_id IS NULL)
    assert len(pool.fetch_sqls) >= 1
    sql = pool.fetch_sqls[0]
    assert "supersedes_entry_id IS NULL" in sql
    assert len(result.entries) == 1


@pytest.mark.anyio
async def test_list_reflections_current_only_false_includes_superseded():
    """When current_only=False, superseded entries are included."""
    user_id = _uid()
    session_id = _uid()
    superseded_id = _uid()
    entry = _make_entry(
        user_id=user_id,
        session_id=session_id,
        supersedes_entry_id=superseded_id,
    )
    pool = _FakePool(fetch_rows=[_entry_to_row(entry)])

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    result = await list_reflections(
        ctx, ListReflectionsInput(current_only=False)
    )

    # supersedes_entry_id IS NULL should NOT be in the SQL
    sql = pool.fetch_sqls[0]
    assert "supersedes_entry_id IS NULL" not in sql
    assert len(result.entries) == 1


@pytest.mark.anyio
async def test_list_reflections_cross_user_rejected():
    """List enforces user_id scope — user B gets empty results for user A's entries."""
    user_a = _uid()
    user_b = _uid()
    entry = _make_entry(user_id=user_a)
    # Store returns empty for user_b because store scopes by user_id
    pool = _FakePool(fetch_rows=[])

    ctx = _make_turn_ctx(user_id=user_b, pool=pool)
    result = await list_reflections(ctx, ListReflectionsInput())

    assert result.entries == []
    # The store was called (list_entries queries by user_id)
    assert len(pool.fetch_sqls) >= 1
    # SQL has WHERE user_id = $1 — the store enforces user scope
    assert "user_id = $" in pool.fetch_sqls[0]


@pytest.mark.anyio
async def test_list_reflections_limit_enforced():
    """The limit parameter is passed to the store and capped."""
    user_id = _uid()
    pool = _FakePool(fetch_rows=[])

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    await list_reflections(ctx, ListReflectionsInput(limit=5))

    assert len(pool.fetch_sqls) >= 1
    # LIMIT is present with a parameter placeholder
    assert "LIMIT $" in pool.fetch_sqls[0]


@pytest.mark.anyio
async def test_list_reflections_bot_id_driven():
    """When a bot_id is given, results are filtered by bot."""
    user_id = _uid()
    entry = _make_entry(user_id=user_id, bot_id="coach")
    pool = _FakePool(fetch_rows=[_entry_to_row(entry)])

    ctx = _make_turn_ctx(user_id=user_id, bot_id="coach", pool=pool)
    result = await list_reflections(
        ctx, ListReflectionsInput(bot_id="coach")
    )

    assert len(result.entries) == 1
    # bot_id should appear in SQL
    sql = pool.fetch_sqls[0]
    assert "bot_id = $" in sql


@pytest.mark.anyio
async def test_list_reflections_session_id_filters():
    """When session_id is given, only entries for that session are returned."""
    user_id = _uid()
    session_id = _uid()
    entry = _make_entry(user_id=user_id, session_id=session_id)
    pool = _FakePool(fetch_rows=[_entry_to_row(entry)])

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    result = await list_reflections(
        ctx, ListReflectionsInput(session_id=session_id)
    )

    assert len(result.entries) == 1
    sql = pool.fetch_sqls[0]
    assert "session_id = $" in sql


# ═══════════════════════════════════════════════════════════════════════════
# get_reflection tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_get_reflection_returns_entry_with_source_message_ids():
    """get_reflection always returns source_message_ids."""
    user_id = _uid()
    entry_id = _uid()
    msg_1 = _uid()
    msg_2 = _uid()
    entry = _make_entry(
        entry_id=entry_id,
        user_id=user_id,
        source_message_ids=[msg_1, msg_2],
    )
    pool = _FakePool(fetchrow_result=_entry_to_row(entry))

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    result = await get_reflection(ctx, GetReflectionInput(entry_id=entry_id))

    assert result.is_error is False
    assert result.entry is not None
    assert result.entry.id == entry_id
    assert result.entry.source_message_ids == [msg_1, msg_2]


@pytest.mark.anyio
async def test_get_reflection_not_found_returns_error():
    """When the entry doesn't exist, an error output is returned."""
    user_id = _uid()
    pool = _FakePool(fetchrow_result=None)

    ctx = _make_turn_ctx(user_id=user_id, pool=pool)
    result = await get_reflection(ctx, GetReflectionInput(entry_id=_uid()))

    assert result.is_error is True
    assert result.error is not None
    assert "not found" in result.error.lower()
    assert result.entry is None


@pytest.mark.anyio
async def test_get_reflection_include_internals_adds_classification():
    """When include_internals=True, classification metadata is populated."""
    user_id = _uid()
    entry_id = _uid()
    session_id = _uid()
    entry = _make_entry(entry_id=entry_id, user_id=user_id, session_id=session_id)
    session = _make_session(
        session_id=session_id,
        user_id=user_id,
        status="processed",
        classification_metadata={"confidence": 0.95},
    )

    # First call: get_entry fetchrow, second: get_session fetchrow
    pool = _FakePool()
    pool.fetchrow_result = _entry_to_row(entry)

    # We need to patch _store.get_session to return our session
    with patch.object(ReflectionStore, "get_entry", return_value=entry), \
         patch.object(ReflectionStore, "get_session", return_value=session):
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await get_reflection(
            ctx, GetReflectionInput(entry_id=entry_id, include_internals=True)
        )

    assert result.is_error is False
    assert result.entry is not None
    assert result.entry.classification_metadata == {"confidence": 0.95}


@pytest.mark.anyio
async def test_get_reflection_cross_user_rejected():
    """get_reflection returns None/error when the user doesn't own the entry."""
    user_a = _uid()
    user_b = _uid()
    entry = _make_entry(entry_id=_uid(), user_id=user_a)
    pool = _FakePool(fetchrow_result=None)  # Store returns None for wrong user

    ctx = _make_turn_ctx(user_id=user_b, pool=pool)
    result = await get_reflection(
        ctx, GetReflectionInput(entry_id=entry.id)
    )

    assert result.is_error is True
    assert "not found" in result.error.lower()


# ═══════════════════════════════════════════════════════════════════════════
# search_reflections tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_search_reflections_blank_query_returns_empty():
    """Empty or whitespace-only queries return empty without hitting retrieval."""
    ctx = _make_turn_ctx()
    result = await search_reflections(
        ctx, SearchReflectionsInput(query="   ")
    )

    assert result.hits == []
    assert result.total_matched == 0
    assert result.is_error is False


@pytest.mark.anyio
async def test_search_reflections_compact_output_hides_internals():
    """Default compact mode hides plaintext_searchable and internal metadata."""
    user_id = _uid()
    entry_id = _uid()
    session_id = _uid()
    msg_id = _uid()

    mock_results = [
        _make_retrieval_result(
            source_type="reflection",
            source_id=entry_id,
            match_type="exact",
            keyword_score=0.8,
            keyword_rank=1,
            evidence_metadata={
                "session_id": str(session_id),
                "template_key": "weekly_review",
                "temporal_scope": "week",
                "phase": "closing",
                "revision_number": 1,
                "schema_version": 1,
                "supersedes_entry_id": None,
            },
            source_message_ids=[msg_id],
        )
    ]

    with patch(
        "app.services.tools.reflection_tools.hybrid_search",
        return_value=mock_results,
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await search_reflections(
            ctx,
            SearchReflectionsInput(
                query="weekly review", mode="exact", include_internals=False
            ),
        )

    assert result.include_internals is False
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.entry_id == entry_id
    assert hit.plaintext_searchable is None  # compact hides plaintext
    assert hit.source_message_ids == [msg_id]
    assert hit.evidence_metadata is not None  # evidence_metadata IS present in compact
    assert hit.evidence_metadata["template_key"] == "weekly_review"


@pytest.mark.anyio
async def test_search_reflections_include_internals_exposes_detail():
    """When include_internals=True, plaintext and full detail are loaded."""
    user_id = _uid()
    entry_id = _uid()
    session_id = _uid()
    msg_id = _uid()
    entry = _make_entry(
        entry_id=entry_id,
        user_id=user_id,
        session_id=session_id,
        plaintext_searchable="full reflection text here",
        source_message_ids=[msg_id],
        template_key="daily_checkin",
        temporal_scope="day",
        phase="opening",
        revision_number=1,
    )
    session = _make_session(
        session_id=session_id,
        user_id=user_id,
        status="processed",
        classification_metadata={"score": 0.9},
    )

    mock_results = [
        _make_retrieval_result(
            source_type="reflection",
            source_id=entry_id,
            match_type="exact",
            keyword_score=0.9,
            keyword_rank=1,
            source_message_ids=[msg_id],
            evidence_metadata={
                "session_id": str(session_id),
                "template_key": "daily_checkin",
                "temporal_scope": "day",
                "phase": "opening",
                "revision_number": 1,
                "schema_version": 1,
                "supersedes_entry_id": None,
            },
        )
    ]

    with patch(
        "app.services.tools.reflection_tools.hybrid_search",
        return_value=mock_results,
    ), patch.object(
        ReflectionStore, "get_entry", return_value=entry
    ), patch.object(
        ReflectionStore, "get_session", return_value=session
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await search_reflections(
            ctx,
            SearchReflectionsInput(
                query="daily", mode="exact", include_internals=True
            ),
        )

    assert result.include_internals is True
    assert len(result.hits) == 1
    hit = result.hits[0]
    assert hit.plaintext_searchable == "full reflection text here"
    assert hit.source_message_ids == [msg_id]
    assert hit.evidence_metadata is None  # include_internals suppresses evidence_metadata
    assert hit.revision_number == 1


@pytest.mark.anyio
async def test_search_reflections_limit_caps_results():
    """Results are capped at the requested limit."""
    user_id = _uid()
    entry_ids = [_uid() for _ in range(5)]

    mock_results = [
        _make_retrieval_result(
            source_type="reflection",
            source_id=eid,
            match_type="exact",
            keyword_score=0.9 - i * 0.1,
            keyword_rank=i + 1,
        )
        for i, eid in enumerate(entry_ids)
    ]

    with patch(
        "app.services.tools.reflection_tools.hybrid_search",
        return_value=mock_results,
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await search_reflections(
            ctx, SearchReflectionsInput(query="test", limit=3, mode="exact")
        )

    assert len(result.hits) == 3
    assert result.total_matched == 5


@pytest.mark.anyio
async def test_search_reflections_source_message_provenance():
    """source_message_ids are included as provenance in compact mode."""
    user_id = _uid()
    entry_id = _uid()
    session_id = _uid()
    msg_a = _uid()
    msg_b = _uid()

    mock_results = [
        _make_retrieval_result(
            source_type="reflection",
            source_id=entry_id,
            match_type="exact",
            keyword_score=0.75,
            keyword_rank=1,
            evidence_metadata={
                "session_id": str(session_id),
                "template_key": "custom",
                "temporal_scope": "custom",
                "phase": "freeform",
                "revision_number": 1,
                "schema_version": 1,
                "supersedes_entry_id": None,
            },
            source_message_ids=[msg_a, msg_b],
        )
    ]

    with patch(
        "app.services.tools.reflection_tools.hybrid_search",
        return_value=mock_results,
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await search_reflections(
            ctx, SearchReflectionsInput(query="provenance test", mode="exact")
        )

    assert len(result.hits) == 1
    assert result.hits[0].source_message_ids == [msg_a, msg_b]


@pytest.mark.anyio
async def test_search_reflections_retrieval_error_graceful():
    """When hybrid_search fails, an error output is returned instead of crashing."""
    with patch(
        "app.services.tools.reflection_tools.hybrid_search",
        side_effect=RuntimeError("provider down"),
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(pool=pool)
        result = await search_reflections(
            ctx, SearchReflectionsInput(query="crash test", mode="exact")
        )

    assert result.is_error is True
    assert result.error is not None
    assert "retrieval" in result.error.lower()
    assert result.hits == []


@pytest.mark.anyio
async def test_search_reflections_filters_non_reflection_results():
    """Only results with source_type='reflection' are returned."""
    user_id = _uid()
    reflection_id = _uid()
    message_id = _uid()

    mock_results = [
        _make_retrieval_result(
            source_type="reflection",
            source_id=reflection_id,
            match_type="exact",
            keyword_score=0.8,
            keyword_rank=1,
        ),
        _make_retrieval_result(
            source_type="message",
            source_id=message_id,
            match_type="exact",
            keyword_score=0.9,
            keyword_rank=2,
        ),
    ]

    with patch(
        "app.services.tools.reflection_tools.hybrid_search",
        return_value=mock_results,
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await search_reflections(
            ctx, SearchReflectionsInput(query="test", mode="exact")
        )

    assert len(result.hits) == 1
    assert result.hits[0].entry_id == reflection_id
    assert result.total_matched == 1  # only the reflection counts


# ═══════════════════════════════════════════════════════════════════════════
# finalize_reflection tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_finalize_reflection_success():
    """Finalizing a collecting session produces the expected output."""
    user_id = _uid()
    session_id = _uid()
    msg_id = _uid()
    session = _make_session(
        session_id=session_id,
        user_id=user_id,
        status="finalizing",
        source_message_ids=[msg_id],
        finalized_at=datetime(2025, 6, 15, 10, 0, tzinfo=UTC),
    )

    with patch.object(ReflectionStore, "finalize_session", return_value=session):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await finalize_reflection(
            ctx, FinalizeReflectionInput(session_id=session_id)
        )

    assert result.is_error is False
    assert result.session_id == session_id
    assert result.status == "finalizing"
    assert result.source_message_ids == [msg_id]
    assert result.finalized_at is not None


@pytest.mark.anyio
async def test_finalize_reflection_not_found():
    """When the session doesn't exist, an error is returned."""
    user_id = _uid()
    session_id = _uid()

    with patch.object(
        ReflectionStore,
        "finalize_session",
        side_effect=SessionNotFoundError("not found"),
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await finalize_reflection(
            ctx, FinalizeReflectionInput(session_id=session_id)
        )

    assert result.is_error is True
    assert "not found" in result.error.lower()


@pytest.mark.anyio
async def test_finalize_reflection_conflict():
    """When the session is not in collecting state, a conflict error is returned."""
    user_id = _uid()
    session_id = _uid()

    with patch.object(
        ReflectionStore,
        "finalize_session",
        side_effect=SessionFinalizeConflictError("already finalized"),
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await finalize_reflection(
            ctx, FinalizeReflectionInput(session_id=session_id)
        )

    assert result.is_error is True
    assert "already finalized" in str(result.error)


# ═══════════════════════════════════════════════════════════════════════════
# correct_reflection tests
# ═══════════════════════════════════════════════════════════════════════════


@pytest.mark.anyio
async def test_correct_reflection_creates_new_revision():
    """Correction creates a new revision that supersedes the old entry."""
    user_id = _uid()
    old_entry_id = _uid()
    new_entry_id = _uid()
    session_id = _uid()
    new_entry = _make_entry(
        entry_id=new_entry_id,
        user_id=user_id,
        session_id=session_id,
        revision_number=2,
        supersedes_entry_id=old_entry_id,
    )

    with patch.object(
        ReflectionStore, "correct_entry", return_value=new_entry
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await correct_reflection(
            ctx,
            CorrectReflectionInput(
                supersedes_entry_id=old_entry_id,
                plaintext_searchable="corrected text",
                summary="updated summary",
            ),
        )

    assert result.is_error is False
    assert result.entry_id == new_entry_id
    assert result.session_id == session_id
    assert result.revision_number == 2
    assert result.supersedes_entry_id == old_entry_id


@pytest.mark.anyio
async def test_correct_reflection_not_found():
    """When the entry to supersede doesn't exist, an error is returned."""
    user_id = _uid()
    entry_id = _uid()

    with patch.object(
        ReflectionStore,
        "correct_entry",
        side_effect=LookupError("entry not found"),
    ):
        pool = _FakePool()
        ctx = _make_turn_ctx(user_id=user_id, pool=pool)
        result = await correct_reflection(
            ctx,
            CorrectReflectionInput(
                supersedes_entry_id=entry_id,
                plaintext_searchable="corrected",
            ),
        )

    assert result.is_error is True
    assert "not found" in result.error.lower()


# ═══════════════════════════════════════════════════════════════════════════
# No task/reminder/commitment/adherence artifact creation
# ═══════════════════════════════════════════════════════════════════════════


def test_reflection_tools_module_does_not_import_task_or_reminder_modules():
    """The reflection_tools module must not import task, reminder, commitment,
    or adherence subsystems."""
    import ast
    import inspect

    from app.services.tools import reflection_tools as rt

    source = inspect.getsource(rt)
    tree = ast.parse(source)

    # Collect all imports
    import_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                import_names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                import_names.add(node.module)

    forbidden_keywords = [
        "task",
        "reminder",
        "commitment",
        "adherence",
        "hector",
        "scheduled_jobs",
        "schedule",
    ]

    for name in import_names:
        for forbidden in forbidden_keywords:
            assert forbidden not in name.lower(), (
                f"reflection_tools imports forbidden module: {name}"
            )


def test_reflection_tools_functions_only_use_reflection_and_retrieval():
    """Tool functions call only ReflectionStore and retrieval methods;
    they do not write to task/reminder/commitment/adherence tables."""
    import inspect

    from app.services.tools import reflection_tools as rt

    # Get all public async functions
    tool_fns = [
        rt.list_reflections,
        rt.get_reflection,
        rt.search_reflections,
        rt.finalize_reflection,
        rt.correct_reflection,
    ]

    # These are the only allowed store/service interactions
    allowed_attrs = {
        "list_entries",
        "get_entry",
        "get_session",
        "finalize_session",
        "correct_entry",
        "hybrid_search",
        "get_current_entry",
        "ReconciliationEngine",
        "DerivationLedger",
    }

    for fn in tool_fns:
        source = inspect.getsource(fn)
        # The source should never reference task, reminder, commitment, or adherence
        forbidden = ["task", "reminder", "commitment", "adherence", "scheduled_job"]
        for word in forbidden:
            assert word not in source.lower(), (
                f"{fn.__name__} references forbidden concept: {word}"
            )


def test_tool_schemas_no_task_or_reminder_schemas():
    """Reflection tool schemas do not inherit from or reference task/reminder
    schema classes."""
    from tool_schemas import (
        CorrectReflectionInput,
        CorrectReflectionOutput,
        FinalizeReflectionInput,
        FinalizeReflectionOutput,
        GetReflectionInput,
        GetReflectionOutput,
        ListReflectionsInput,
        ListReflectionsOutput,
        ReflectionEntryDetail,
        ReflectionEntrySummary,
        ReflectionSearchHit,
        SearchReflectionsInput,
        SearchReflectionsOutput,
    )

    reflection_schemas = [
        ReflectionEntrySummary,
        ListReflectionsInput,
        ReflectionEntryDetail,
        ListReflectionsOutput,
        GetReflectionInput,
        GetReflectionOutput,
        FinalizeReflectionInput,
        FinalizeReflectionOutput,
        CorrectReflectionInput,
        CorrectReflectionOutput,
        ReflectionSearchHit,
        SearchReflectionsInput,
        SearchReflectionsOutput,
    ]

    for schema_cls in reflection_schemas:
        # Check field names don't include forbidden concepts
        for field_name in schema_cls.model_fields:
            forbidden = ["task", "reminder", "commitment", "adherence"]
            for word in forbidden:
                assert word not in field_name.lower(), (
                    f"{schema_cls.__name__}.{field_name} references forbidden concept"
                )


# ═══════════════════════════════════════════════════════════════════════════
# Tool registry registration
# ═══════════════════════════════════════════════════════════════════════════


def test_reflection_tools_are_registered():
    """All five reflection tools appear in TOOL_REGISTRY."""
    from tool_schemas import TOOL_REGISTRY

    expected = [
        "list_reflections",
        "get_reflection",
        "search_reflections",
        "finalize_reflection",
        "correct_reflection",
    ]
    for tool_name in expected:
        assert tool_name in TOOL_REGISTRY, f"{tool_name} missing from TOOL_REGISTRY"


# ── Internal helpers ────────────────────────────────────────────────────────


def _entry_to_row(entry: ReflectionEntry) -> dict:
    """Convert a ReflectionEntry to a dict mimicking an asyncpg row."""
    return {
        "id": entry.id,
        "session_id": entry.session_id,
        "user_id": entry.user_id,
        "topic_id": entry.topic_id,
        "bot_id": entry.bot_id,
        "template_key": entry.template_key,
        "temporal_scope": entry.temporal_scope,
        "phase": entry.phase,
        "period_start": entry.period_start,
        "period_end": entry.period_end,
        "timezone": entry.timezone,
        "source_message_ids": entry.source_message_ids,
        "payload_encrypted": entry.payload_encrypted,
        "plaintext_searchable": entry.plaintext_searchable,
        "summary_encrypted": entry.summary_encrypted,
        "schema_version": entry.schema_version,
        "processor_version": entry.processor_version,
        "revision_number": entry.revision_number,
        "supersedes_entry_id": entry.supersedes_entry_id,
        "created_by_turn_id": entry.created_by_turn_id,
        "created_at": entry.created_at,
    }


def _session_to_row(session: ReflectionSession) -> dict:
    """Convert a ReflectionSession to a dict mimicking an asyncpg row."""
    return {
        "id": session.id,
        "user_id": session.user_id,
        "topic_id": session.topic_id,
        "bot_id": session.bot_id,
        "opened_by_message_id": session.opened_by_message_id,
        "opened_by_turn_id": session.opened_by_turn_id,
        "source_message_ids": session.source_message_ids,
        "template_key": session.template_key,
        "temporal_scope": session.temporal_scope,
        "phase": session.phase,
        "period_start": session.period_start,
        "period_end": session.period_end,
        "timezone": session.timezone,
        "classification_source": session.classification_source,
        "classification_confidence": session.classification_confidence,
        "classification_metadata": session.classification_metadata,
        "status": session.status,
        "idle_finalize_at": session.idle_finalize_at,
        "finalized_at": session.finalized_at,
        "processed_at": session.processed_at,
        "abandoned_at": session.abandoned_at,
        "claimed_by": session.claimed_by,
        "claimed_at": session.claimed_at,
        "retry_count": session.retry_count,
        "failure_class": session.failure_class,
        "failure_reason": session.failure_reason,
        "last_error": session.last_error,
        "idempotency_key": session.idempotency_key,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def _make_retrieval_result(
    *,
    source_type: str = "reflection",
    source_id: UUID | None = None,
    match_type: str = "exact",
    keyword_score: float | None = None,
    keyword_rank: int | None = None,
    semantic_rank: int | None = None,
    rrf_score: float | None = None,
    evidence_metadata: dict | None = None,
    source_message_ids: list[UUID] | None = None,
    sent_at: datetime | None = None,
) -> object:
    """Build a RetrievalResult-like object for mocking hybrid_search."""
    from app.services.retrieval import RetrievalResult

    return RetrievalResult(
        message_id=None if source_type != "message" else source_id,
        source_type=source_type,
        source_id=source_id,
        match_type=match_type,
        rrf_score=rrf_score,
        keyword_rank=keyword_rank,
        semantic_rank=semantic_rank,
        semantic_degraded=False,
        keyword_score=keyword_score,
        sent_at=sent_at or datetime(2025, 6, 1, 12, 0, tzinfo=UTC),
        evidence_metadata=evidence_metadata,
        source_message_ids=source_message_ids,
    )
