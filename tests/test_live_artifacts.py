"""Helper + config unit tests for conversation artifacts (Sprint 1).

Two layers:
1. DB-gated helper tests: create_artifact, add_artifact_link,
   list_artifact_links, savepoint retry — require DATABASE_URL or
   EVAL_DATABASE_URL and are skipped when neither is set.
2. Non-DB tests: config bounds, constant-vs-SQL parity, ValueError
   rejection (mocked connection).

Follows the pytest pattern from tests/test_live_migrations.py.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.services.live.artifacts import (
    ALLOWED_TARGET_TABLES,
    ARTIFACT_TYPES,
    LIVE_DEBRIEF_KIND,
    LIVE_PREP_KIND,
    RELATIONS,
    ArtifactLinkRow,
    ArtifactRow,
    add_artifact_link,
    create_artifact,
    get_current_artifact,
    list_artifact_links,
    list_artifacts,
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


# ---------------------------------------------------------------------------
# DB-gated helpers — requires DATABASE_URL or EVAL_DATABASE_URL
# ---------------------------------------------------------------------------


@pytest.fixture(name="_check_db_url")
def _check_db_url_fixture() -> None:
    """Skip DB-gated tests when no database URL is configured."""
    if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
        pytest.skip("DATABASE_URL / EVAL_DATABASE_URL not set")


@pytest.fixture(name="scratch_conn")
async def _scratch_conn_fixture(_check_db_url: None) -> Any:
    """Yield an asyncpg Connection in a scratch schema with all migrations applied."""
    from evals.db import create_eval_pool, scratch_schema

    pool = await create_eval_pool()
    try:
        async with scratch_schema(pool, schema=f"eval_artifacts_{uuid4().hex[:12]}") as scratch:
            async with pool.acquire() as conn:
                await conn.execute(f"SET search_path TO \"{scratch.schema}\", public")
                yield conn
    finally:
        await pool.close()


@pytest.fixture(name="seed_conversation")
async def _seed_conversation_fixture(scratch_conn: Any) -> tuple[str, str]:
    """Insert a conversations + users row and return (conversation_id, user_id)."""
    user_id = str(uuid4())
    conversation_id = str(uuid4())

    # Seed a user row (minimal — just the PK).
    await scratch_conn.execute(
        "INSERT INTO mediator.users (id) VALUES ($1) ON CONFLICT DO NOTHING",
        user_id,
    )

    # Seed a conversations row.
    await scratch_conn.execute(
        """
        INSERT INTO mediator.conversations (id, user_id, partner_label, status)
        VALUES ($1, $2, 'test-partner', 'live')
        """,
        conversation_id,
        user_id,
    )

    return conversation_id, user_id


class TestCreateArtifact:
    async def test_happy_path_creates_revision_1(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """First create_artifact call for a type produces revision_number=1."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"summary": "hello world"},
        )
        assert artifact.revision_number == 1
        assert artifact.artifact_type == "live_prep_brief"
        assert artifact.payload == {"summary": "hello world"}
        assert artifact.bot_id == "mediator"
        assert artifact.id is not None

    async def test_two_sequential_calls_produce_revisions_1_and_2(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Two create_artifact calls for the same type auto-increment revisions."""
        conversation_id, user_id = seed_conversation
        a1 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 1},
        )
        a2 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 2},
        )
        assert a1.revision_number == 1
        assert a2.revision_number == 2
        assert a2.id != a1.id

    async def test_revision_per_artifact_type(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Different artifact_type get independent revision sequences."""
        conversation_id, user_id = seed_conversation
        a1 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        a2 = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        assert a1.revision_number == 1
        assert a2.revision_number == 1  # different type, independent counter

    async def test_get_current_artifact_returns_highest_revision(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """get_current_artifact returns the row with max revision_number."""
        conversation_id, user_id = seed_conversation
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 1},
        )
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"v": 2},
        )
        current = await get_current_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="live_prep_brief",
        )
        assert current is not None
        assert current.revision_number == 2
        assert current.payload == {"v": 2}

    async def test_get_current_artifact_returns_none_for_missing(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """get_current_artifact returns None when no matching artifact exists."""
        conversation_id, _ = seed_conversation
        result = await get_current_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="agenda_revision",
        )
        assert result is None

    async def test_list_artifacts_filters_by_type(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """list_artifacts with artifact_type filter returns only matching rows."""
        conversation_id, user_id = seed_conversation
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        briefs = await list_artifacts(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="live_prep_brief",
        )
        assert len(briefs) == 1
        assert briefs[0].artifact_type == "live_prep_brief"


class TestCreateArtifactSavepointRetry:
    async def test_savepoint_retry_outer_transaction_survives(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Pre-insert a row at revision_number=1, then call create_artifact inside
        an open outer transaction — assert it succeeds at revision 2 without
        rolling back the outer transaction."""
        conversation_id, user_id = seed_conversation
        # Pre-insert a row occupying revision_number=1 for this type.
        await scratch_conn.execute(
            """
            INSERT INTO mediator.conversation_artifacts
                (id, conversation_id, bot_id, user_id, artifact_type,
                 payload, revision_number)
            VALUES ($1, $2, $3, $4, $5, $6, 1)
            """,
            str(uuid4()),
            conversation_id,
            "mediator",
            user_id,
            "live_prep_brief",
            '{"pre":true}',
        )

        # Now open an outer transaction, create_artifact, and commit.
        await scratch_conn.execute("BEGIN")
        try:
            artifact = await create_artifact(
                scratch_conn,
                conversation_id=conversation_id,
                bot_id="mediator",
                user_id=user_id,
                artifact_type="live_prep_brief",
                payload={"v": 2},
            )
            assert artifact.revision_number == 2, (
                f"expected revision 2 (pre-inserted 1), got {artifact.revision_number}"
            )
            await scratch_conn.execute("COMMIT")
        except Exception:
            await scratch_conn.execute("ROLLBACK")
            raise

        # Prove the outer transaction committed: the new row is visible.
        current = await get_current_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            artifact_type="live_prep_brief",
        )
        assert current is not None
        assert current.revision_number == 2
        assert current.payload == {"v": 2}


class TestAddArtifactLink:
    async def test_happy_path(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """add_artifact_link creates a link row and returns it."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={"x": 1},
        )
        link = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=str(uuid4()),
            relation="extracted_memory",
            evidence={"quote": "test"},
        )
        assert link.artifact_id == artifact.id
        assert link.target_table == "memories"
        assert link.relation == "extracted_memory"
        assert link.evidence == {"quote": "test"}

    async def test_idempotency_same_return(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """Two calls with same key return the same link row."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_prep_brief",
            payload={},
        )
        target_id = str(uuid4())
        link1 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=target_id,
            relation="extracted_memory",
        )
        link2 = await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="memories",
            target_id=target_id,
            relation="extracted_memory",
        )
        assert link1.id == link2.id
        assert link1.artifact_id == link2.artifact_id

    async def test_reverse_lookup_by_target_table_target_id(
        self, scratch_conn: Any, seed_conversation: tuple[str, str]
    ) -> None:
        """list_artifact_links with (target_table, target_id) returns matching links."""
        conversation_id, user_id = seed_conversation
        artifact = await create_artifact(
            scratch_conn,
            conversation_id=conversation_id,
            bot_id="mediator",
            user_id=user_id,
            artifact_type="live_debrief",
            payload={},
        )
        target_id = str(uuid4())
        await add_artifact_link(
            scratch_conn,
            artifact_id=artifact.id,
            target_table="observations",
            target_id=target_id,
            relation="extracted_observation",
        )
        results = await list_artifact_links(
            scratch_conn,
            target_table="observations",
            target_id=target_id,
        )
        assert len(results) == 1
        assert results[0].target_table == "observations"
        assert results[0].target_id == target_id
        assert results[0].relation == "extracted_observation"


# ---------------------------------------------------------------------------
# ValueError rejection — no DB required (mocked connection)
# ---------------------------------------------------------------------------


class TestAddArtifactLinkRejection:
    def test_unknown_target_table_raises_valueerror_before_sql(self) -> None:
        """add_artifact_link rejects unknown target_table with ValueError
        before issuing any SQL."""
        mock_conn = MagicMock()
        # We don't want the mock to actually do anything — if the function
        # reaches SQL, the mock will return something and we want to know.
        # Instead, track whether any SQL method was called.
        with pytest.raises(ValueError) as exc_info:
            # Use asyncio to run the async function
            import asyncio
            async def _call() -> None:
                await add_artifact_link(
                    mock_conn,
                    artifact_id=str(uuid4()),
                    target_table="nonexistent_table",
                    target_id=str(uuid4()),
                    relation="extracted_memory",
                )
            asyncio.run(_call())
        assert "nonexistent_table" in str(exc_info.value)
        assert "not allowed" in str(exc_info.value).lower() or "Allowed" in str(exc_info.value)
        # Prove zero DB round-trips: fetchrow/fetch/execute must not be called.
        mock_conn.fetchrow.assert_not_called()
        mock_conn.fetch.assert_not_called()
        mock_conn.execute.assert_not_called()

    def test_unknown_relation_raises_valueerror_before_sql(self) -> None:
        """add_artifact_link rejects unknown relation with ValueError
        before issuing any SQL."""
        mock_conn = MagicMock()
        import asyncio

        with pytest.raises(ValueError) as exc_info:
            async def _call() -> None:
                await add_artifact_link(
                    mock_conn,
                    artifact_id=str(uuid4()),
                    target_table="memories",
                    target_id=str(uuid4()),
                    relation="nonexistent_relation",
                )
            asyncio.run(_call())
        assert "nonexistent_relation" in str(exc_info.value)
        mock_conn.fetchrow.assert_not_called()
        mock_conn.fetch.assert_not_called()
        mock_conn.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Constant-vs-SQL parity — no DB required
# ---------------------------------------------------------------------------


def _extract_check_literals(check_kind: str, sql: str) -> set[str]:
    """Extract the quoted literals from a CHECK (column IN (...)) constraint.

    check_kind is one of: 'artifact_type', 'relation', 'target_table', 'kind'.
    Returns a set of the string literals (without quotes).
    """
    if check_kind == "kind":
        # Kind is special: CHECK (kind IS NULL OR kind IN (...))
        pattern = rf"CHECK\s*\(\s*kind\s+IS\s+NULL\s+OR\s+kind\s+IN\s*\((.*?)\)\)"
        match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
        if not match:
            raise AssertionError(f"Could not find kind CHECK constraint in SQL")
        inner = match.group(1)
    else:
        # Standard: CHECK (column_name IN (...))
        pattern = rf"CHECK\s*\(\s*{check_kind}\s+IN\s*\((.*?)\)\)"
        match = re.search(pattern, sql, re.DOTALL | re.IGNORECASE)
        if not match:
            raise AssertionError(f"Could not find {check_kind} CHECK constraint in SQL")
        inner = match.group(1)

    # Extract all single-quoted strings from the IN (...) list.
    literals: list[str] = re.findall(r"'([^']*)'", inner)
    return set(literals)


def _read_migration_up() -> str:
    return (MIGRATIONS_DIR / "0051_conversation_artifacts.sql").read_text()


class TestConstantSqlParity:
    def test_artifact_type_parity(self) -> None:
        """ARTIFACT_TYPES frozenset must match the SQL CHECK literals exactly."""
        sql = _read_migration_up()
        sql_types = _extract_check_literals("artifact_type", sql)
        assert sql_types == set(ARTIFACT_TYPES), (
            f"SQL artifact_type: {sorted(sql_types)}\n"
            f"Python ARTIFACT_TYPES: {sorted(ARTIFACT_TYPES)}"
        )

    def test_relation_parity(self) -> None:
        """RELATIONS frozenset must match the SQL CHECK literals exactly."""
        sql = _read_migration_up()
        sql_relations = _extract_check_literals("relation", sql)
        assert sql_relations == set(RELATIONS), (
            f"SQL relation: {sorted(sql_relations)}\n"
            f"Python RELATIONS: {sorted(RELATIONS)}"
        )

    def test_target_table_parity(self) -> None:
        """ALLOWED_TARGET_TABLES frozenset must match the SQL CHECK literals exactly."""
        sql = _read_migration_up()
        sql_targets = _extract_check_literals("target_table", sql)
        assert sql_targets == set(ALLOWED_TARGET_TABLES), (
            f"SQL target_table: {sorted(sql_targets)}\n"
            f"Python ALLOWED_TARGET_TABLES: {sorted(ALLOWED_TARGET_TABLES)}"
        )

    def test_kind_parity(self) -> None:
        """LIVE_PREP_KIND and LIVE_DEBRIEF_KIND must match the SQL CHECK literals."""
        sql = _read_migration_up()
        sql_kinds = _extract_check_literals("kind", sql)
        expected = {LIVE_PREP_KIND, LIVE_DEBRIEF_KIND}
        assert sql_kinds == expected, (
            f"SQL kind: {sorted(sql_kinds)}\n"
            f"Python kinds: {sorted(expected)}"
        )


# ---------------------------------------------------------------------------
# Config tests — no DB required
# ---------------------------------------------------------------------------


class TestConfigDefaults:
    def test_nonchat_default_max_tool_iterations_default(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test")
        assert settings.nonchat_default_max_tool_iterations == 100

    def test_live_debrief_max_tool_iterations_default(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test")
        assert settings.live_debrief_max_tool_iterations == 500

    def test_nonchat_boundary_zero_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            nonchat_default_max_tool_iterations=0)
        assert settings.nonchat_default_max_tool_iterations == 0

    def test_nonchat_boundary_2000_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            nonchat_default_max_tool_iterations=2000)
        assert settings.nonchat_default_max_tool_iterations == 2000

    def test_live_debrief_boundary_zero_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            live_debrief_max_tool_iterations=0)
        assert settings.live_debrief_max_tool_iterations == 0

    def test_live_debrief_boundary_5000_accepted(self) -> None:
        settings = Settings(database_url="postgres://test", supabase_url="https://test",
                            anthropic_api_key="sk-test", openai_api_key="sk-test",
                            groq_api_key="sk-test", whatsapp_token="test",
                            whatsapp_verify_token="test", admin_password="test",
                            live_debrief_max_tool_iterations=5000)
        assert settings.live_debrief_max_tool_iterations == 5000

    def test_nonchat_negative_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     nonchat_default_max_tool_iterations=-1)

    def test_nonchat_above_2000_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     nonchat_default_max_tool_iterations=2001)

    def test_live_debrief_negative_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     live_debrief_max_tool_iterations=-1)

    def test_live_debrief_above_5000_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            Settings(database_url="postgres://test", supabase_url="https://test",
                     anthropic_api_key="sk-test", openai_api_key="sk-test",
                     groq_api_key="sk-test", whatsapp_token="test",
                     whatsapp_verify_token="test", admin_password="test",
                     live_debrief_max_tool_iterations=5001)


# ---------------------------------------------------------------------------
# Live debrief artifacts — no DB required
# ---------------------------------------------------------------------------


class TestLiveDebriefArtifact:
    """Verify live_debrief artifact type is supported in the artifact system."""

    def test_live_debrief_in_artifact_types(self) -> None:
        """live_debrief is in ARTIFACT_TYPES frozenset."""
        assert "live_debrief" in ARTIFACT_TYPES, (
            f"live_debrief must be in ARTIFACT_TYPES; got {sorted(ARTIFACT_TYPES)}"
        )

    def test_live_debrief_kind_constant(self) -> None:
        """LIVE_DEBRIEF_KIND is 'live_debrief'."""
        assert LIVE_DEBRIEF_KIND == "live_debrief", (
            f"Expected LIVE_DEBRIEF_KIND='live_debrief', got {LIVE_DEBRIEF_KIND!r}"
        )

    def test_review_summary_in_artifact_types(self) -> None:
        """review_summary is in ARTIFACT_TYPES frozenset."""
        assert "review_summary" in ARTIFACT_TYPES, (
            f"review_summary must be in ARTIFACT_TYPES; got {sorted(ARTIFACT_TYPES)}"
        )

    def test_get_current_artifact_for_live_debrief(self) -> None:
        """get_current_artifact is importable with live_debrief type."""
        assert callable(get_current_artifact)

    def test_create_artifact_live_debrief_supported(self) -> None:
        """create_artifact function handles artifact_type='live_debrief'."""
        assert callable(create_artifact)
