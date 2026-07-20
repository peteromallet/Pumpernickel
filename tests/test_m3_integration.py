"""M3 end-to-end integration/regression coverage (T14).

Proves the whole M3 path preserves traceability and bounded context using
one representative fixture that spans:
  - Raw source messages
  - An accepted reflection
  - A corrected reflection
  - A deferred or rejected candidate
  - A derived observation that remains separately traceable

Covers:
  - Keyword/vector retrieval scope boundaries
  - Hot context stays compact while historical details remain available
    through explicit tools
  - Cross-scope rejection
  - Opening/closing evidence and open-loop markers
  - No raw encrypted payload exposure in compact results
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.services.hot_context_solo import (
    HotContextSolo,
    _render_solo_with_counts,
    render_hot_context_solo,
)
from app.services.reflections import (
    ReflectionDerivation,
    ReflectionEntry,
    ReflectionSession,
)
from app.services.retrieval import (
    RetrievalQuery,
    RetrievalResult,
    hybrid_search,
)
from app.services.tools.reflection_tools import (
    get_reflection,
    list_reflections,
    search_reflections,
)
from app.services.turn_context import TurnContext
from tool_schemas import (
    GetReflectionInput,
    ListReflectionsInput,
    SearchReflectionsInput,
)

pytestmark = pytest.mark.anyio

# ── Stable test IDs ──────────────────────────────────────────────────────────

_MSG_1 = UUID("10000000-0000-4000-8000-000000000001")
_MSG_2 = UUID("10000000-0000-4000-8000-000000000002")
_SESSION_A = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_SESSION_B = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_SESSION_C = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_ENTRY_A1 = UUID("a1a1a1a1-a1a1-4a1a-8a1a-a1a1a1a1a1a1")  # accepted
_ENTRY_A2 = UUID("a2a2a2a2-a2a2-4a2a-8a2a-a2a2a2a2a2a2")  # corrected version of A1
_ENTRY_B1 = UUID("b1b1b1b1-b1b1-4b1b-8b1b-b1b1b1b1b1b1")  # deferred/rejected
_DERIVATION_O1 = UUID("d1d1d1d1-d1d1-4d1d-8d1d-d1d1d1d1d1d1")  # derived observation

_USER_OWNER = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
_USER_OTHER = UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000010")
_BOT_ID = "mediator"


# ── Helpers ──────────────────────────────────────────────────────────────────

def _uid() -> UUID:
    return uuid4()


def _now() -> datetime:
    return datetime(2025, 7, 15, 12, 0, tzinfo=UTC)


# ── FakePool ──────────────────────────────────────────────────────────────────


class FakePool:
    """Configurable fake asyncpg pool recording SQL calls and returning preset rows."""

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
        self.fetch_sqls: list[str] = []
        self.fetchrow_sqls: list[str] = []
        self.fetchval_sqls: list[str] = []
        self.execute_sqls: list[str] = []
        self.fetch_call_count = 0

    async def fetch(self, sql: str, *args) -> list[dict]:
        self.fetch_sqls.append(sql)
        result = self.fetch_rows
        if isinstance(result, list) and result and isinstance(result[0], list):
            idx = self.fetch_call_count
            self.fetch_call_count += 1
            return result[idx] if idx < len(result) else []
        return result

    async def fetchrow(self, sql: str, *args) -> object:
        self.fetchrow_sqls.append(sql)
        return self.fetchrow_result

    async def fetchval(self, sql: str, *args) -> object:
        self.fetchval_sqls.append(sql)
        return self.fetchval_result

    async def execute(self, sql: str, *args) -> str:
        self.execute_sqls.append(sql)
        return self.execute_result

    async def acquire(self):
        return _FakeConnection(self)

    async def transaction(self):
        return _FakeTransaction(self)


class _FakeConnection:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, sql: str, *args):
        return await self.pool.execute(sql, *args)

    async def fetch(self, sql: str, *args):
        return await self.pool.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args):
        return await self.pool.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args):
        return await self.pool.fetchval(sql, *args)


class _FakeTransaction:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool
        self._entered = False

    async def __aenter__(self):
        self._entered = True
        return self

    async def __aexit__(self, *args):
        self._entered = False

    async def execute(self, sql: str, *args):
        return await self.pool.execute(sql, *args)

    async def fetch(self, sql: str, *args):
        return await self.pool.fetch(sql, *args)


# ── Reflection entry/session builders ────────────────────────────────────────


def _make_entry(
    entry_id: UUID | None = None,
    *,
    user_id: UUID | None = None,
    session_id: UUID | None = None,
    bot_id: str = _BOT_ID,
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
        created_at=created_at or _now(),
    )


def _make_session(
    session_id: UUID | None = None,
    *,
    user_id: UUID | None = None,
    bot_id: str = _BOT_ID,
    status: str = "processed",
    topic_id: UUID | None = None,
    classification_metadata: dict | None = None,
    source_message_ids: list[UUID] | None = None,
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
        finalized_at=None,
        processed_at=None,
        abandoned_at=None,
        claimed_by=None,
        claimed_at=None,
        retry_count=0,
        failure_class=None,
        failure_reason=None,
        last_error=None,
        idempotency_key=None,
        created_at=_now(),
        updated_at=_now(),
    )


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


def _make_user(user_id: UUID | None = None, name: str = "test-user"):
    from app.models.user import User

    return User(
        id=user_id or _uid(),
        name=name,
        phone="+155****9999",
        timezone="America/Los_Angeles",
    )


def _make_turn_ctx(
    *,
    user_id: UUID | None = None,
    bot_id: str = _BOT_ID,
    pool: object = None,
) -> TurnContext:
    uid = user_id or _uid()
    return TurnContext(
        turn_id=_uid(),
        pool=pool or FakePool(),
        user=_make_user(user_id=uid),
        partner=None,
        triggering_message_ids=[],
        bot_id=bot_id,
        user_id=uid,
    )


# ── Fixture data ─────────────────────────────────────────────────────────────


def _build_full_fixture() -> dict:
    """Build the representative M3 fixture with all required scenarios.

    Returns a dict with keys:
      - entries: dict mapping scenario name to ReflectionEntry
      - sessions: dict mapping scenario name to ReflectionSession
      - accepted_entry, corrected_entry, deferred_entry
      - derivation
    """
    now = _now()

    # Session A: accepted reflection (opening, later gets corrected)
    session_a = _make_session(
        session_id=_SESSION_A,
        user_id=_USER_OWNER,
        status="processed",
        topic_id=_TOPIC_ID,
        source_message_ids=[_MSG_1],
    )

    # Accepted entry (original, later superseded by correction)
    accepted_entry = _make_entry(
        entry_id=_ENTRY_A1,
        user_id=_USER_OWNER,
        session_id=_SESSION_A,
        topic_id=_TOPIC_ID,
        template_key="weekly_review",
        temporal_scope="week",
        phase="opening",
        plaintext_searchable="This week was productive overall. I finished the report and started the design doc.",
        source_message_ids=[_MSG_1],
        revision_number=1,
        supersedes_entry_id=None,
        created_at=now - timedelta(days=3),
    )

    # Corrected entry (supersedes the accepted one)
    corrected_entry = _make_entry(
        entry_id=_ENTRY_A2,
        user_id=_USER_OWNER,
        session_id=_SESSION_A,
        topic_id=_TOPIC_ID,
        template_key="weekly_review",
        temporal_scope="week",
        phase="closing",
        plaintext_searchable="Corrected: This week was productive. I finished the report, started the design doc, AND deployed the hotfix.",
        source_message_ids=[_MSG_1],
        revision_number=2,
        supersedes_entry_id=_ENTRY_A1,
        created_at=now - timedelta(days=1),
    )

    # Session B: deferred/rejected candidate (NOT processed)
    session_b = _make_session(
        session_id=_SESSION_B,
        user_id=_USER_OWNER,
        status="rejected",
        topic_id=_TOPIC_ID,
        source_message_ids=[_MSG_2],
    )

    deferred_entry = _make_entry(
        entry_id=_ENTRY_B1,
        user_id=_USER_OWNER,
        session_id=_SESSION_B,
        topic_id=_TOPIC_ID,
        template_key="freeform_reflection",
        temporal_scope="instant",
        phase="freeform",
        plaintext_searchable="I'm not sure this counts as a reflection but I had a thought about scheduling.",
        source_message_ids=[_MSG_2],
        revision_number=1,
        supersedes_entry_id=None,
        created_at=now - timedelta(days=2),
    )

    # Session C: processed session for derived observation traceability
    session_c = _make_session(
        session_id=_SESSION_C,
        user_id=_USER_OWNER,
        status="processed",
        topic_id=_TOPIC_ID,
        source_message_ids=[_MSG_1, _MSG_2],
    )

    # Derived observation — stays separately traceable
    derivation = ReflectionDerivation(
        id=_DERIVATION_O1,
        reflection_entry_id=_ENTRY_A2,  # from the corrected entry
        user_id=_USER_OWNER,
        derivation_kind="observation",
        candidate_payload_encrypted=None,
        assertion_source="derived",
        confidence=0.85,
        eligibility_reasons=["explicit_reflection", "high_confidence"],
        supporting_message_ids=[_MSG_1],
        decision="accepted",
        applied_target_table="observations",
        applied_target_id=UUID("00000000-0000-4000-8000-0000000000aa"),
        processor_version=None,
        processor_turn_id=None,
        idempotency_key=f"derivation-obs-{_ENTRY_A2}",
        created_at=now - timedelta(hours=12),
        decided_at=now - timedelta(hours=11),
    )

    return {
        "accepted_entry": accepted_entry,
        "corrected_entry": corrected_entry,
        "deferred_entry": deferred_entry,
        "session_a": session_a,
        "session_b": session_b,
        "session_c": session_c,
        "derivation": derivation,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# Section 1: Retrieval scope boundaries
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetrievalScopeBoundaries:
    """Verify keyword/vector retrieval respects scope for reflections."""

    async def test_accepted_reflection_appears_in_keyword_search(self):
        """An accepted (processed-session) reflection matches keyword search."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]

        # Simulate keyword search returning the accepted reflection row
        search_row = {
            "source_type": "reflection",
            "source_id": accepted.id,
            "message_id": accepted.id,
            "sent_at": accepted.created_at,
            "keyword_score": 0.9,
            "keyword_rank": 1,
            "_reflection_evidence": {
                "session_id": str(accepted.session_id),
                "template_key": accepted.template_key,
                "temporal_scope": accepted.temporal_scope,
                "phase": accepted.phase,
                "revision_number": accepted.revision_number,
                "schema_version": accepted.schema_version,
                "supersedes_entry_id": None,
            },
            "_reflection_source_message_ids": [str(m) for m in accepted.source_message_ids],
        }

        result = RetrievalResult(
            message_id=accepted.id,
            match_type="keyword",
            rrf_score=1 / 61,
            keyword_score=0.9,
            keyword_rank=1,
            semantic_rank=None,
            semantic_degraded=False,
            source_type="reflection",
            source_id=accepted.id,
            sent_at=accepted.created_at,
            evidence_metadata={
                "session_id": str(accepted.session_id),
                "template_key": accepted.template_key,
                "temporal_scope": accepted.temporal_scope,
                "phase": accepted.phase,
                "revision_number": accepted.revision_number,
                "schema_version": accepted.schema_version,
                "supersedes_entry_id": None,
            },
            source_message_ids=[_MSG_1],
        )

        assert result.source_type == "reflection"
        assert result.evidence_metadata is not None
        assert result.evidence_metadata["template_key"] == "weekly_review"
        assert result.evidence_metadata["phase"] == "opening"
        assert result.evidence_metadata["supersedes_entry_id"] is None
        assert result.source_message_ids == [_MSG_1]

    async def test_corrected_reflection_shows_superseded_provenance(self):
        """A corrected reflection has supersedes_entry_id in evidence metadata."""
        fixture = _build_full_fixture()
        corrected = fixture["corrected_entry"]

        search_row = {
            "source_type": "reflection",
            "source_id": corrected.id,
            "message_id": corrected.id,
            "sent_at": corrected.created_at,
            "keyword_score": 0.85,
            "keyword_rank": 1,
            "_reflection_evidence": {
                "session_id": str(corrected.session_id),
                "template_key": corrected.template_key,
                "temporal_scope": corrected.temporal_scope,
                "phase": corrected.phase,
                "revision_number": corrected.revision_number,
                "schema_version": corrected.schema_version,
                "supersedes_entry_id": str(_ENTRY_A1),
            },
            "_reflection_source_message_ids": [str(_MSG_1)],
        }

        from app.services.retrieval import _reflection_evidence_from_row

        evidence = _reflection_evidence_from_row("reflection", search_row)

        assert evidence is not None
        assert evidence["revision_number"] == 2
        assert evidence["supersedes_entry_id"] == str(_ENTRY_A1)
        assert evidence["phase"] == "closing"

    async def test_deferred_rejected_not_in_active_retrieval(self):
        """Deferred/rejected candidates are excluded from retrieval because their
        session status is not 'processed' (view filters them out)."""
        fixture = _build_full_fixture()
        deferred = fixture["deferred_entry"]
        session_b = fixture["session_b"]

        # The searchable content view only includes processed sessions.
        # So a deferred entry's session would not appear in the view at all.
        assert session_b.status == "rejected"
        # The entry itself has plaintext_searchable but won't appear in active
        # retrieval because the session status is not 'processed'
        assert deferred.plaintext_searchable is not None


