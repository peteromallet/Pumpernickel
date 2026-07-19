"""Static + live validation of migration 0063_reflection_foundation.

Two layers:
1. Static text checks against the migration SQL (always run; no DB needed).
2. Live DB checks that apply the migration in a scratch schema and verify
   table existence, RLS, policies, indexes, and down-migration rollback.
   Skipped when DATABASE_URL / EVAL_DATABASE_URL is not set, following the
   convention in tests/test_live_migrations.py.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
UP_PATH = MIGRATIONS_DIR / "0063_reflection_foundation.sql"
DOWN_PATH = MIGRATIONS_DIR / "0063_reflection_foundation.down.sql"
UP_SQL = UP_PATH.read_text()
DOWN_SQL = DOWN_PATH.read_text()

REFLECTION_TABLES: tuple[str, ...] = (
    "reflection_sessions",
    "reflection_entries",
    "reflection_derivations",
)

REFLECTION_INDEXES: tuple[str, ...] = (
    "idx_reflection_sessions_one_collecting",
    "idx_reflection_sessions_finalized_ready",
    "idx_reflection_sessions_failed_retry",
    "idx_reflection_sessions_idle_due",
    "idx_reflection_sessions_user_recent",
    "idx_reflection_sessions_claimed_by",
    "idx_reflection_entries_session_rev",
    "idx_reflection_entries_current",
    "idx_reflection_entries_supersedes",
    "idx_reflection_entries_user_recent",
    "idx_reflection_derivations_entry",
    "idx_reflection_derivations_deferred",
)


def _compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def _ddl_only(sql: str) -> str:
    kept_lines = []
    for line in sql.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        if "--" in line:
            line = line.split("--", 1)[0]
        kept_lines.append(line)
    return _compact("\n".join(kept_lines))


ud = _ddl_only(UP_SQL)
dd = _ddl_only(DOWN_SQL)


def test_0063_files_exist():
    assert UP_PATH.exists()
    assert DOWN_PATH.exists()


def test_0063_up_transaction_wrapping():
    assert "begin;" in ud
    assert "commit;" in ud


def test_0063_down_transaction_wrapping():
    assert "begin;" in dd
    assert "commit;" in dd


def test_0063_creates_exactly_three_reflection_tables():
    assert "create table mediator.reflection_sessions" in ud
    assert "create table mediator.reflection_entries" in ud
    assert "create table mediator.reflection_derivations" in ud
    assert ud.count("create table mediator.reflection_") == 3


def test_0063_no_forbidden_surfaces():
    assert "scheduled_job" not in ud.replace("-- no scheduled_jobs", "")
    assert "feature_flag" not in ud


def test_0063_source_message_arrays():
    assert "source_message_ids uuid[]" in ud


def test_0063_scope_foreign_keys():
    assert "references mediator.users(id)" in ud
    assert "references mediator.bots(id)" in ud
    assert "references mediator.topics(id)" in ud


def test_0063_encrypted_columns():
    assert "payload_encrypted bytea" in ud
    assert "summary_encrypted bytea" in ud
    assert "candidate_payload_encrypted bytea" in ud
    assert "plaintext_searchable" in ud


def test_0063_idempotency_keys():
    assert "idempotency_key text" in ud
    assert "unique (idempotency_key)" in ud


def test_0063_claim_queue_state():
    for field in ["claimed_by", "claimed_at", "retry_count", "failure_class", "failure_reason", "last_error"]:
        assert field in ud, f"Missing: {field}"


def test_0063_immutable_revisions():
    assert "supersedes_entry_id" in ud
    assert "revision_number" in ud
    assert "unique (session_id, revision_number)" in ud
    assert "check (supersedes_entry_id is null or supersedes_entry_id <> id)" in ud


def test_0063_rls_all_three_tables():
    for table in ("reflection_sessions", "reflection_entries", "reflection_derivations"):
        assert f"alter table mediator.{table} enable row level security" in ud
        assert f"alter table mediator.{table} force row level security" in ud
        assert f"revoke all on table mediator.{table} from anon, authenticated" in ud
        assert f"create policy deny_anon_{table}" in ud
        assert f"create policy owner_scoped_{table}" in ud


def test_0063_one_collecting_session_guard():
    assert "unique index idx_reflection_sessions_one_collecting" in ud
    assert "where status = 'collecting'" in ud


def test_0063_down_fk_safe_order():
    policies_pos = dd.index("drop policy")
    deriv_pos = dd.index("drop table if exists mediator.reflection_derivations")
    entries_pos = dd.index("drop table if exists mediator.reflection_entries")
    sessions_pos = dd.index("drop table if exists mediator.reflection_sessions")
    assert policies_pos < deriv_pos < entries_pos < sessions_pos


def test_0063_down_all_drops_use_if_exists():
    for stmt in dd.split(";"):
        stmt = stmt.strip()
        if stmt.startswith("drop "):
            assert "if exists" in stmt, f"Missing IF EXISTS: {stmt[:80]}"


def test_0063_down_drops_all_six_policies():
    assert dd.count("drop policy if exists") == 6


def test_0063_session_lifecycle_checks():
    assert "check (status in ('collecting', 'finalizing', 'processed', 'abandoned', 'processing_failed'))" in ud
    assert "status <> 'abandoned' or abandoned_at is not null" in ud


def test_0063_derivation_checks():
    assert "check (derivation_kind in ('memory', 'observation', 'distillation', 'orientation'))" in ud
    assert "check (assertion_source in ('user_explicit', 'user_implied', 'agent_inferred'))" in ud
    assert "check (decision in ('applied', 'reinforced', 'deferred', 'rejected', 'superseded'))" in ud


def test_0063_all_required_indexes():
    for idx in REFLECTION_INDEXES:
        assert idx in ud, f"Missing index: {idx}"


# ===========================================================================
# Live DB checks — apply 0063 in a scratch schema and verify the surface,
# then apply the down migration and verify clean rollback.
# ===========================================================================


class TestReflectionMigrationDatabase:
    """Apply 0063 to a scratch schema and verify the live schema shape.

    Also verifies the down migration by applying it within the scratch
    schema and confirming all three tables are removed.
    """

    @pytest.fixture(autouse=True)
    def _check_db_url(self) -> None:
        if not os.environ.get("DATABASE_URL") and not os.environ.get("EVAL_DATABASE_URL"):
            pytest.skip(
                "DATABASE_URL / EVAL_DATABASE_URL not set — skipping live migration test"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _table_names_in_mediator(conn: object) -> set[str]:
        rows = await conn.fetch(
            """SELECT c.relname AS table_name
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'mediator'
                 AND c.relkind = 'r'
                 AND c.relname = ANY($1::text[])""",
            list(REFLECTION_TABLES),
        )
        return {r["table_name"] for r in rows}

    @staticmethod
    async def _fetch_rls_state(conn: object) -> dict[str, dict[str, bool]]:
        rows = await conn.fetch(
            """SELECT c.relname AS table_name,
                      c.relrowsecurity AS rls_enabled,
                      c.relforcerowsecurity AS rls_forced
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'mediator'
                 AND c.relkind = 'r'
                 AND c.relname = ANY($1::text[])""",
            list(REFLECTION_TABLES),
        )
        return {
            r["table_name"]: {
                "rls_enabled": r["rls_enabled"],
                "rls_forced": r["rls_forced"],
            }
            for r in rows
        }

    @staticmethod
    async def _fetch_policies(conn: object) -> dict[str, set[str]]:
        rows = await conn.fetch(
            """SELECT tablename, policyname
               FROM pg_policies
               WHERE schemaname = 'mediator'
                 AND tablename = ANY($1::text[])""",
            list(REFLECTION_TABLES),
        )
        by_table: dict[str, set[str]] = {}
        for r in rows:
            by_table.setdefault(r["tablename"], set()).add(r["policyname"])
        return by_table

    @staticmethod
    async def _fetch_index_names(conn: object) -> set[str]:
        rows = await conn.fetch(
            """SELECT i.relname AS index_name
               FROM pg_class i
               JOIN pg_namespace n ON n.oid = i.relnamespace
               WHERE n.nspname = 'mediator'
                 AND i.relkind = 'i'
                 AND i.relname = ANY($1::text[])""",
            list(REFLECTION_INDEXES),
        )
        return {r["index_name"] for r in rows}

    @staticmethod
    async def _fetch_column_info(conn: object, table: str) -> list[dict[str, object]]:
        return await conn.fetch(
            """SELECT column_name, data_type, udt_name
               FROM information_schema.columns
               WHERE table_schema = 'mediator'
                 AND table_name = $1
               ORDER BY ordinal_position""",
            table,
        )

    @staticmethod
    async def _reflection_table_count(conn: object) -> int:
        row = await conn.fetchrow(
            """SELECT count(*) AS cnt
               FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'mediator'
                 AND c.relkind = 'r'
                 AND c.relname LIKE 'reflection_%'"""
        )
        return row["cnt"] if row else 0

    @staticmethod
    async def _table_exists(conn: object, table: str) -> bool:
        row = await conn.fetchrow(
            """SELECT 1 FROM pg_class c
               JOIN pg_namespace n ON n.oid = c.relnamespace
               WHERE n.nspname = 'mediator'
                 AND c.relkind = 'r'
                 AND c.relname = $1""",
            table,
        )
        return row is not None

    # ------------------------------------------------------------------
    # Apply + verify
    # ------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_three_reflection_tables_exist_in_scratch(self) -> None:
        """After applying all migrations, exactly 3 reflection_* tables exist."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_foundation") as scratch:
                async with pool.acquire() as conn:
                    names = await self._table_names_in_mediator(conn)
                    missing = [t for t in REFLECTION_TABLES if t not in names]
                    assert not missing, f"Tables not created in mediator: {missing}"

                    extra_count = await self._reflection_table_count(conn)
                    assert extra_count == 3, (
                        f"Expected exactly 3 reflection_* tables, found {extra_count}"
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_no_forbidden_surfaces_in_live_schema(self) -> None:
        """No scheduled_jobs or feature-flag integration tables exist."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_no_forbidden") as scratch:
                async with pool.acquire() as conn:
                    # No reflection association/processing-job table.
                    for forbidden in (
                        "reflection_associations",
                        "reflection_processing_jobs",
                        "reflection_scheduled_jobs",
                    ):
                        exists = await self._table_exists(conn, forbidden)
                        assert not exists, f"Forbidden table exists: {forbidden}"

                    # No scheduled_jobs integration column on reflection tables.
                    for table in REFLECTION_TABLES:
                        cols = await self._fetch_column_info(conn, table)
                        col_names = {c["column_name"] for c in cols}
                        assert "scheduled_job_id" not in col_names, (
                            f"scheduled_job_id column found on {table}"
                        )
                        assert "feature_flag" not in col_names, (
                            f"feature_flag column found on {table}"
                        )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_rls_enabled_and_forced_on_all_three_tables(self) -> None:
        """RLS is ENABLED and FORCED on every reflection table."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_rls") as scratch:
                async with pool.acquire() as conn:
                    rls = await self._fetch_rls_state(conn)
                    for table in REFLECTION_TABLES:
                        assert table in rls, f"Table {table} not found in pg_class"
                        assert rls[table]["rls_enabled"], (
                            f"RLS not enabled on {table}"
                        )
                        assert rls[table]["rls_forced"], (
                            f"RLS not forced on {table}"
                        )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_deny_and_owner_scoped_policies_on_all_tables(self) -> None:
        """Every reflection table has deny_anon + owner_scoped policies."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_policies") as scratch:
                async with pool.acquire() as conn:
                    policies = await self._fetch_policies(conn)
                    for table in REFLECTION_TABLES:
                        tbl_policies = policies.get(table, set())
                        assert f"deny_anon_{table}" in tbl_policies, (
                            f"Missing deny_anon policy on {table}; have={tbl_policies}"
                        )
                        assert f"owner_scoped_{table}" in tbl_policies, (
                            f"Missing owner_scoped policy on {table}; have={tbl_policies}"
                        )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_all_required_indexes_exist(self) -> None:
        """All 12 required indexes are physically present."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_indexes") as scratch:
                async with pool.acquire() as conn:
                    index_names = await self._fetch_index_names(conn)
                    missing = [idx for idx in REFLECTION_INDEXES if idx not in index_names]
                    assert not missing, f"Missing indexes: {missing}"
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_encrypted_columns_present(self) -> None:
        """Dual-column encryption fields exist on entries and derivations."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_encrypted") as scratch:
                async with pool.acquire() as conn:
                    # reflection_entries must have payload_encrypted (bytea),
                    # plaintext_searchable (text), summary_encrypted (bytea).
                    entry_cols = await self._fetch_column_info(conn, "reflection_entries")
                    entry_names = {c["column_name"] for c in entry_cols}
                    for col in ("payload_encrypted", "plaintext_searchable", "summary_encrypted"):
                        assert col in entry_names, f"Missing column {col} on reflection_entries"

                    # Verify data types.
                    by_name = {c["column_name"]: c["data_type"] for c in entry_cols}
                    assert by_name.get("payload_encrypted") == "bytea", (
                        f"payload_encrypted type={by_name.get('payload_encrypted')}"
                    )
                    assert by_name.get("summary_encrypted") == "bytea", (
                        f"summary_encrypted type={by_name.get('summary_encrypted')}"
                    )

                    # reflection_derivations must have candidate_payload_encrypted (bytea).
                    deriv_cols = await self._fetch_column_info(conn, "reflection_derivations")
                    deriv_names = {c["column_name"] for c in deriv_cols}
                    assert "candidate_payload_encrypted" in deriv_names, (
                        "Missing candidate_payload_encrypted on reflection_derivations"
                    )
                    deriv_by_name = {c["column_name"]: c["data_type"] for c in deriv_cols}
                    assert deriv_by_name.get("candidate_payload_encrypted") == "bytea", (
                        f"candidate_payload_encrypted type={deriv_by_name.get('candidate_payload_encrypted')}"
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_source_message_ids_is_uuid_array(self) -> None:
        """source_message_ids columns are uuid[] arrays."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_uuid_array") as scratch:
                async with pool.acquire() as conn:
                    for table in ("reflection_sessions", "reflection_entries"):
                        cols = await self._fetch_column_info(conn, table)
                        by_name = {c["column_name"]: c for c in cols}
                        col = by_name.get("source_message_ids")
                        assert col is not None, f"source_message_ids missing on {table}"
                        assert col["data_type"] == "ARRAY", (
                            f"source_message_ids on {table} is {col['data_type']}, expected ARRAY"
                        )
                        assert col["udt_name"] == "_uuid", (
                            f"source_message_ids on {table} is {col['udt_name']}, expected _uuid"
                        )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_unique_constraints_exist(self) -> None:
        """Idempotency key uniques and session+revision unique are enforced."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_uniques") as scratch:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        """SELECT con.conname AS constraint_name,
                                  t.relname AS table_name
                           FROM pg_constraint con
                           JOIN pg_class t ON t.oid = con.conrelid
                           JOIN pg_namespace n ON n.oid = t.relnamespace
                           WHERE n.nspname = 'mediator'
                             AND t.relname = ANY($1::text[])
                             AND con.contype = 'u'""",
                        list(REFLECTION_TABLES),
                    )
                    by_table: dict[str, set[str]] = {}
                    for r in rows:
                        by_table.setdefault(r["table_name"], set()).add(r["constraint_name"])

                    # sessions: idempotency_key unique
                    sess_uniques = by_table.get("reflection_sessions", set())
                    has_idem_unique = any(
                        "idempotency_key" in name for name in sess_uniques
                    )
                    assert has_idem_unique, (
                        f"No unique constraint on idempotency_key in reflection_sessions; have={sess_uniques}"
                    )

                    # entries: (session_id, revision_number) unique
                    entry_uniques = by_table.get("reflection_entries", set())
                    has_sess_rev_unique = any(
                        "session_id" in name and "revision_number" in name
                        for name in entry_uniques
                    )
                    assert has_sess_rev_unique, (
                        f"No unique on (session_id, revision_number) in reflection_entries; have={entry_uniques}"
                    )

                    # derivations: idempotency_key unique
                    deriv_uniques = by_table.get("reflection_derivations", set())
                    has_deriv_idem_unique = any(
                        "idempotency_key" in name for name in deriv_uniques
                    )
                    assert has_deriv_idem_unique, (
                        f"No unique constraint on idempotency_key in reflection_derivations; have={deriv_uniques}"
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_one_collecting_session_partial_unique_index(self) -> None:
        """The partial unique index prevents two collecting sessions per user+bot."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_collecting") as scratch:
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        """SELECT 1
                           FROM pg_indexes
                           WHERE schemaname = 'mediator'
                             AND tablename = 'reflection_sessions'
                             AND indexname = 'idx_reflection_sessions_one_collecting'"""
                    )
                    assert row is not None, (
                        "Partial unique index idx_reflection_sessions_one_collecting not found"
                    )
        finally:
            await pool.close()

    # ------------------------------------------------------------------
    # Rollback: apply the down migration and verify clean removal
    # ------------------------------------------------------------------

    @pytest.mark.anyio
    async def test_down_migration_removes_all_three_tables(self) -> None:
        """Applying the down migration drops all three reflection tables."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_rollback") as scratch:
                async with pool.acquire() as conn:
                    # Confirm tables exist before rollback.
                    names_before = await self._table_names_in_mediator(conn)
                    assert names_before == set(REFLECTION_TABLES), (
                        f"Pre-rollback tables mismatch: {names_before}"
                    )

                    # Apply the down migration.
                    await conn.execute(DOWN_SQL)

                    # Confirm all three tables are gone.
                    for table in REFLECTION_TABLES:
                        exists = await self._table_exists(conn, table)
                        assert not exists, f"Table {table} still exists after down migration"

                    # Also verify no orphan reflection_* tables remain.
                    count_after = await self._reflection_table_count(conn)
                    assert count_after == 0, (
                        f"Expected 0 reflection_* tables after rollback, found {count_after}"
                    )
        finally:
            await pool.close()

    @pytest.mark.anyio
    async def test_down_migration_removes_all_six_policies(self) -> None:
        """After down migration, all deny_anon + owner_scoped policies are gone."""
        from evals.db import create_eval_pool, scratch_schema

        pool = await create_eval_pool()
        try:
            async with scratch_schema(pool, schema="eval_reflection_rollback_policies") as scratch:
                async with pool.acquire() as conn:
                    # Apply the down migration.
                    await conn.execute(DOWN_SQL)

                    # All reflection policies should be gone.
                    rows = await conn.fetch(
                        """SELECT policyname
                           FROM pg_policies
                           WHERE schemaname = 'mediator'
                             AND tablename = ANY($1::text[])""",
                        list(REFLECTION_TABLES),
                    )
                    remaining = {r["policyname"] for r in rows}
                    assert not remaining, (
                        f"Policies still present after down migration: {remaining}"
                    )
        finally:
            await pool.close()
