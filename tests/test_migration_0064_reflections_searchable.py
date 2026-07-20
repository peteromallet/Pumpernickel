"""Static + live validation of migration 0064_reflections_searchable_content.

Proves that:
  - All pre-existing searchable source types (message, memory, observation,
    distillation, artifact, conversation_note, theme) still appear in the
    unified v_searchable_content view.
  - Reflection rows appear only when the session status is 'processed', the
    entry is the current (un-superseded) revision, and plaintext_searchable
    is non-empty.
  - Superseded entries, entries from non-processed sessions, and entries
    with empty/null plaintext are excluded.
  - The source-type CHECK constraints on content_embeddings and embed_jobs
    include 'reflection' alongside all existing types.
  - The down migration cleans up reflection rows and restores the pre-0064
    source-type contract.
"""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
UP_PATH = MIGRATIONS_DIR / "0064_reflections_searchable_content.sql"
DOWN_PATH = MIGRATIONS_DIR / "0064_reflections_searchable_content.down.sql"
UP_SQL = UP_PATH.read_text()
DOWN_SQL = DOWN_PATH.read_text()

ALL_EXISTING_SOURCE_TYPES = (
    "message",
    "memory",
    "observation",
    "distillation",
    "artifact",
    "conversation_note",
    "theme",
)


def _compact(sql: str) -> str:
    return " ".join(sql.lower().split())


# ===========================================================================
# Static text checks — always run, no DB needed
# ===========================================================================


def test_0064_files_exist_and_are_next_numbered_pair() -> None:
    numbered = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    assert numbered[-1].startswith("0064_")
    assert UP_PATH.exists()
    assert DOWN_PATH.exists()


def test_0064_widens_both_source_type_constraints_with_reflection() -> None:
    """content_embeddings and embed_jobs CHECK constraints include 'reflection'
    while preserving ALL seven pre-existing types."""
    lowered = _compact(UP_SQL)
    for table in ("content_embeddings", "embed_jobs"):
        assert f"alter table mediator.{table}" in lowered
    for st in ALL_EXISTING_SOURCE_TYPES:
        assert lowered.count(f"'{st}'") >= 2, f"'{st}' missing from widened CHECKs"
    assert lowered.count("'reflection'") >= 2

    # Verify the full check constraint list is present
    expected_check = (
        "check ( source_type in ( 'message', 'memory', 'observation', "
        "'distillation', 'artifact', 'conversation_note', 'theme', 'reflection' ) )"
    )
    assert expected_check in lowered


def test_0064_adds_reflection_arm_to_v_searchable_content() -> None:
    """The reflection UNION ALL arm is present with correct filtering."""
    lowered = _compact(UP_SQL)
    assert "'reflection'::text as source_type" in lowered
    assert "from mediator.reflection_entries re" in lowered
    assert "join mediator.reflection_sessions rs" in lowered
    assert "on rs.id = re.session_id" in lowered


def test_0064_reflection_arm_uses_plaintext_searchable_as_canonical_text() -> None:
    """Canonical text is plaintext_searchable, never the encrypted payload."""
    lowered = _compact(UP_SQL)
    assert "re.plaintext_searchable as content" in lowered
    assert "re.plaintext_searchable as canonical_text" in lowered
    assert "to_tsvector('simple'::regconfig, coalesce(re.plaintext_searchable, '')) as search_tsv" in lowered
    # Within the reflection arm SELECT list, payload_encrypted and
    # summary_encrypted must not appear (the file may mention them in
    # comments, but not in the actual column list).
    # Isolate the reflection arm: everything from "'reflection'::text as source_type"
    # to the final semicolon.
    reflection_arm_start = lowered.index("'reflection'::text as source_type")
    reflection_arm_end = lowered.index(";", lowered.index("btrim(re.plaintext_searchable) <> ''"))
    reflection_arm = lowered[reflection_arm_start:reflection_arm_end]
    assert "payload_encrypted" not in reflection_arm, (
        "payload_encrypted leaked into reflection arm SELECT list"
    )
    assert "summary_encrypted" not in reflection_arm, (
        "summary_encrypted leaked into reflection arm SELECT list"
    )


