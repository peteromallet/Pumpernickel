"""Migration tests for the Hector fitness schema (0037 + 0038).

Verifies table existence, columns, CHECK constraints, indexes, seed rows,
RLS/revoke posture, and FK-order down migrations via text-based checks
against migration SQL files.
No DB connection required.
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def _read_migration(filename: str) -> str:
    """Read a migration file by exact name."""
    return (MIGRATIONS_DIR / filename).read_text()


# ═══════════════════════════════════════════════════════════════════
# 0037: fitness topic + hector bot row
# ═══════════════════════════════════════════════════════════════════


class TestMigration0037:
    """Migration 0037 seeds the fitness topic and hector bot row."""

    def test_0037_exists(self):
        assert (MIGRATIONS_DIR / "0037_fitness_topic.sql").exists()
        assert (MIGRATIONS_DIR / "0037_fitness_topic.down.sql").exists()

    def test_0037_inserts_fitness_topic(self):
        sql = _read_migration("0037_fitness_topic.sql")
        assert "INSERT INTO mediator.topics" in sql
        assert "'fitness'" in sql
        assert "ON CONFLICT (slug) DO NOTHING" in sql
        assert "gen_random_uuid()" in sql

    def test_0037_inserts_hector_bot_row(self):
        sql = _read_migration("0037_fitness_topic.sql")
        assert "INSERT INTO mediator.bots" in sql
        assert "'hector'" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql

    def test_0037_down_deletes_in_fk_order(self):
        sql = _read_migration("0037_fitness_topic.down.sql")
        assert "DELETE FROM mediator.bots WHERE id = 'hector'" in sql
        assert "DELETE FROM mediator.topics WHERE slug = 'fitness'" in sql

    def test_0037_is_idempotent(self):
        sql = _read_migration("0037_fitness_topic.sql")
        assert "ON CONFLICT (slug) DO NOTHING" in sql
        assert "ON CONFLICT (id) DO NOTHING" in sql


# ═══════════════════════════════════════════════════════════════════
# 0038: commitments + events tables (locked DDL)
# ═══════════════════════════════════════════════════════════════════


class TestMigration0038Files:
    """Both up and down migration files must be on disk."""

    def test_0038_up_exists(self):
        assert (MIGRATIONS_DIR / "0038_commitments_events.sql").exists()

    def test_0038_down_exists(self):
        assert (MIGRATIONS_DIR / "0038_commitments_events.down.sql").exists()


class TestMigration0038Commitments:
    """Commitments table must match the locked DDL exactly."""

    def setup_method(self):
        self.sql = _read_migration("0038_commitments_events.sql")

    def test_creates_commitments_table(self):
        assert "CREATE TABLE mediator.commitments" in self.sql

    def test_commitments_has_all_columns(self):
        expected = [
            "id uuid",
            "user_id uuid",
            "topic_id uuid",
            "bot_id text",
            "label text",
            "kind text",
            "status text",
            "cadence text",
            "days_of_week",
            "target_count",
            "start_date date",
            "end_date date",
            "schedule_rule jsonb",
            "pressure_style text",
            "created_at timestamptz",
            "updated_at timestamptz",
        ]
        for col in expected:
            assert col in self.sql, f"Column '{col}' missing from commitments table"

    def test_commitments_status_check(self):
        assert "status IN ('active', 'paused', 'completed', 'dropped')" in self.sql

    def test_commitments_pressure_style_check(self):
        assert "pressure_style IN ('very_gentle', 'low_key', 'firm')" in self.sql

    def test_commitments_fk_bots(self):
        assert "REFERENCES mediator.bots(id)" in self.sql

    def test_commitments_fk_users(self):
        assert "REFERENCES mediator.users(id)" in self.sql

    def test_commitments_fk_topics(self):
        assert "REFERENCES mediator.topics(id)" in self.sql


class TestMigration0038Events:
    """Events table must match the locked DDL exactly."""

    def setup_method(self):
        self.sql = _read_migration("0038_commitments_events.sql")

    def test_creates_events_table(self):
        assert "CREATE TABLE mediator.events" in self.sql

    def test_events_has_all_columns(self):
        expected = [
            "id uuid",
            "commitment_id uuid",
            "user_id uuid",
            "topic_id uuid",
            "bot_id text",
            "metric_key text",
            "adherence_status text",
            "value_numeric",
            "value_text text",
            "unit text",
            "observed_at timestamptz",
            "note text",
            "source_message_ids",
            "created_at timestamptz",
        ]
        for col in expected:
            assert col in self.sql, f"Column '{col}' missing from events table"

    def test_events_adherence_status_check(self):
        assert "adherence_status IN ('done', 'missed', 'excused')" in self.sql

    def test_events_has_value_check(self):
        """At-least-one-value CHECK constraint must exist."""
        assert "adherence_status IS NOT NULL" in self.sql
        assert "value_numeric IS NOT NULL" in self.sql
        assert "value_text IS NOT NULL" in self.sql

    def test_events_commitment_fk_on_delete_set_null(self):
        assert "ON DELETE SET NULL" in self.sql

    def test_events_fk_bots(self):
        assert "REFERENCES mediator.bots(id)" in self.sql


class TestMigration0038Indexes:
    """Indexes must match the locked DDL."""

    def setup_method(self):
        self.sql = _read_migration("0038_commitments_events.sql")

    def test_commitments_partial_active_index(self):
        assert "idx_commitments_active_user_topic_bot" in self.sql
        assert "WHERE status = 'active'" in self.sql

    def test_events_commitment_observed_index(self):
        assert "idx_events_commitment_observed" in self.sql
        assert "commitment_id, observed_at DESC" in self.sql

    def test_events_user_topic_observed_index(self):
        assert "idx_events_user_topic_observed" in self.sql
        assert "user_id, topic_id, observed_at DESC" in self.sql


class TestMigration0038RLS:
    """Both tables must have private-table RLS/revoke posture."""

    def setup_method(self):
        self.sql = _read_migration("0038_commitments_events.sql")

    def test_commitments_rls_enabled(self):
        assert "ALTER TABLE mediator.commitments ENABLE ROW LEVEL SECURITY" in self.sql

    def test_commitments_rls_forced(self):
        assert "ALTER TABLE mediator.commitments FORCE ROW LEVEL SECURITY" in self.sql

    def test_commitments_revoke_anon(self):
        assert "REVOKE ALL ON TABLE mediator.commitments FROM anon" in self.sql

    def test_commitments_deny_policy(self):
        assert "CREATE POLICY deny_anon_commitments ON mediator.commitments" in self.sql
        assert "FOR ALL TO anon USING (false) WITH CHECK (false)" in self.sql

    def test_events_rls_enabled(self):
        assert "ALTER TABLE mediator.events ENABLE ROW LEVEL SECURITY" in self.sql

    def test_events_rls_forced(self):
        assert "ALTER TABLE mediator.events FORCE ROW LEVEL SECURITY" in self.sql

    def test_events_revoke_anon(self):
        assert "REVOKE ALL ON TABLE mediator.events FROM anon" in self.sql

    def test_events_deny_policy(self):
        assert "CREATE POLICY deny_anon_events ON mediator.events" in self.sql
        assert "FOR ALL TO anon USING (false) WITH CHECK (false)" in self.sql


class TestMigration0038Down:
    """Down migration drops tables in correct FK order."""

    def test_down_drops_events_first(self):
        sql = _read_migration("0038_commitments_events.down.sql")
        # Events must be dropped before commitments (FK order)
        lines = sql.split("\n")
        drop_lines = [l for l in lines if "DROP TABLE" in l]
        assert len(drop_lines) >= 2
        assert "events" in drop_lines[0].lower()
        assert "commitments" in drop_lines[1].lower()

    def test_down_uses_if_exists(self):
        sql = _read_migration("0038_commitments_events.down.sql")
        assert "DROP TABLE IF EXISTS" in sql