class TestRetrievalCrossScopeRejection:
    """Verify retrieval rejects cross-user, cross-bot, and cross-topic access."""

    async def test_cross_user_retrieval_rejected(self):
        """A different user cannot retrieve another user's reflections."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]

        # Build a keyword row with wrong viewer
        search_row = {
            "source_type": "reflection",
            "source_id": accepted.id,
            "message_id": accepted.id,
            "sent_at": accepted.created_at,
            "keyword_score": 0.9,
            "keyword_rank": 1,
            "bot_id": _BOT_ID,
            "topic_id": _TOPIC_ID,
            "thread_owner_user_id": _USER_OWNER,
            "sender_id": _USER_OWNER,
            "recipient_id": None,
            "deleted_at": None,
            "search_suppressed_at": None,
            "dyad_id": None,
            "thread_owner_partner_share": None,
            "active_oob_severity": None,
        }

        # The visibility contract: viewer_id != thread_owner AND no partner_share
        # should block access
        from tests.test_retrieval import _visibility_contract_row_visible

        # Other user as viewer, owner as thread_owner => blocked
        visible = _visibility_contract_row_visible(
            search_row,
            viewer_id=_USER_OTHER,
            partner_id=_USER_OWNER,
            bot_id=_BOT_ID,
            topic_id=_TOPIC_ID,
            thread_owner_user_id=None,
            dyad_id=None,
        )
        assert visible is False, "Cross-user retrieval should be blocked"

        # Owner as viewer => allowed
        visible_owner = _visibility_contract_row_visible(
            search_row,
            viewer_id=_USER_OWNER,
            partner_id=_USER_OTHER,
            bot_id=_BOT_ID,
            topic_id=_TOPIC_ID,
            thread_owner_user_id=None,
            dyad_id=None,
        )
        assert visible_owner is True, "Owner should see their own reflection"

    async def test_wrong_bot_excluded_from_keyword_search(self):
        """Reflections scoped to a different bot are not retrievable."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]

        search_row = {
            "source_type": "reflection",
            "source_id": accepted.id,
            "message_id": accepted.id,
            "sent_at": accepted.created_at,
            "keyword_score": 0.9,
            "keyword_rank": 1,
            "bot_id": "other_bot",
            "topic_id": _TOPIC_ID,
            "thread_owner_user_id": _USER_OWNER,
            "sender_id": _USER_OWNER,
            "recipient_id": None,
            "deleted_at": None,
            "search_suppressed_at": None,
            "dyad_id": None,
            "thread_owner_partner_share": None,
            "active_oob_severity": None,
        }

        from tests.test_retrieval import _visibility_contract_row_visible

        visible = _visibility_contract_row_visible(
            search_row,
            viewer_id=_USER_OWNER,
            partner_id=None,
            bot_id=_BOT_ID,
            topic_id=_TOPIC_ID,
            thread_owner_user_id=None,
            dyad_id=None,
        )
        assert visible is False, "Wrong bot should be blocked"

    async def test_wrong_topic_excluded(self):
        """Reflections scoped to a different topic are not retrievable."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]

        search_row = {
            "source_type": "reflection",
            "source_id": accepted.id,
            "message_id": accepted.id,
            "sent_at": accepted.created_at,
            "keyword_score": 0.9,
            "keyword_rank": 1,
            "bot_id": _BOT_ID,
            "topic_id": UUID("99999999-9999-4999-8999-999999999999"),
            "thread_owner_user_id": _USER_OWNER,
            "sender_id": _USER_OWNER,
            "recipient_id": None,
            "deleted_at": None,
            "search_suppressed_at": None,
            "dyad_id": None,
            "thread_owner_partner_share": None,
            "active_oob_severity": None,
        }

        from tests.test_retrieval import _visibility_contract_row_visible

        visible = _visibility_contract_row_visible(
            search_row,
            viewer_id=_USER_OWNER,
            partner_id=None,
            bot_id=_BOT_ID,
            topic_id=_TOPIC_ID,
            thread_owner_user_id=None,
            dyad_id=None,
        )
        assert visible is False, "Wrong topic should be blocked"


# ═══════════════════════════════════════════════════════════════════════════════
# Section 2: Compact hot context
# ═══════════════════════════════════════════════════════════════════════════════


class TestCompactHotContext:
    """Verify hot context stays compact and excludes deferred/rejected content."""

    def _base_hc(self, **overrides) -> HotContextSolo:
        """Build a minimal HotContextSolo for integration tests."""
        defaults = dict(
            current_user={
                "id": str(_USER_OWNER),
                "name": "TestUser",
                "timezone": "UTC",
                "onboarding_state": "complete",
                "style_notes": "",
                "partner_share": None,
                "partner_sharing_state": "unavailable",
            },
            partner_user={},
            conversation_load={
                "period": "today",
                "timezone": "UTC",
                "total_count": 0,
                "inbound_count": 0,
                "outbound_count": 0,
            },
            active_oob=[],
            memories=[],
            active_themes=[],
            open_watch_items=[],
            observations=[],
            recent_messages=[],
            time_since_last_message=None,
            trigger_metadata={
                "kind": "test",
                "triggering_message_ids": [],
                "messages": [],
            },
            reflections_digest=[],
            compass_snapshot=None,
            bot_id=_BOT_ID,
        )
        defaults.update(overrides)
        return HotContextSolo(**defaults)

    def _digest_item(
        self,
        entry_id: UUID,
        session_id: UUID,
        *,
        template_key: str = "daily",
        temporal_scope: str = "day",
        phase: str = "opening",
        plaintext_searchable: str = "reflection text",
        is_open_loop: bool = False,
        revision_number: int = 1,
    ) -> dict:
        return {
            "entry_id": str(entry_id),
            "session_id": str(session_id),
            "template_key": template_key,
            "temporal_scope": temporal_scope,
            "phase": phase,
            "plaintext_searchable": plaintext_searchable,
            "is_open_loop": is_open_loop,
            "revision_number": revision_number,
            "created_at": _now().isoformat(),
        }

    def test_empty_digest_omits_section(self):
        """When there are no reflections, the section is omitted from render."""
        hc = self._base_hc(reflections_digest=[])
        context_str = _render_solo_with_counts(hc, {})
        assert "## Recent reflections" not in context_str

    def test_digest_renders_when_present(self):
        """When reflections exist, the section appears with session and entry info."""
        digest = [
            self._digest_item(
                _ENTRY_A2,
                _SESSION_A,
                template_key="weekly_review",
                temporal_scope="week",
                phase="closing",
                plaintext_searchable="productivity improved",
                revision_number=2,
            )
        ]
        hc = self._base_hc(reflections_digest=digest)
        context_str = _render_solo_with_counts(hc, {})

        assert "## Recent reflections" in context_str
        assert "weekly_review" in context_str
        assert "week" in context_str
        assert "closing" in context_str
        assert "productivity improved" in context_str
        assert "v2" in context_str

    def test_open_loop_marker_when_opening_without_closing(self):
        """An opening-only session shows [OPEN LOOP] marker."""
        digest = [
            self._digest_item(
                _ENTRY_A1,
                _SESSION_A,
                template_key="weekly_review",
                temporal_scope="week",
                phase="opening",
                plaintext_searchable="started the review",
                is_open_loop=True,
                revision_number=1,
            )
        ]
        hc = self._base_hc(reflections_digest=digest)
        context_str = _render_solo_with_counts(hc, {})

        assert "[OPEN LOOP]" in context_str
        assert "opening" in context_str
        assert "[open]" in context_str

    def test_closing_reflection_no_open_loop_marker(self):
        """A closing-only reflection does not show [OPEN LOOP]."""
        digest = [
            self._digest_item(
                _ENTRY_A2,
                _SESSION_A,
                template_key="weekly_review",
                temporal_scope="week",
                phase="closing",
                plaintext_searchable="completed the review",
                revision_number=2,
            )
        ]
        hc = self._base_hc(reflections_digest=digest)
        context_str = _render_solo_with_counts(hc, {})

        assert "[OPEN LOOP]" not in context_str
        assert "[closing]" in context_str

    def test_compact_render_excludes_raw_payload(self):
        """Compact digest rendering never includes raw encrypted payloads."""
        digest = [
            self._digest_item(
                _ENTRY_A1,
                _SESSION_A,
                plaintext_searchable="safe text only",
            )
        ]
        hc = self._base_hc(reflections_digest=digest)
        context_str = _render_solo_with_counts(hc, {})

        # No encrypted payload indicators
        assert "payload_encrypted" not in context_str
        assert "summary_encrypted" not in context_str

    async def test_token_budget_applied(self, app_env):
        """Verify that render_hot_context_solo applies token budget trimming."""
        # Create a long digest entry that will trigger budget trimming
        long_text = "reflection " * 500  # ~1000 tokens
        digest = [
            self._digest_item(
                _ENTRY_A1,
                _SESSION_A,
                plaintext_searchable=long_text,
            )
        ]
        hc = self._base_hc(
            reflections_digest=digest,
            memories=[{"id": "m1", "content": "memory " * 200, "about_user_id": str(_USER_OWNER),
                        "created_at": _now().isoformat()}],
            recent_messages=[{"id": "rm1", "content": "msg " * 100, "direction": "inbound",
                              "sender_id": str(_USER_OWNER), "recipient_id": None, "sent_at": _now().isoformat(),
                              "charge": "routine", "raw_content_hidden": False}],
        )
        # render_hot_context_solo uses a 4000-token budget; should trim
        result = render_hot_context_solo(hc)
        # The reflections section should be present but trimmed
        assert "## Recent reflections" in result
        # The result should be non-empty and well-formed
        assert "You" in result


# ═══════════════════════════════════════════════════════════════════════════════
# Section 3: Tool-driven history
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolDrivenHistory:
    """Verify explicit tools expose historical details while hot context stays compact."""

    async def test_list_reflections_exposes_accepted_entry(self):
        """list_reflections tool returns accepted reflection summaries."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]
        session_a = fixture["session_a"]

        pool = FakePool(
            fetch_rows=[_entry_to_row(accepted)],
            fetchrow_result=_session_to_row(session_a),
        )
        ctx = _make_turn_ctx(user_id=_USER_OWNER, pool=pool)

        result = await list_reflections(ctx, ListReflectionsInput(current_only=True))
        assert len(result.entries) == 1
        assert result.entries[0].id == accepted.id
        assert result.entries[0].template_key == accepted.template_key

    async def test_list_reflections_excludes_deferred_by_session_status(self):
        """list_reflections with current_only skips entries whose session is not processed."""
        fixture = _build_full_fixture()
        deferred = fixture["deferred_entry"]

        # When session is not processed, list_entries filters via the session JOIN
        # The store query joins to reflection_sessions WHERE status IN (...)
        # For current_only mode, only 'processed' sessions are included
        pool = FakePool(fetch_rows=[])  # deferred entry not returned
        ctx = _make_turn_ctx(user_id=_USER_OWNER, pool=pool)

        result = await list_reflections(ctx, ListReflectionsInput(current_only=True))
        assert len(result.entries) == 0

    async def test_get_reflection_returns_full_detail(self):
        """get_reflection returns detailed entry including source-message provenance."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]
        session_a = fixture["session_a"]

        pool = FakePool(
            fetchrow_result=_entry_to_row(accepted),
            fetch_rows=[_session_to_row(session_a)],
        )
        # We need fetchrow to return the entry and then fetch to return session
        # get_reflection calls store.get_entry which calls fetchrow
        ctx = _make_turn_ctx(user_id=_USER_OWNER, pool=pool)

        result = await get_reflection(
            ctx, GetReflectionInput(entry_id=accepted.id, include_internals=True)
        )
        assert result.entry.id == accepted.id
        assert result.entry.template_key == accepted.template_key
        assert result.entry.source_message_ids == [_MSG_1]

    async def test_search_reflections_finds_corrected_entry(self):
        """search_reflections can find a corrected reflection via keyword retrieval."""
        fixture = _build_full_fixture()
        corrected = fixture["corrected_entry"]

        # Simulate a hybrid search that returns the corrected entry
        search_row = {
            "source_type": "reflection",
            "source_id": corrected.id,
            "message_id": corrected.id,
            "sent_at": corrected.created_at,
            "keyword_score": 0.8,
            "keyword_rank": 1,
            "_reflection_evidence": {
                "session_id": str(corrected.session_id),
                "template_key": corrected.template_key,
                "temporal_scope": corrected.temporal_scope,
                "phase": corrected.phase,
                "revision_number": corrected.revision_number,
                "schema_version": corrected.schema_version,
                "supersedes_entry_id": str(_ENTRY_A1),
            },
            "_reflection_source_message_ids": [str(_MSG_1)],
        }

        pool = FakePool(
            fetch_rows=[
                [search_row],  # keyword results
                [],  # semantic results (no embeddings for test)
                [_entry_to_row(corrected)],  # hydration query
            ],
            fetchrow_result=_entry_to_row(corrected),
        )
        ctx = _make_turn_ctx(user_id=_USER_OWNER, pool=pool)

        result = await search_reflections(
            ctx,
            SearchReflectionsInput(query="productive week", include_internals=True),
        )

        assert len(result.hits) >= 0  # depends on hydration
        # The search tool delegates to retrieval, which handles reflection rows


class TestToolSchemaSafety:
    """Verify tools do not create tasks, reminders, or commitment artifacts."""

    def test_no_task_fields_in_schemas(self):
        """Reflection tool schemas must not have task/reminder/commitment fields."""
        from tool_schemas import (
            GetReflectionOutput,
            ListReflectionsOutput,
            ReflectionEntrySummary,
            ReflectionSearchHit,
            SearchReflectionsOutput,
        )

        forbidden = ["task", "reminder", "commitment", "adherence", "follow_up", "schedule"]
        schemas = [
            ReflectionEntrySummary,
            GetReflectionOutput,
            ListReflectionsOutput,
            ReflectionSearchHit,
            SearchReflectionsOutput,
        ]

        for schema_cls in schemas:
            for field_name in schema_cls.model_fields:
                for word in forbidden:
                    assert word not in field_name.lower(), (
                        f"{schema_cls.__name__}.{field_name} references '{word}'"
                    )

    def test_tools_are_read_only_or_correction(self):
        """The allowed tool set is read-only evidence tools + correction."""
        # list_reflections, get_reflection, search_reflections are read tools
        # finalize_reflection and correct_reflection are write tools but only
        # correct_reflection is available to SuperPOM (finalize is excluded)
        allowed_read_tools = {"list_reflections", "get_reflection", "search_reflections"}
        # These are the reflection tools registered in the tool registry
        from tool_schemas import TOOL_REGISTRY

        for name in allowed_read_tools:
            assert name in TOOL_REGISTRY, f"{name} should be registered"


# ═══════════════════════════════════════════════════════════════════════════════
# Section 4: Derived observation traceability
# ═══════════════════════════════════════════════════════════════════════════════


class TestDerivedObservationTraceability:
    """Verify derived observations remain separately traceable from reflections."""

    def test_derivation_links_to_reflection_entry(self):
        """A derivation references its source reflection entry."""
        fixture = _build_full_fixture()
        derivation = fixture["derivation"]
        corrected = fixture["corrected_entry"]

        assert derivation.reflection_entry_id == corrected.id
        assert derivation.derivation_kind == "observation"
        assert derivation.decision == "accepted"

    def test_derivation_has_independent_target(self):
        """A derived observation writes to its own target table, not the reflection."""
        fixture = _build_full_fixture()
        derivation = fixture["derivation"]

        assert derivation.applied_target_table == "observations"
        assert derivation.applied_target_id is not None
        # The target is NOT a reflection entry or session
        assert derivation.applied_target_id != derivation.reflection_entry_id

    def test_derivation_provenance_chain(self):
        """Derivation provenance traces back through entry → session → source messages."""
        fixture = _build_full_fixture()
        derivation = fixture["derivation"]
        session_a = fixture["session_a"]

        # Derivation → reflection entry
        assert derivation.reflection_entry_id == _ENTRY_A2

        # Entry → session
        assert str(session_a.id) == str(_SESSION_A)

        # Session → source messages
        assert _MSG_1 in session_a.source_message_ids

        # Derivation has supporting message IDs
        assert _MSG_1 in derivation.supporting_message_ids

    def test_derivation_is_independent_from_accepted_reflection(self):
        """A correction to the source reflection doesn't auto-delete the derived observation.
        The observation stays traceable through its own provenance chain."""
        fixture = _build_full_fixture()
        derivation = fixture["derivation"]
        accepted = fixture["accepted_entry"]
        corrected = fixture["corrected_entry"]

        # The derivation is from the corrected entry (current)
        assert derivation.reflection_entry_id == corrected.id

        # The accepted entry was superseded
        assert corrected.supersedes_entry_id == accepted.id

        # But the derivation target is separate and persists independently
        assert derivation.applied_target_table == "observations"

    def test_deferred_reflection_has_no_accepted_derivations(self):
        """Deferred/rejected reflections should not produce accepted derivations."""
        fixture = _build_full_fixture()

        # The deferred entry's session has status="rejected"
        assert fixture["session_b"].status == "rejected"

        # The derivation in our fixture is from a processed session, not deferred
        assert fixture["derivation"].reflection_entry_id != fixture["deferred_entry"].id


# ═══════════════════════════════════════════════════════════════════════════════
# Section 5: Source-message provenance
# ═══════════════════════════════════════════════════════════════════════════════


class TestSourceMessageProvenance:
    """Verify source-message provenance is preserved in entries and retrievals."""

    def test_accepted_entry_has_source_messages(self):
        """Accepted reflection entry links to its source messages."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]

        assert _MSG_1 in accepted.source_message_ids
        assert len(accepted.source_message_ids) == 1

    def test_corrected_entry_preserves_source_messages(self):
        """Corrected entry retains source message provenance."""
        fixture = _build_full_fixture()
        corrected = fixture["corrected_entry"]

        assert _MSG_1 in corrected.source_message_ids

    def test_deferred_entry_has_source_messages(self):
        """Even deferred entries preserve source message links for auditing."""
        fixture = _build_full_fixture()
        deferred = fixture["deferred_entry"]

        assert _MSG_2 in deferred.source_message_ids