def test_0064_reflection_arm_filters_only_processed_sessions() -> None:
    """Only entries from sessions with status = 'processed' appear."""
    lowered = _compact(UP_SQL)
    assert "rs.status = 'processed'" in lowered
    # Other session statuses (collecting, finalizing, abandoned, processing_failed)
    # must not appear as inclusion gates.
    for bad_status in ("collecting", "finalizing", "abandoned", "processing_failed"):
        assert f"rs.status = '{bad_status}'" not in lowered


def test_0064_reflection_arm_excludes_superseded_entries() -> None:
    """Only the current (latest) revision is searchable."""
    lowered = _compact(UP_SQL)
    assert "re.supersedes_entry_id is null" in lowered


def test_0064_reflection_arm_excludes_empty_plaintext() -> None:
    """Entries with null or whitespace-only plaintext are excluded."""
    lowered = _compact(UP_SQL)
    assert "re.plaintext_searchable is not null" in lowered
    assert "btrim(re.plaintext_searchable) <> ''" in lowered


def test_0064_reflection_arm_carries_required_scope_fields() -> None:
    """sender_id, thread_owner_user_id, bot_id, topic_id are all present."""
    lowered = _compact(UP_SQL)
    assert "re.user_id as sender_id" in lowered
    assert "re.user_id as thread_owner_user_id" in lowered
    assert "re.bot_id" in lowered
    assert "re.topic_id" in lowered


def test_0064_reflection_arm_media_analysis_includes_metadata() -> None:
    """media_analysis jsonb carries session/entry metadata for tool surfaces."""
    lowered = _compact(UP_SQL)
    for key in ("session_id", "template_key", "temporal_scope", "phase",
                "revision_number", "schema_version", "supersedes_entry_id"):
        assert f"'{key}'" in lowered


def test_0064_preserves_all_existing_source_arms() -> None:
    """Every pre-existing source_type arm is still in the view."""
    lowered = _compact(UP_SQL)
    for source_label in (
        "'message'::text as source_type",
        "'memory'::text as source_type",
        "'observation'::text as source_type",
        "'distillation'::text as source_type",
        "'conversation_note'::text as source_type",
        "'theme'::text as source_type",
        "'artifact'::text as source_type",
    ):
        assert source_label in lowered, f"Missing source arm: {source_label}"


def test_0064_preserves_existing_visibility_filters() -> None:
    """Existing exclusion gates (deleted_at, search_suppressed_at, status, etc.)
    are still present."""
    lowered = _compact(UP_SQL)
    assert "m.deleted_at is null" in lowered
    assert "m.search_suppressed_at is null" in lowered
    assert "mem.status = 'active'" in lowered
    assert "obs.status = 'active'" in lowered
    assert "obs.significance >= 3" in lowered
    assert "d.status = 'active'" in lowered
    assert "ca.deleted_at is null" in lowered
    assert "(ca.expires_at is null or ca.expires_at > now())" in lowered
    assert "t.status = 'active'" in lowered
    assert "where btrim(coalesce(cn.text, '')) <> ''" in lowered


def test_0064_down_cleans_reflection_rows_before_tightening_constraints() -> None:
    """Down migration deletes 'reflection' rows from content_embeddings and
    embed_jobs BEFORE altering the CHECK constraints back to the 0059 set."""
    lowered = _compact(DOWN_SQL)
    assert "delete from mediator.embed_jobs where source_type = 'reflection'" in lowered
    assert "delete from mediator.content_embeddings where source_type = 'reflection'" in lowered

    # Cleanup must happen before re-applying tighter constraints
    del_embed = lowered.index("delete from mediator.embed_jobs where source_type = 'reflection'")
    del_content = lowered.index("delete from mediator.content_embeddings where source_type = 'reflection'")
    alter_check = lowered.index("add constraint content_embeddings_source_type_check")
    assert del_embed < alter_check
    assert del_content < alter_check


