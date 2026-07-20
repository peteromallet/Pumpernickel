from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_NUMBER = "0063"
UP_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_health_provider_foundation.sql"
DOWN_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_health_provider_foundation.down.sql"
UP_SQL = UP_PATH.read_text()
DOWN_SQL = DOWN_PATH.read_text()

HEALTH_TABLES = (
    "health_connections",
    "health_source_records",
    "health_sync_runs",
    "health_webhook_receipts",
    "health_dirty_categories",
    "health_normalized_measurements",
    "health_normalized_workouts",
    "health_normalized_sleep",
    "health_source_to_event_projections",
)


def _compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def _table_slice(sql: str, table: str, next_table: str | None) -> str:
    compacted = _compact(sql)
    start = compacted.index(f"create table mediator.{table}")
    if next_table is None:
        end = compacted.index("-- =========================================================================== -- 10. rls posture")
    else:
        end = compacted.index(f"create table mediator.{next_table}")
    return compacted[start:end]


def _database_dsn(admin_dsn: str, db_name: str) -> str:
    if "?" in admin_dsn:
        base, _, qs = admin_dsn.partition("?")
        head, _, _ = base.rpartition("/")
        return f"{head}/{db_name}?{qs}"
    head, _, _ = admin_dsn.rpartition("/")
    return f"{head}/{db_name}"


def test_0063_files_exist_and_are_numbered_pair() -> None:
    numbered = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    assert sum(1 for name in numbered if name.startswith(f"{MIGRATION_NUMBER}_")) == 1
    assert UP_PATH.exists()
    assert DOWN_PATH.exists()


def test_0063_up_creates_all_health_tables_in_one_transaction() -> None:
    lowered = UP_SQL.lower()
    assert "begin;" in lowered
    assert "commit;" in lowered
    for table in HEALTH_TABLES:
        assert f"create table mediator.{table}" in lowered
    assert lowered.index("begin;") < lowered.index("create table mediator.health_connections")
    assert lowered.index("create table mediator.health_source_to_event_projections") < lowered.index("commit;")


def test_0063_health_connections_contract() -> None:
    body = _table_slice(UP_SQL, "health_connections", "health_source_records")
    for column in (
        "user_id",
        "provider",
        "external_user_id",
        "status",
        "granted_scopes",
        "access_token_encrypted",
        "refresh_token_encrypted",
        "cursor_state",
        "last_error_code",
        "last_error_detail",
        "deleted_at",
    ):
        assert column in body
    assert "check (provider in ('withings'))" in body
    assert (
        "check (status in ('active', 'disconnected', 'revoked', 'reauth_required', 'deleted'))"
        in body
    )
    assert "check (jsonb_typeof(cursor_state) = 'object')" in body
    compacted = _compact(UP_SQL)
    assert "create unique index idx_health_connections_provider_external_user" in compacted
    assert "create unique index idx_health_connections_user_provider_active" in compacted


def test_0063_health_source_records_contract_and_uniqueness() -> None:
    body = _table_slice(UP_SQL, "health_source_records", "health_sync_runs")
    for column in (
        "connection_id",
        "user_id",
        "provider",
        "resource_type",
        "external_id",
        "payload_hash",
        "provider_revision",
        "source_metadata",
        "attribution",
        "is_deleted",
        "deleted_at",
    ):
        assert column in body
    assert "check (resource_type in ('measurement', 'workout', 'sleep'))" in body
    assert "unique (connection_id, resource_type, external_id)" in body
    assert "check (jsonb_typeof(source_metadata) = 'object')" in body
    assert "check (jsonb_typeof(attribution) = 'object')" in body
    compacted = _compact(UP_SQL)
    assert "create index idx_health_source_records_conn_resource_modified" in compacted
    assert "create index idx_health_source_records_conn_deleted" in compacted


def test_0063_other_table_indexes_and_constraints_are_present() -> None:
    compacted = _compact(UP_SQL)
    assert "create unique index idx_health_dirty_categories_open_unique" in compacted
    assert (
        "on mediator.health_dirty_categories (connection_id, resource_type) where cleared_at is null"
        in compacted
    )
    assert (
        "check (trigger_reason in ('dirty', 'manual', 'reconcile', 'initial_backfill', 'disconnect_cleanup'))"
        in compacted
    )
    assert "status = 'running' and completed_at is null" in compacted
    assert "status <> 'running' and completed_at is not null" in compacted
    assert "unique (provider, payload_hash)" in compacted
    assert "source_record_id uuid not null unique" in compacted
    assert "references mediator.events(id) on delete set null" in compacted
    assert "references mediator.commitments(id) on delete set null" in compacted