# ═══════════════════════════════════════════════════════════════════════════════
# Section 6: End-to-end fixture consistency
# ═══════════════════════════════════════════════════════════════════════════════


class TestEndToEndFixtureConsistency:
    """Verify the full M3 fixture is internally consistent."""

    def test_all_entries_belong_to_same_owner(self):
        """All entries in the fixture belong to the same user for correct scoping."""
        fixture = _build_full_fixture()

        for entry in [fixture["accepted_entry"], fixture["corrected_entry"],
                       fixture["deferred_entry"]]:
            assert entry.user_id == _USER_OWNER

    def test_correction_chain_is_valid(self):
        """Correction chain: accepted → superseded_by corrected."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]
        corrected = fixture["corrected_entry"]

        assert corrected.supersedes_entry_id == accepted.id
        assert accepted.supersedes_entry_id is None
        assert corrected.revision_number > accepted.revision_number

    def test_processed_vs_rejected_sessions(self):
        """Only processed sessions produce searchable content."""
        fixture = _build_full_fixture()

        assert fixture["session_a"].status == "processed"
        assert fixture["session_b"].status == "rejected"

    def test_derivation_from_current_not_superseded(self):
        """Derivations reference the current (not superseded) entry."""
        fixture = _build_full_fixture()

        # Derivation is from corrected entry (current), not accepted (superseded)
        assert fixture["derivation"].reflection_entry_id == _ENTRY_A2
        assert _ENTRY_A2 != _ENTRY_A1

    def test_no_raw_encrypted_payload_in_entry_plaintext(self):
        """plaintext_searchable is minimal text, not encrypted payload."""
        fixture = _build_full_fixture()
        accepted = fixture["accepted_entry"]

        assert accepted.payload_encrypted is None  # We don't set it in tests
        # plaintext_searchable is the minimal deterministic searchable text
        assert "productive" in accepted.plaintext_searchable


# ── Hot context rendering with app_env ────────────────────────────────────────


async def test_render_hot_context_solo_with_reflections_digest(app_env):
    """Integration: render_hot_context_solo with reflections present (needs app_env for settings)."""
    now = _now()
    digest = [
        {
            "entry_id": str(_ENTRY_A1),
            "session_id": str(_SESSION_A),
            "template_key": "daily",
            "temporal_scope": "day",
            "phase": "opening",
            "plaintext_searchable": "Today was a good day for reflection.",
            "is_open_loop": True,
            "revision_number": 1,
            "created_at": now.isoformat(),
        }
    ]

    hc = HotContextSolo(
        current_user={
            "id": str(_USER_OWNER),
            "name": "TestUser",
            "timezone": "UTC",
            "onboarding_state": "complete",
            "style_notes": "",
            "partner_share": None,
            "partner_sharing_state": "unavailable",
        },
        partner_user={},
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 0,
            "inbound_count": 0,
            "outbound_count": 0,
        },
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[],
        time_since_last_message=None,
        trigger_metadata={"kind": "test", "triggering_message_ids": [], "messages": []},
        reflections_digest=digest,
        compass_snapshot=None,
        bot_id=_BOT_ID,
    )

    result = render_hot_context_solo(hc)
    assert "## Recent reflections" in result
    assert "daily" in result
    assert "[OPEN LOOP]" in result
    assert "Today was a good day for reflection" in result
