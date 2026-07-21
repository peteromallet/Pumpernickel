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


# ---------------------------------------------------------------------------
# Migration 0064 — measurement fan-out composite UNIQUE
# ---------------------------------------------------------------------------

MIGRATION_NUMBER_0064 = "0064"
UP_PATH_0064 = MIGRATIONS_DIR / f"{MIGRATION_NUMBER_0064}_health_measurement_fan_out.sql"
DOWN_PATH_0064 = MIGRATIONS_DIR / f"{MIGRATION_NUMBER_0064}_health_measurement_fan_out.down.sql"
UP_SQL_0064 = UP_PATH_0064.read_text()
DOWN_SQL_0064 = DOWN_PATH_0064.read_text()


def test_0064_files_exist_and_are_numbered_pair() -> None:
    assert UP_PATH_0064.exists()
    assert DOWN_PATH_0064.exists()


def test_0064_up_replaces_single_unique_with_composite() -> None:
    compacted = _compact(UP_SQL_0064)
    assert "begin;" in compacted
    assert "commit;" in compacted
    assert (
        "drop constraint if exists health_normalized_measurements_source_record_id_key"
        in compacted
    )
    assert (
        "add constraint health_normalized_measurements_source_metric_key unique (source_record_id, metric)"
        in compacted
    )
    # Must drop the old before adding the new.
    drop_pos = compacted.index("drop constraint if exists")
    add_pos = compacted.index("add constraint")
    assert drop_pos < add_pos
    # Must not touch RLS or indexes — no ALTER TABLE for those.
    assert "force row level security" not in compacted
    assert "enable row level security" not in compacted
    assert "revoke all" not in compacted


def test_0064_up_preserves_existing_index_and_does_not_touch_sleep() -> None:
    compacted = _compact(UP_SQL_0064)
    # This migration must NOT alter the sleep table at all.
    assert "alter table mediator.health_normalized_sleep" not in compacted
    # It must NOT issue CREATE INDEX or DROP INDEX for the measurement index.
    assert (
        "create index idx_health_normalized_measurements_user_metric_measured"
        not in compacted
    )
    assert (
        "drop index idx_health_normalized_measurements_user_metric_measured"
        not in compacted
    )


def test_0064_down_reinstates_single_column_unique() -> None:
    compacted = _compact(DOWN_SQL_0064)
    assert "begin;" in compacted
    assert "commit;" in compacted
    assert (
        "drop constraint if exists health_normalized_measurements_source_metric_key"
        in compacted
    )
    assert (
        "add constraint health_normalized_measurements_source_record_id_key unique (source_record_id)"
        in compacted
    )
    drop_pos = compacted.index("drop constraint if exists")
    add_pos = compacted.index("add constraint")
    assert drop_pos < add_pos


