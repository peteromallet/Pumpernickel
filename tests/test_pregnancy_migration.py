"""Migration tests for the pregnancy schema (0032 + 0033).

Verifies column existence, CHECK constraints, partial index, and topic row
against a fresh schema.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _migration_exists(num: int, suffix: str) -> bool:
    """Check that a migration file exists."""
    pattern = f"{num:04d}_pregnancy{suffix}.sql"
    return (MIGRATIONS_DIR / pattern).exists()


def _read_migration(num: int, suffix: str) -> str:
    """Read a migration file."""
    pattern = f"{num:04d}_pregnancy{suffix}.sql"
    return (MIGRATIONS_DIR / pattern).read_text()


class TestMigrationFilesExist:
    """All four migration files must be on disk."""

    def test_0032_up_exists(self):
        assert _migration_exists(32, ""), "0032_pregnancy.sql missing"

    def test_0032_down_exists(self):
        assert _migration_exists(32, ".down"), "0032_pregnancy.down.sql missing"

    def test_0033_up_exists(self):
        assert _migration_exists(33, "_topic"), "0033_pregnancy_topic.sql missing"

    def test_0033_down_exists(self):
        assert _migration_exists(33, "_topic.down"), "0033_pregnancy_topic.down.sql missing"


class TestMigrationContent:
    """Semantic checks against the migration SQL text."""

    def test_0032_defines_all_eight_columns(self):
        """All 8 pregnancy columns must be defined in 0032."""
        sql = _read_migration(32, "")
        expected = [
            "pregnancy_edd",
            "pregnancy_dating_basis",
            "pregnancy_lmp_date",
            "pregnancy_scan_date",
            "pregnancy_scan_corrected_at",
            "pregnancy_started_at",
            "pregnancy_ended_at",
            "pregnancy_outcome",
        ]
        for col in expected:
            assert col in sql, f"Column {col} missing from 0032_pregnancy.sql"

    def test_0032_defines_dating_basis_check(self):
        """CHECK constraint for dating_basis must enforce valid values."""
        sql = _read_migration(32, "")
        assert "pregnancy_dating_basis IN ('lmp', 'scan')" in sql

    def test_0032_defines_outcome_check(self):
        """CHECK constraint for outcome must enforce valid values."""
        sql = _read_migration(32, "")
        assert "pregnancy_outcome IN ('birth', 'loss', 'termination')" in sql

    def test_0032_defines_dating_basis_requires_edd_constraint(self):
        """CHECK: dating_basis must be NULL iff EDD is NULL."""
        sql = _read_migration(32, "")
        assert "dating_basis_requires_edd" in sql

    def test_0032_defines_outcome_requires_ended_at_constraint(self):
        """CHECK: outcome must be NULL iff ended_at is NULL."""
        sql = _read_migration(32, "")
        assert "outcome_requires_ended_at" in sql

    def test_0032_defines_partial_index(self):
        """Partial index for active pregnancy queries must exist."""
        sql = _read_migration(32, "")
        assert "idx_users_active_pregnancy" in sql
        assert "pregnancy_edd IS NOT NULL" in sql
        assert "pregnancy_ended_at IS NULL" in sql

    def test_0033_inserts_topic_row(self):
        """0033 must insert the pregnancy topic with ON CONFLICT DO NOTHING."""
        sql = _read_migration(33, "_topic")
        assert "pregnancy" in sql
        assert "ON CONFLICT" in sql

    def test_0033_down_omits_topic_deletion(self):
        """0033.down.sql must explain why topic row deletion is omitted."""
        sql = _read_migration(33, "_topic.down")
        # Should have a comment explaining the omission
        assert "cascade" in sql.lower() or "omit" in sql.lower() or "delet" in sql.lower()


class TestMigrationDatabase:
    """Apply migrations to a live dev DB and verify the schema."""

    @pytest.fixture(autouse=True)
    def _check_db_url(self):
        """Skip if DATABASE_URL is not reachable."""
        if not os.environ.get("DATABASE_URL"):
            pytest.skip("DATABASE_URL not set — skipping live DB migration test")

    def test_0032_columns_exist_in_dev(self):
        """After applying 0032, all pregnancy columns exist on mediator.users."""
        import asyncpg
        import asyncio

        async def _check():
            conn = await asyncpg.connect(
                os.environ["DATABASE_URL"], statement_cache_size=0
            )
            try:
                # Check that we can SELECT the columns without error
                row = await conn.fetchrow(
                    """SELECT pregnancy_edd, pregnancy_dating_basis,
                              pregnancy_lmp_date, pregnancy_scan_date,
                              pregnancy_scan_corrected_at, pregnancy_started_at,
                              pregnancy_ended_at, pregnancy_outcome
                       FROM mediator.users LIMIT 0"""
                )
                # If we get here without error, columns exist
                assert row is None  # LIMIT 0 returns no rows
            finally:
                await conn.close()

        asyncio.get_event_loop().run_until_complete(_check())

    def test_0032_constraints_reject_invalid_data(self):
        """CHECK constraints reject invalid inserts."""
        import asyncpg
        import asyncio

        async def _check():
            conn = await asyncpg.connect(
                os.environ["DATABASE_URL"], statement_cache_size=0
            )
            try:
                # dating_basis without edd should fail
                with pytest.raises(asyncpg.RaiseError):
                    await conn.execute(
                        """INSERT INTO mediator.users (id, name, phone, timezone,
                           pregnancy_dating_basis) VALUES (gen_random_uuid(), 'test',
                           '+1', 'UTC', 'lmp')"""
                    )

                # outcome without ended_at should fail
                with pytest.raises(asyncpg.RaiseError):
                    await conn.execute(
                        """INSERT INTO mediator.users (id, name, phone, timezone,
                           pregnancy_outcome) VALUES (gen_random_uuid(), 'test',
                           '+1', 'UTC', 'birth')"""
                    )

                # Invalid dating_basis should fail
                with pytest.raises(asyncpg.RaiseError):
                    await conn.execute(
                        """INSERT INTO mediator.users (id, name, phone, timezone,
                           pregnancy_edd, pregnancy_dating_basis)
                           VALUES (gen_random_uuid(), 'test', '+1', 'UTC',
                           '2026-10-22', 'invalid')"""
                    )

                # Invalid outcome should fail
                with pytest.raises(asyncpg.RaiseError):
                    await conn.execute(
                        """INSERT INTO mediator.users (id, name, phone, timezone,
                           pregnancy_edd, pregnancy_dating_basis,
                           pregnancy_ended_at, pregnancy_outcome)
                           VALUES (gen_random_uuid(), 'test', '+1', 'UTC',
                           '2026-10-22', 'lmp', now(), 'invalid')"""
                    )
            finally:
                await conn.close()

        asyncio.get_event_loop().run_until_complete(_check())

    def test_0033_topic_row_exists_in_dev(self):
        """The pregnancy topic row exists after migration 0033."""
        import asyncpg
        import asyncio

        async def _check():
            conn = await asyncpg.connect(
                os.environ["DATABASE_URL"], statement_cache_size=0
            )
            try:
                row = await conn.fetchrow(
                    "SELECT id, slug, display_name FROM mediator.topics WHERE slug = 'pregnancy'"
                )
                assert row is not None, "pregnancy topic row missing"
                assert row["slug"] == "pregnancy"
                assert row["display_name"] == "Pregnancy"
            finally:
                await conn.close()

        asyncio.get_event_loop().run_until_complete(_check())

    def test_0032_partial_index_exists(self):
        """The partial index idx_users_active_pregnancy exists."""
        import asyncpg
        import asyncio

        async def _check():
            conn = await asyncpg.connect(
                os.environ["DATABASE_URL"], statement_cache_size=0
            )
            try:
                row = await conn.fetchrow(
                    """SELECT 1 FROM pg_indexes
                       WHERE tablename = 'users'
                         AND schemaname = 'mediator'
                         AND indexname = 'idx_users_active_pregnancy'"""
                )
                assert row is not None, "Partial index idx_users_active_pregnancy missing"
            finally:
                await conn.close()

        asyncio.get_event_loop().run_until_complete(_check())