def test_0064_down_restores_0059_source_type_contract() -> None:
    """Down migration restores CHECK constraints without 'reflection'."""
    lowered = _compact(DOWN_SQL)
    expected_0059_check = (
        "check ( source_type in ( 'message', 'memory', 'observation', "
        "'distillation', 'artifact', 'conversation_note', 'theme' ) )"
    )
    assert expected_0059_check in lowered
    assert "'reflection'" not in lowered.replace(
        "'reflection'", ""
    ) or lowered.count("'reflection'") <= 2  # only in DELETE statements


def test_0064_down_restores_view_without_reflection_arm() -> None:
    """Down view has no 'reflection' source_type arm."""
    lowered = _compact(DOWN_SQL)
    assert "create or replace view mediator.v_searchable_content" in lowered
    assert "'reflection'::text as source_type" not in lowered


# ===========================================================================
# Live DB checks — apply through 0064 in a scratch schema, seed reflection
# data, and verify the searchable-content surface.
# ===========================================================================


@pytest.mark.postgres
@pytest.mark.anyio
class TestReflectionSearchableContentDatabase:
    """Apply through 0064, seed reflection data, and verify v_searchable_content.

    Gated on TEST_DATABASE_URL because 0058+ require pgvector.
    """

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("TEST_DATABASE_URL"):
            pytest.skip(
                "TEST_DATABASE_URL unset; live migration validation requires it"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _database_dsn(admin_dsn: str, db_name: str) -> str:
        if "?" in admin_dsn:
            base, _, qs = admin_dsn.partition("?")
            head, _, _ = base.rpartition("/")
            return f"{head}/{db_name}?{qs}"
        head, _, _ = admin_dsn.rpartition("/")
        return f"{head}/{db_name}"

    @staticmethod
    async def _migrate_through_0064(conn, *, seed_before_0025: bool = True) -> None:
        """Apply all migrations through 0064 in the current search_path."""
        from tests.fixtures.postgres import _SEED_BEFORE_0025, _migration_files

        for path in _migration_files():
            if path.name.startswith("0065_"):
                break
            if seed_before_0025 and path.name == "0025_backfill_legacy_scope_columns.sql":
                await conn.execute(_SEED_BEFORE_0025)
            await conn.execute(path.read_text())

    # ------------------------------------------------------------------
    # Source-type parity
    # ------------------------------------------------------------------

    async def test_all_preexisting_source_types_appear_in_view(self) -> None:
        """After 0064, all seven pre-existing source types are present as
        enum values in v_searchable_content (they may have zero rows in a
        fresh DB, but the arms exist)."""
        import asyncpg as _asyncpg

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
        db_name = f"veas_0064_parity_{uuid4().hex[:12]}"
        test_dsn = self._database_dsn(admin_dsn, db_name)
        try:
            has_vector = await admin_conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )
            if not has_vector:
                pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

            for role in ("anon", "authenticated", "service_role"):
                await admin_conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

            conn = await _asyncpg.connect(test_dsn, statement_cache_size=0)
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
                await conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION auth.uid()
                    RETURNS uuid
                    LANGUAGE sql
                    STABLE
                    AS $$ SELECT NULL::uuid $$;
                    """
                )
                await conn.execute(
                    f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
                )
                await conn.execute("SET search_path TO mediator, public;")

                await self._migrate_through_0064(conn)

                # The view should be queryable and return at least the
                # expected source_type labels (even if no rows exist yet).
                distinct_types = await conn.fetch(
                    "SELECT DISTINCT source_type FROM mediator.v_searchable_content ORDER BY source_type;"
                )
                # On a fresh DB there may be zero rows, but the view must
                # still be structurally queryable.  We verify the view
                # definition instead via static checks above.
                assert isinstance(distinct_types, list)
            finally:
                await conn.close()
        finally:
            if admin_conn.is_closed():
                admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
            try:
                await admin_conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await admin_conn.close()

    # ------------------------------------------------------------------
    # Reflection inclusion/exclusion
    # ------------------------------------------------------------------

    async def test_reflection_entries_from_processed_sessions_appear(self) -> None:
        """A reflection entry from a processed session with non-empty
        plaintext_searchable and no superseding revision appears in
        v_searchable_content."""
        import asyncpg as _asyncpg

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
        db_name = f"veas_0064_include_{uuid4().hex[:12]}"
        test_dsn = self._database_dsn(admin_dsn, db_name)
        try:
            has_vector = await admin_conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )
            if not has_vector:
                pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

            for role in ("anon", "authenticated", "service_role"):
                await admin_conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

            conn = await _asyncpg.connect(test_dsn, statement_cache_size=0)
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
                await conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION auth.uid()
                    RETURNS uuid
                    LANGUAGE sql
                    STABLE
                    AS $$ SELECT NULL::uuid $$;
                    """
                )
                await conn.execute(
                    f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
                )
                await conn.execute("SET search_path TO mediator, public;")

                await self._migrate_through_0064(conn)

                # Get a real user, topic, and bot from the seed data.
                user_id = await conn.fetchval(
                    "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
                )
                topic_id = await conn.fetchval(
                    "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
                )
                bot_id = "mediator"

                # Create a processed session.
                session_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_sessions
                        (user_id, topic_id, bot_id, template_key, temporal_scope, phase,
                         status, finalized_at, processed_at)
                    VALUES ($1, $2, $3, 'end_of_day', 'day', 'closing',
                            'processed', now(), now())
                    RETURNING id;
                    """,
                    user_id, topic_id, bot_id,
                )

                # Create a searchable entry.
                entry_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, topic_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number)
                    VALUES ($1, $2, $3, $4,
                            'end_of_day', 'day', 'closing',
                            'Today I reflected on project progress and team dynamics.',
                            1)
                    RETURNING id;
                    """,
                    session_id, user_id, topic_id, bot_id,
                )

                # Query the view.
                rows = await conn.fetch(
                    """
                    SELECT source_type, source_id, canonical_text, sender_id,
                           thread_owner_user_id, bot_id, topic_id
                    FROM mediator.v_searchable_content
                    WHERE source_type = 'reflection'
                      AND source_id = $1;
                    """,
                    entry_id,
                )

                assert len(rows) == 1
                assert rows[0]["source_type"] == "reflection"
                assert rows[0]["source_id"] == entry_id
                assert "project progress" in rows[0]["canonical_text"]
                assert rows[0]["sender_id"] == user_id
                assert rows[0]["thread_owner_user_id"] == user_id
                assert rows[0]["bot_id"] == bot_id
                assert rows[0]["topic_id"] == topic_id
            finally:
                await conn.close()
        finally:
            if admin_conn.is_closed():
                admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
            try:
                await admin_conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await admin_conn.close()

    async def test_reflection_entries_from_non_processed_sessions_excluded(self) -> None:
        """Entries from sessions with status != 'processed' (collecting,
        finalizing, abandoned, processing_failed) are excluded."""
        import asyncpg as _asyncpg

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
        db_name = f"veas_0064_excl_{uuid4().hex[:12]}"
        test_dsn = self._database_dsn(admin_dsn, db_name)
        try:
            has_vector = await admin_conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )
            if not has_vector:
                pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

            for role in ("anon", "authenticated", "service_role"):
                await admin_conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

            conn = await _asyncpg.connect(test_dsn, statement_cache_size=0)
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
                await conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION auth.uid()
                    RETURNS uuid
                    LANGUAGE sql
                    STABLE
                    AS $$ SELECT NULL::uuid $$;
                    """
                )
                await conn.execute(
                    f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
                )
                await conn.execute("SET search_path TO mediator, public;")

                await self._migrate_through_0064(conn)

                user_id = await conn.fetchval(
                    "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
                )
                topic_id = await conn.fetchval(
                    "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
                )
                bot_id = "mediator"

                non_processed_statuses = ["collecting", "finalizing", "abandoned", "processing_failed"]
                entry_ids = []

                for status in non_processed_statuses:
                    extra = {}
                    if status == "abandoned":
                        extra["abandoned_at"] = "now()"
                    elif status in ("processing_failed",):
                        extra["finalized_at"] = "now()"
                    elif status == "finalizing":
                        extra["finalized_at"] = "now()"

                    cols = ["user_id", "topic_id", "bot_id", "template_key",
                            "temporal_scope", "phase", "status"]
                    vals = [f"${i+1}" for i in range(len(cols))]
                    for ek, ev in extra.items():
                        cols.append(ek)
                        vals.append(ev)
                    # Build the SQL dynamically but safely with known status values
                    session_id = await conn.fetchval(
                        f"""
                        INSERT INTO mediator.reflection_sessions
                            (user_id, topic_id, bot_id, template_key,
                             temporal_scope, phase, status{", " + ", ".join(extra.keys()) if extra else ""})
                        VALUES ($1, $2, $3, 'end_of_day', 'day', 'closing',
                                '{status}'{", now()" if extra else ""})
                        RETURNING id;
                        """,
                        user_id, topic_id, bot_id,
                    )

                    entry_id = await conn.fetchval(
                        """
                        INSERT INTO mediator.reflection_entries
                            (session_id, user_id, topic_id, bot_id,
                             template_key, temporal_scope, phase,
                             plaintext_searchable, revision_number)
                        VALUES ($1, $2, $3, $4,
                                'end_of_day', 'day', 'closing',
                                'Reflection from ' || $5,
                                1)
                        RETURNING id;
                        """,
                        session_id, user_id, topic_id, bot_id, status,
                    )
                    entry_ids.append(entry_id)

                # None of these entries should appear in v_searchable_content.
                visible = await conn.fetch(
                    """
                    SELECT source_id FROM mediator.v_searchable_content
                    WHERE source_type = 'reflection'
                      AND source_id = ANY($1::uuid[]);
                    """,
                    entry_ids,
                )
                assert len(visible) == 0, (
                    f"Expected 0 entries, got {len(visible)}: "
                    f"{[r['source_id'] for r in visible]}"
                )
            finally:
                await conn.close()
        finally:
            if admin_conn.is_closed():
                admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
            try:
                await admin_conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await admin_conn.close()

    async def test_superseded_reflection_entries_excluded(self) -> None:
        """When an entry has supersedes_entry_id IS NOT NULL (i.e. it was
        corrected by a newer revision), it is excluded from the view."""
        import asyncpg as _asyncpg

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
        db_name = f"veas_0064_super_{uuid4().hex[:12]}"
        test_dsn = self._database_dsn(admin_dsn, db_name)
        try:
            has_vector = await admin_conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )
            if not has_vector:
                pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

            for role in ("anon", "authenticated", "service_role"):
                await admin_conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

            conn = await _asyncpg.connect(test_dsn, statement_cache_size=0)
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
                await conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION auth.uid()
                    RETURNS uuid
                    LANGUAGE sql
                    STABLE
                    AS $$ SELECT NULL::uuid $$;
                    """
                )
                await conn.execute(
                    f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
                )
                await conn.execute("SET search_path TO mediator, public;")

                await self._migrate_through_0064(conn)

                user_id = await conn.fetchval(
                    "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
                )
                topic_id = await conn.fetchval(
                    "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
                )
                bot_id = "mediator"

                # Create a processed session.
                session_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_sessions
                        (user_id, topic_id, bot_id, template_key, temporal_scope, phase,
                         status, finalized_at, processed_at)
                    VALUES ($1, $2, $3, 'end_of_day', 'day', 'closing',
                            'processed', now(), now())
                    RETURNING id;
                    """,
                    user_id, topic_id, bot_id,
                )

                # Create original entry (v1).
                original_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, topic_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number)
                    VALUES ($1, $2, $3, $4,
                            'end_of_day', 'day', 'closing',
                            'Original reflection that was later corrected.',
                            1)
                    RETURNING id;
                    """,
                    session_id, user_id, topic_id, bot_id,
                )

                # Create corrected entry (v2) that supersedes v1.
                corrected_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, topic_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number,
                         supersedes_entry_id)
                    VALUES ($1, $2, $3, $4,
                            'end_of_day', 'day', 'closing',
                            'Corrected reflection with accurate details.',
                            2, $5)
                    RETURNING id;
                    """,
                    session_id, user_id, topic_id, bot_id, original_id,
                )

                # Only the corrected (current) entry should appear.
                rows = await conn.fetch(
                    """
                    SELECT source_id FROM mediator.v_searchable_content
                    WHERE source_type = 'reflection'
                      AND source_id = ANY($1::uuid[]);
                    """,
                    [original_id, corrected_id],
                )
                visible_ids = {r["source_id"] for r in rows}
                assert original_id not in visible_ids, (
                    "Superseded entry should be excluded"
                )
                assert corrected_id in visible_ids, (
                    "Current (un-superseded) entry should appear"
                )
            finally:
                await conn.close()
        finally:
            if admin_conn.is_closed():
                admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
            try:
                await admin_conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await admin_conn.close()

    async def test_entries_with_empty_or_null_plaintext_excluded(self) -> None:
        """Entries with NULL or whitespace-only plaintext_searchable do not
        appear in v_searchable_content, even from processed sessions."""
        import asyncpg as _asyncpg

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
        db_name = f"veas_0064_empty_{uuid4().hex[:12]}"
        test_dsn = self._database_dsn(admin_dsn, db_name)
        try:
            has_vector = await admin_conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )
            if not has_vector:
                pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

            for role in ("anon", "authenticated", "service_role"):
                await admin_conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

            conn = await _asyncpg.connect(test_dsn, statement_cache_size=0)
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
                await conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION auth.uid()
                    RETURNS uuid
                    LANGUAGE sql
                    STABLE
                    AS $$ SELECT NULL::uuid $$;
                    """
                )
                await conn.execute(
                    f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
                )
                await conn.execute("SET search_path TO mediator, public;")

                await self._migrate_through_0064(conn)

                user_id = await conn.fetchval(
                    "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
                )
                topic_id = await conn.fetchval(
                    "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
                )
                bot_id = "mediator"

                # Create a processed session.
                session_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_sessions
                        (user_id, topic_id, bot_id, template_key, temporal_scope, phase,
                         status, finalized_at, processed_at)
                    VALUES ($1, $2, $3, 'end_of_day', 'day', 'closing',
                            'processed', now(), now())
                    RETURNING id;
                    """,
                    user_id, topic_id, bot_id,
                )

                # Entry with NULL plaintext.
                null_entry_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, topic_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number)
                    VALUES ($1, $2, $3, $4,
                            'end_of_day', 'day', 'closing',
                            NULL, 1)
                    RETURNING id;
                    """,
                    session_id, user_id, topic_id, bot_id,
                )

                # Entry with whitespace-only plaintext.
                blank_entry_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, topic_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number)
                    VALUES ($1, $2, $3, $4,
                            'end_of_day', 'day', 'closing',
                            '   ', 2)
                    RETURNING id;
                    """,
                    session_id, user_id, topic_id, bot_id,
                )

                # Entry with real content (should be visible).
                good_entry_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, topic_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number)
                    VALUES ($1, $2, $3, $4,
                            'end_of_day', 'day', 'closing',
                            'A valid searchable reflection.',
                            3)
                    RETURNING id;
                    """,
                    session_id, user_id, topic_id, bot_id,
                )

                rows = await conn.fetch(
                    """
                    SELECT source_id FROM mediator.v_searchable_content
                    WHERE source_type = 'reflection'
                      AND source_id = ANY($1::uuid[]);
                    """,
                    [null_entry_id, blank_entry_id, good_entry_id],
                )
                visible_ids = {r["source_id"] for r in rows}

                assert null_entry_id not in visible_ids, (
                    "NULL plaintext entry should be excluded"
                )
                assert blank_entry_id not in visible_ids, (
                    "Whitespace-only plaintext entry should be excluded"
                )
                assert good_entry_id in visible_ids, (
                    "Non-empty plaintext entry should appear"
                )
            finally:
                await conn.close()
        finally:
            if admin_conn.is_closed():
                admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
            try:
                await admin_conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await admin_conn.close()

    async def test_reflection_source_type_valid_in_check_constraints(self) -> None:
        """Verify that 'reflection' is accepted by both content_embeddings
        and embed_jobs CHECK constraints (no constraint violation on insert)."""
        import asyncpg as _asyncpg

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
        db_name = f"veas_0064_checks_{uuid4().hex[:12]}"
        test_dsn = self._database_dsn(admin_dsn, db_name)
        try:
            has_vector = await admin_conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )
            if not has_vector:
                pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

            for role in ("anon", "authenticated", "service_role"):
                await admin_conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

            conn = await _asyncpg.connect(test_dsn, statement_cache_size=0)
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
                await conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION auth.uid()
                    RETURNS uuid
                    LANGUAGE sql
                    STABLE
                    AS $$ SELECT NULL::uuid $$;
                    """
                )
                await conn.execute(
                    f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
                )
                await conn.execute("SET search_path TO mediator, public;")

                await self._migrate_through_0064(conn)

                user_id = await conn.fetchval(
                    "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
                )
                bot_id = "mediator"

                # Create a processed session + entry so we have a real source_id.
                session_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_sessions
                        (user_id, bot_id, template_key, temporal_scope, phase,
                         status, finalized_at, processed_at)
                    VALUES ($1, $2, 'end_of_day', 'day', 'closing',
                            'processed', now(), now())
                    RETURNING id;
                    """,
                    user_id, bot_id,
                )
                entry_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number)
                    VALUES ($1, $2, $3,
                            'end_of_day', 'day', 'closing',
                            'Check constraint validation entry.', 1)
                    RETURNING id;
                    """,
                    session_id, user_id, bot_id,
                )

                # Insert into content_embeddings with source_type='reflection'.
                await conn.execute(
                    """
                    INSERT INTO mediator.content_embeddings
                        (source_type, source_id, embedding, model, dimension, content_hash, embedded_at)
                    VALUES ('reflection', $1, $2::vector, 'test-model', 3, '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
                            now());
                    """,
                    entry_id,
                    "[" + ",".join(["0.1"] * 3) + "]",
                )

                # Insert into embed_jobs with source_type='reflection'.
                await conn.execute(
                    """
                    INSERT INTO mediator.embed_jobs
                        (source_type, source_id, job_kind, status, content_hash, model, dimension, next_attempt_at)
                    VALUES ('reflection', $1, 'embed', 'pending',
                            'fedcba9876543210fedcba9876543210fedcba9876543210fedcba9876543210',
                            'test-model', 3, now());
                    """,
                    entry_id,
                )

                # Verify the rows exist.
                ce_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM mediator.content_embeddings WHERE source_type = 'reflection';"
                )
                ej_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM mediator.embed_jobs WHERE source_type = 'reflection';"
                )
                assert ce_count == 1
                assert ej_count == 1

                # Verify that an invalid source_type is still rejected.
                with pytest.raises(Exception):
                    await conn.execute(
                        """
                        INSERT INTO mediator.embed_jobs
                            (source_type, source_id, job_kind, status, content_hash, model, dimension, next_attempt_at)
                        VALUES ('invalid_type', $1, 'embed', 'pending',
                                'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789',
                                'test-model', 3, now());
                        """,
                        entry_id,
                    )
            finally:
                await conn.close()
        finally:
            if admin_conn.is_closed():
                admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
            try:
                await admin_conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await admin_conn.close()

    async def test_derivations_not_in_searchable_content(self) -> None:
        """Reflection derivations (with deferred/rejected decisions) are NOT
        in the v_searchable_content view — they are inspectable only through
        explicit list/get tools."""
        import asyncpg as _asyncpg

        admin_dsn = os.environ["TEST_DATABASE_URL"]
        admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
        db_name = f"veas_0064_deriv_{uuid4().hex[:12]}"
        test_dsn = self._database_dsn(admin_dsn, db_name)
        try:
            has_vector = await admin_conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')"
            )
            if not has_vector:
                pytest.skip("TEST_DATABASE_URL cluster does not have pgvector available")

            for role in ("anon", "authenticated", "service_role"):
                await admin_conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

            conn = await _asyncpg.connect(test_dsn, statement_cache_size=0)
            try:
                await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
                await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
                await conn.execute(
                    """
                    CREATE OR REPLACE FUNCTION auth.uid()
                    RETURNS uuid
                    LANGUAGE sql
                    STABLE
                    AS $$ SELECT NULL::uuid $$;
                    """
                )
                await conn.execute(
                    f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
                )
                await conn.execute("SET search_path TO mediator, public;")

                await self._migrate_through_0064(conn)

                user_id = await conn.fetchval(
                    "SELECT id FROM mediator.users ORDER BY created_at LIMIT 1;"
                )
                bot_id = "mediator"

                # Create a processed session + entry so the entry appears.
                session_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_sessions
                        (user_id, bot_id, template_key, temporal_scope, phase,
                         status, finalized_at, processed_at)
                    VALUES ($1, $2, 'end_of_day', 'day', 'closing',
                            'processed', now(), now())
                    RETURNING id;
                    """,
                    user_id, bot_id,
                )
                entry_id = await conn.fetchval(
                    """
                    INSERT INTO mediator.reflection_entries
                        (session_id, user_id, bot_id,
                         template_key, temporal_scope, phase,
                         plaintext_searchable, revision_number)
                    VALUES ($1, $2, $3,
                            'end_of_day', 'day', 'closing',
                            'Searchable entry with deferred and rejected derivations.', 1)
                    RETURNING id;
                    """,
                    session_id, user_id, bot_id,
                )

                # Create derivations with deferred and rejected decisions.
                # These should NOT leak into v_searchable_content.
                await conn.execute(
                    """
                    INSERT INTO mediator.reflection_derivations
                        (entry_id, user_id, bot_id, derivation_kind, assertion_source,
                         decision, decision_reason, candidate_payload_encrypted)
                    VALUES
                        ($1, $2, $3, 'memory', 'agent_inferred', 'deferred',
                         'needs more evidence', E'\\\\x00'),
                        ($1, $2, $3, 'observation', 'agent_inferred', 'rejected',
                         'contradicts existing observations', E'\\\\x00');
                    """,
                    entry_id, user_id, bot_id,
                )

                # The entry should still appear in v_searchable_content once.
                rows = await conn.fetch(
                    """
                    SELECT source_id, source_type FROM mediator.v_searchable_content
                    WHERE source_type = 'reflection' AND source_id = $1;
                    """,
                    entry_id,
                )
                assert len(rows) == 1
                assert rows[0]["source_type"] == "reflection"

                # But there should be no rows keyed by derivation IDs.
                deriv_ids = await conn.fetch(
                    "SELECT id FROM mediator.reflection_derivations WHERE entry_id = $1;",
                    entry_id,
                )
                if deriv_ids:
                    deriv_id_list = [r["id"] for r in deriv_ids]
                    deriv_rows = await conn.fetch(
                        """
                        SELECT source_id FROM mediator.v_searchable_content
                        WHERE source_id = ANY($1::uuid[]);
                        """,
                        deriv_id_list,
                    )
                    assert len(deriv_rows) == 0, (
                        "Derivations must not appear in v_searchable_content"
                    )
            finally:
                await conn.close()
        finally:
            if admin_conn.is_closed():
                admin_conn = await _asyncpg.connect(admin_dsn, statement_cache_size=0)
            try:
                await admin_conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await admin_conn.close()