def test_0063_applies_force_rls_revoke_and_policies_to_every_health_table() -> None:
    compacted = _compact(UP_SQL)
    for table in HEALTH_TABLES:
        assert f"alter table mediator.{table} enable row level security" in compacted
        assert f"alter table mediator.{table} force row level security" in compacted
        assert f"revoke all on table mediator.{table} from anon, authenticated" in compacted
        assert f"create policy deny_anon_{table} on mediator.{table}" in compacted
        assert f"create policy owner_scoped_{table} on mediator.{table}" in compacted


def test_0063_down_drops_policies_before_tables_in_reverse_order() -> None:
    lowered = DOWN_SQL.lower()
    assert "begin;" in lowered
    assert "commit;" in lowered

    drop_order = [
        "health_source_to_event_projections",
        "health_normalized_sleep",
        "health_normalized_workouts",
        "health_normalized_measurements",
        "health_dirty_categories",
        "health_webhook_receipts",
        "health_sync_runs",
        "health_source_records",
        "health_connections",
    ]
    for table in drop_order:
        policy_pos = lowered.index(f"drop policy if exists deny_anon_{table}")
        owner_pos = lowered.index(f"drop policy if exists owner_scoped_{table}")
        table_pos = lowered.index(f"drop table if exists mediator.{table}")
        assert policy_pos < table_pos
        assert owner_pos < table_pos

    positions = [lowered.index(f"drop table if exists mediator.{table}") for table in drop_order]
    assert positions == sorted(positions)


@pytest.mark.postgres
@pytest.mark.anyio
async def test_0063_apply_and_rollback_catalog_surface() -> None:
    asyncpg = pytest.importorskip("asyncpg")

    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL unset; migration validation requires it")

    admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    db_name = f"health_0063_{uuid4().hex[:12]}"
    test_dsn = _database_dsn(admin_dsn, db_name)
    try:
        from tests.fixtures.postgres import _apply_migrations

        for role in ("anon", "authenticated", "service_role"):
            await admin_conn.execute(
                f"DO $$ BEGIN CREATE ROLE {role}; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
        await admin_conn.execute(f'CREATE DATABASE "{db_name}";')
        await _apply_migrations(test_dsn, db_name)

        conn = await asyncpg.connect(test_dsn, statement_cache_size=0)
        try:
            rows = await conn.fetch(
                """
                SELECT c.relname AS table_name,
                       c.relrowsecurity AS rls_enabled,
                       c.relforcerowsecurity AS rls_forced
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'mediator'
                  AND c.relkind = 'r'
                  AND c.relname = ANY($1::text[])
                """,
                list(HEALTH_TABLES),
            )
            by_name = {row["table_name"]: row for row in rows}
            assert set(by_name) == set(HEALTH_TABLES)
            for table in HEALTH_TABLES:
                assert by_name[table]["rls_enabled"] is True
                assert by_name[table]["rls_forced"] is True

            policy_rows = await conn.fetch(
                """
                SELECT tablename, policyname
                FROM pg_policies
                WHERE schemaname = 'mediator'
                  AND tablename = ANY($1::text[])
                """,
                list(HEALTH_TABLES),
            )
            policy_names: dict[str, set[str]] = {}
            for row in policy_rows:
                policy_names.setdefault(row["tablename"], set()).add(row["policyname"])
            for table in HEALTH_TABLES:
                assert f"deny_anon_{table}" in policy_names.get(table, set())
                assert f"owner_scoped_{table}" in policy_names.get(table, set())

            privilege_rows = await conn.fetch(
                """
                SELECT table_name, grantee, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema = 'mediator'
                  AND table_name = ANY($1::text[])
                  AND grantee IN ('anon', 'authenticated')
                """,
                list(HEALTH_TABLES),
            )
            assert privilege_rows == []

            unique_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE n.nspname = 'mediator'
                      AND t.relname = 'health_source_records'
                      AND c.contype = 'u'
                      AND pg_get_constraintdef(c.oid) LIKE
                          'UNIQUE (connection_id, resource_type, external_id)%'
                )
                """
            )
            assert unique_exists is True

            await conn.execute(DOWN_SQL)
            remaining_tables = await conn.fetch(
                """
                SELECT tablename
                FROM pg_tables
                WHERE schemaname = 'mediator'
                  AND tablename = ANY($1::text[])
                """,
                list(HEALTH_TABLES),
            )
            assert remaining_tables == []
        finally:
            await conn.close()
    finally:
        if admin_conn.is_closed():
            admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
        try:
            await admin_conn.execute(
                "SELECT pg_terminate_backend(pid) "
                "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                db_name,
            )
            await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
        finally:
            await admin_conn.close()