@pytest.mark.postgres
@pytest.mark.anyio
async def test_0064_measurement_fan_out_and_sleep_uniqueness() -> None:
    """Prove measurement fan-out via composite UNIQUE while sleep stays 1:1."""
    asyncpg = pytest.importorskip("asyncpg")

    admin_dsn = os.environ.get("TEST_DATABASE_URL")
    if not admin_dsn:
        pytest.skip("TEST_DATABASE_URL unset; migration validation requires it")

    admin_conn = await asyncpg.connect(admin_dsn, statement_cache_size=0)
    db_name = f"health_0064_{uuid4().hex[:12]}"
    test_dsn = _database_dsn(admin_dsn, db_name)
    try:
        for role in ("anon", "authenticated", "service_role"):
            await admin_conn.execute(
                f"DO $$ BEGIN CREATE ROLE {role}; "
                f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
            )
        await admin_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
        await admin_conn.execute(f'CREATE DATABASE "{db_name}";')

        conn = await asyncpg.connect(test_dsn, statement_cache_size=0)
        try:
            await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
            await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
            await conn.execute(
                """
                CREATE OR REPLACE FUNCTION auth.uid()
                RETURNS uuid LANGUAGE sql STABLE
                AS $$ SELECT NULL::uuid $$;
                """
            )
            await conn.execute(
                f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
            )
            await conn.execute("SET search_path TO mediator, public;")

            # Minimal users table so health migrations' FK references resolve.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mediator.users (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
                );
                """
            )
            # Stub tables referenced by health_source_to_event_projections FKs.
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mediator.events (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
                );
                """
            )
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mediator.commitments (
                    id uuid PRIMARY KEY DEFAULT gen_random_uuid()
                );
                """
            )

            # Apply 0063 then 0064 as a self-contained foundation.
            await conn.execute(UP_SQL)
            await conn.execute(UP_SQL_0064)

            # ------------------------------------------------------------------
            # Seed a connection + source record so FKs are satisfied.
            # ------------------------------------------------------------------
            uid = str(uuid4())
            cid = str(uuid4())
            await conn.execute(
                "INSERT INTO mediator.users (id) VALUES ($1::uuid)", uid
            )
            await conn.execute(
                """
                INSERT INTO mediator.health_connections
                    (id, user_id, provider)
                VALUES ($1::uuid, $2::uuid, 'withings')
                """,
                cid,
                uid,
            )

            src_id1 = str(uuid4())
            await conn.execute(
                """
                INSERT INTO mediator.health_source_records
                    (id, connection_id, user_id, provider, resource_type, external_id)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'withings', 'measurement', 'ext-1')
                """,
                src_id1,
                cid,
                uid,
            )

            src_id2 = str(uuid4())
            await conn.execute(
                """
                INSERT INTO mediator.health_source_records
                    (id, connection_id, user_id, provider, resource_type, external_id)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'withings', 'sleep', 'ext-2')
                """,
                src_id2,
                cid,
                uid,
            )

            # ------------------------------------------------------------------
            # 1. Fan-out: same source_record_id, two different metrics → OK.
            # ------------------------------------------------------------------
            await conn.execute(
                """
                INSERT INTO mediator.health_normalized_measurements
                    (source_record_id, connection_id, user_id, metric,
                     measured_at, value_numeric, canonical_unit)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'weight',
                        now(), 72.5, 'kg')
                """,
                src_id1,
                cid,
                uid,
            )
            await conn.execute(
                """
                INSERT INTO mediator.health_normalized_measurements
                    (source_record_id, connection_id, user_id, metric,
                     measured_at, value_numeric, canonical_unit)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'fat_ratio',
                        now(), 18.3, 'percent')
                """,
                src_id1,
                cid,
                uid,
            )

            fan_out = await conn.fetch(
                """
                SELECT metric FROM mediator.health_normalized_measurements
                WHERE source_record_id = $1::uuid
                ORDER BY metric
                """,
                src_id1,
            )
            metrics = [r["metric"] for r in fan_out]
            assert metrics == ["fat_ratio", "weight"], f"unexpected fan-out: {metrics}"

            # ------------------------------------------------------------------
            # 2. Duplicate (same source_record_id + same metric) must be rejected.
            # ------------------------------------------------------------------
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO mediator.health_normalized_measurements
                        (source_record_id, connection_id, user_id, metric,
                         measured_at, value_numeric, canonical_unit)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, 'weight',
                            now(), 73.0, 'kg')
                    """,
                    src_id1,
                    cid,
                    uid,
                )

            # ------------------------------------------------------------------
            # 3. Sleep: still one row per source_record_id (its own UNIQUE).
            # ------------------------------------------------------------------
            await conn.execute(
                """
                INSERT INTO mediator.health_normalized_sleep
                    (source_record_id, connection_id, user_id,
                     started_at, ended_at, local_sleep_date)
                VALUES ($1::uuid, $2::uuid, $3::uuid,
                        '2025-01-01 22:00+00', '2025-01-02 06:00+00',
                        '2025-01-02')
                """,
                src_id2,
                cid,
                uid,
            )
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO mediator.health_normalized_sleep
                        (source_record_id, connection_id, user_id,
                         started_at, ended_at, local_sleep_date)
                    VALUES ($1::uuid, $2::uuid, $3::uuid,
                            '2025-01-02 22:00+00', '2025-01-03 06:00+00',
                            '2025-01-03')
                    """,
                    src_id2,
                    cid,
                    uid,
                )

            # ------------------------------------------------------------------
            # 4. FK cascade: deleting a source record cascades to normalized rows.
            # ------------------------------------------------------------------
            await conn.execute(
                "DELETE FROM mediator.health_source_records WHERE id = $1::uuid",
                src_id1,
            )
            remaining = await conn.fetchval(
                "SELECT count(*) FROM mediator.health_normalized_measurements WHERE source_record_id = $1::uuid",
                src_id1,
            )
            assert remaining == 0

            # ------------------------------------------------------------------
            # 5. Index still exists.
            # ------------------------------------------------------------------
            idx_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM pg_indexes
                    WHERE indexname = 'idx_health_normalized_measurements_user_metric_measured'
                )
                """
            )
            assert idx_exists is True

            # ------------------------------------------------------------------
            # 6. RLS and deny-anon posture preserved.
            # ------------------------------------------------------------------
            rls_info = await conn.fetchrow(
                """
                SELECT c.relrowsecurity AS rls_enabled,
                       c.relforcerowsecurity AS rls_forced
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = 'mediator'
                  AND c.relname = 'health_normalized_measurements'
                """
            )
            assert rls_info["rls_enabled"] is True
            assert rls_info["rls_forced"] is True

            policy_rows = await conn.fetch(
                """
                SELECT policyname FROM pg_policies
                WHERE schemaname = 'mediator'
                  AND tablename = 'health_normalized_measurements'
                """
            )
            policy_names = {r["policyname"] for r in policy_rows}
            assert "deny_anon_health_normalized_measurements" in policy_names
            assert "owner_scoped_health_normalized_measurements" in policy_names

            # ------------------------------------------------------------------
            # 7. Down migration: revert to single-column UNIQUE.
            # ------------------------------------------------------------------
            await conn.execute(DOWN_SQL_0064)

            # Now composite UNIQUE is gone — single-column is back.
            # Verify we can insert a row with a new source_record_id.
            src_id3 = str(uuid4())
            await conn.execute(
                """
                INSERT INTO mediator.health_source_records
                    (id, connection_id, user_id, provider, resource_type, external_id)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'withings', 'measurement', 'ext-3')
                """,
                src_id3,
                cid,
                uid,
            )
            await conn.execute(
                """
                INSERT INTO mediator.health_normalized_measurements
                    (source_record_id, connection_id, user_id, metric,
                     measured_at, value_numeric, canonical_unit)
                VALUES ($1::uuid, $2::uuid, $3::uuid, 'weight',
                        now(), 80.0, 'kg')
                """,
                src_id3,
                cid,
                uid,
            )
            # A second row with the same source_record_id (even diff metric)
            # must now be rejected by the single-column UNIQUE.
            with pytest.raises(asyncpg.exceptions.UniqueViolationError):
                await conn.execute(
                    """
                    INSERT INTO mediator.health_normalized_measurements
                        (source_record_id, connection_id, user_id, metric,
                         measured_at, value_numeric, canonical_unit)
                    VALUES ($1::uuid, $2::uuid, $3::uuid, 'fat_ratio',
                            now(), 20.0, 'percent')
                    """,
                    src_id3,
                    cid,
                    uid,
                )

            # Verify the composite constraint is gone and single-column is back.
            single_col_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE n.nspname = 'mediator'
                      AND t.relname = 'health_normalized_measurements'
                      AND c.contype = 'u'
                      AND pg_get_constraintdef(c.oid) =
                          'UNIQUE (source_record_id)'
                )
                """
            )
            assert single_col_exists is True

            composite_exists = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint c
                    JOIN pg_class t ON t.oid = c.conrelid
                    JOIN pg_namespace n ON n.oid = t.relnamespace
                    WHERE n.nspname = 'mediator'
                      AND t.relname = 'health_normalized_measurements'
                      AND c.contype = 'u'
                      AND pg_get_constraintdef(c.oid) =
                          'UNIQUE (source_record_id, metric)'
                )
                """
            )
            assert composite_exists is False

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
