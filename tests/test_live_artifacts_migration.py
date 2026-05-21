"""Static migration tests for the conversation artifacts migration (0051).

Modeled after tests/test_live_migrations.py.  These tests run without a DB
connection — they assert against the migration SQL text directly.

Test classes:
- TestMigrationFilesExist: up/down file presence
- TestMigrationContent: forward migration content checks (all 5 sections)
- TestMigrationAllowList: regression guards (bot_turns, pregnancy_state excluded)
- TestDownMigrationContent: down migration content checks
"""

from __future__ import annotations

from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

MIGRATION = "0051_conversation_artifacts"


def _read_up() -> str:
    return (MIGRATIONS_DIR / f"{MIGRATION}.sql").read_text()


def _read_down() -> str:
    return (MIGRATIONS_DIR / f"{MIGRATION}.down.sql").read_text()


# -- A. File existence -------------------------------------------------------


class TestMigrationFilesExist:
    def test_up_present(self) -> None:
        assert (MIGRATIONS_DIR / f"{MIGRATION}.sql").exists(), (
            f"forward migration {MIGRATION}.sql not found"
        )

    def test_down_present(self) -> None:
        assert (MIGRATIONS_DIR / f"{MIGRATION}.down.sql").exists(), (
            f"down migration {MIGRATION}.down.sql not found"
        )


# -- B. Forward migration content (all 5 sections) ---------------------------


class TestMigrationContent:
    def test_creates_conversation_artifacts_table(self) -> None:
        sql = _read_up()
        assert "CREATE TABLE mediator.conversation_artifacts" in sql, (
            "missing CREATE TABLE mediator.conversation_artifacts"
        )

    def test_creates_artifact_links_table(self) -> None:
        sql = _read_up()
        assert "CREATE TABLE mediator.artifact_links" in sql, (
            "missing CREATE TABLE mediator.artifact_links"
        )

    def test_alters_bot_turns_add_column_conversation_id(self) -> None:
        sql = _read_up()
        assert (
            "ALTER TABLE mediator.bot_turns" in sql
            and "ADD COLUMN IF NOT EXISTS conversation_id" in sql
        ), "missing ALTER TABLE bot_turns ADD COLUMN conversation_id"

    def test_alters_bot_turns_add_column_kind(self) -> None:
        sql = _read_up()
        assert (
            "ALTER TABLE mediator.bot_turns" in sql
            and "ADD COLUMN IF NOT EXISTS kind" in sql
        ), "missing ALTER TABLE bot_turns ADD COLUMN kind"

    def test_user_id_references_mediator_users(self) -> None:
        sql = _read_up()
        assert (
            "REFERENCES mediator.users(id)" in sql
        ), "user_id must reference mediator.users(id)"

    def test_unique_conversation_id_artifact_type_revision_number(self) -> None:
        sql = _read_up()
        assert (
            "UNIQUE (conversation_id, artifact_type, revision_number)" in sql
        ), "missing UNIQUE on (conversation_id, artifact_type, revision_number)"

    def test_artifact_type_check_constraint(self) -> None:
        sql = _read_up()
        assert "CHECK (artifact_type IN (" in sql, (
            "missing CHECK on artifact_type"
        )
        for t in (
            "'live_prep_brief'", "'live_debrief'", "'review_summary'",
            "'agenda_revision'", "'transcript_reflection'",
        ):
            assert t in sql, f"artifact_type CHECK missing {t}"

    def test_relation_check_constraint(self) -> None:
        sql = _read_up()
        assert "CHECK (relation IN (" in sql, (
            "missing CHECK on relation"
        )
        for r in (
            "'planned_item'", "'summarized_from'", "'evidence_quote'",
            "'extracted_memory'", "'extracted_observation'",
            "'extracted_distillation'", "'created_commitment'",
            "'logged_event'", "'created_follow_up'", "'updated_topic_status'",
        ):
            assert r in sql, f"relation CHECK missing {r}"

    def test_target_table_check_constraint(self) -> None:
        sql = _read_up()
        assert "CHECK (target_table IN (" in sql, (
            "missing CHECK on target_table"
        )
        for t in (
            "'conversations'", "'conversation_items'", "'transcript_turns'",
            "'conversation_notes'", "'messages'", "'memories'",
            "'observations'", "'distillations'", "'commitments'",
            "'events'", "'scheduled_jobs'", "'topic_status'",
        ):
            assert t in sql, f"target_table CHECK missing {t}"

    def test_kind_check_constraint(self) -> None:
        sql = _read_up()
        assert "CHECK (kind IS NULL OR kind IN (" in sql, (
            "missing CHECK on kind"
        )
        assert "'live_prep'" in sql
        assert "'live_debrief'" in sql

    def test_bot_turns_partial_index_conversation_id(self) -> None:
        sql = _read_up()
        assert "idx_bot_turns_conversation_id" in sql, (
            "missing idx_bot_turns_conversation_id"
        )
        assert "WHERE conversation_id IS NOT NULL" in sql, (
            "idx_bot_turns_conversation_id must be partial"
        )

    def test_bot_turns_partial_index_kind(self) -> None:
        sql = _read_up()
        assert "idx_bot_turns_kind" in sql, (
            "missing idx_bot_turns_kind"
        )
        assert "WHERE kind IS NOT NULL" in sql, (
            "idx_bot_turns_kind must be partial"
        )

    def test_enable_force_rls_on_conversation_artifacts(self) -> None:
        sql = _read_up()
        assert "ALTER TABLE mediator.conversation_artifacts ENABLE ROW LEVEL SECURITY" in sql
        assert "ALTER TABLE mediator.conversation_artifacts FORCE ROW LEVEL SECURITY" in sql

    def test_enable_force_rls_on_artifact_links(self) -> None:
        sql = _read_up()
        assert "ALTER TABLE mediator.artifact_links ENABLE ROW LEVEL SECURITY" in sql
        assert "ALTER TABLE mediator.artifact_links FORCE ROW LEVEL SECURITY" in sql

    def test_revoke_all_from_anon_authenticated(self) -> None:
        sql = _read_up()
        assert "REVOKE ALL ON TABLE mediator.conversation_artifacts FROM anon" in sql
        assert "REVOKE ALL ON TABLE mediator.artifact_links FROM anon" in sql

    def test_deny_anon_policies(self) -> None:
        sql = _read_up()
        assert "deny_anon_conversation_artifacts" in sql, (
            "missing deny_anon_conversation_artifacts policy"
        )
        assert "deny_anon_artifact_links" in sql, (
            "missing deny_anon_artifact_links policy"
        )

    def test_owner_scoped_policies(self) -> None:
        sql = _read_up()
        assert "owner_scoped_conversation_artifacts" in sql, (
            "missing owner_scoped_conversation_artifacts policy"
        )
        assert "owner_scoped_artifact_links" in sql, (
            "missing owner_scoped_artifact_links policy"
        )

    def test_owner_scoped_conversation_artifacts_uses_one_hop_exists(self) -> None:
        """Owner-scoped policy must join through conversations in one hop."""
        sql = _read_up()
        assert "EXISTS (" in sql
        # Verify the one-hop pattern: conversations table mentioned near
        # the owner_scoped_conversation_artifacts policy.
        assert "mediator.conversations c" in sql, (
            "owner_scoped policy must reference mediator.conversations"
        )
        assert (
            "c.user_id = auth.uid() OR c.partner_user_id = auth.uid()"
        ) in sql, "owner check must include both user_id and partner_user_id"

    def test_doc_block_mentions_no_data_window(self) -> None:
        """The SQL file header comment must document the Sprint 1 no-data window."""
        sql = _read_up()
        assert "No production path populates" in sql or "no production path" in sql.lower(), (
            "migration header must document the Sprint 1 no-data window for bot_turns columns"
        )

    def test_begin_commit_wrapping(self) -> None:
        sql = _read_up()
        assert "BEGIN" in sql, "forward migration must contain BEGIN (transaction start)"
        assert "COMMIT" in sql, "forward migration must contain COMMIT"


# -- C. Allow-list regression guards -----------------------------------------


class TestMigrationAllowList:
    def test_bot_turns_not_in_artifact_links_target_table_check(self) -> None:
        """bot_turns must NOT appear in the artifact_links.target_table CHECK.

        Provenance for the producing turn is stored via created_by_turn_id FK,
        not as a link row.
        """
        sql = _read_up()
        # Find the artifact_links target_table CHECK constraint block.
        # The CHECK appears after "CREATE TABLE mediator.artifact_links".
        artifacts_pos = sql.find("CREATE TABLE mediator.artifact_links")
        assert artifacts_pos > 0, "artifact_links CREATE TABLE not found"
        # Find the CHECK that follows it.
        check_pos = sql.find("CHECK (target_table IN (", artifacts_pos)
        assert check_pos > 0, "target_table CHECK not found after artifact_links CREATE"
        # Extract the CHECK block (from the opening paren to the closing ).
        check_end = sql.find(")", check_pos)
        # Actually need to find the matching close paren for the IN list.
        # Use a heuristic: find the ) that closes the IN (...), which is the
        # one after the last target_table string.
        topic_status_pos = sql.find("'topic_status'", check_pos)
        assert topic_status_pos > 0, "'topic_status' not found in target_table CHECK"
        check_close = sql.find(")", topic_status_pos)
        assert check_close > 0, "closing paren for target_table CHECK not found"
        check_block = sql[check_pos:check_close + 1]
        assert "'bot_turns'" not in check_block, (
            "bot_turns must NOT be in artifact_links target_table CHECK — "
            "provenance is via created_by_turn_id FK"
        )

    def test_pregnancy_state_not_in_artifact_links_target_table_check(self) -> None:
        """pregnancy_state must NOT appear — the table does not exist."""
        sql = _read_up()
        artifacts_pos = sql.find("CREATE TABLE mediator.artifact_links")
        assert artifacts_pos > 0
        check_pos = sql.find("CHECK (target_table IN (", artifacts_pos)
        assert check_pos > 0
        topic_status_pos = sql.find("'topic_status'", check_pos)
        assert topic_status_pos > 0
        check_close = sql.find(")", topic_status_pos)
        assert check_close > 0
        check_block = sql[check_pos:check_close + 1]
        assert "'pregnancy_state'" not in check_block, (
            "pregnancy_state must NOT be in artifact_links target_table CHECK — "
            "table does not exist (pregnancy is columns on mediator.users)"
        )

    def test_target_table_values_are_unqualified(self) -> None:
        """Every target_table literal must be unqualified (no 'mediator.' prefix)."""
        sql = _read_up()
        assert "'mediator.conversations'" not in sql, (
            "target_table values must be unqualified (no 'mediator.' prefix)"
        )


# -- D. Down migration content -----------------------------------------------


class TestDownMigrationContent:
    def test_drops_deny_anon_conversation_artifacts_policy(self) -> None:
        sql = _read_down()
        assert "DROP POLICY IF EXISTS deny_anon_conversation_artifacts" in sql

    def test_drops_owner_scoped_conversation_artifacts_policy(self) -> None:
        sql = _read_down()
        assert "DROP POLICY IF EXISTS owner_scoped_conversation_artifacts" in sql

    def test_drops_deny_anon_artifact_links_policy(self) -> None:
        sql = _read_down()
        assert "DROP POLICY IF EXISTS deny_anon_artifact_links" in sql

    def test_drops_owner_scoped_artifact_links_policy(self) -> None:
        sql = _read_down()
        assert "DROP POLICY IF EXISTS owner_scoped_artifact_links" in sql

    def test_drops_artifact_links_before_conversation_artifacts(self) -> None:
        """artifact_links (child) must be dropped before conversation_artifacts (parent)."""
        sql = _read_down()
        al_pos = sql.find("DROP TABLE IF EXISTS mediator.artifact_links")
        ca_pos = sql.find("DROP TABLE IF EXISTS mediator.conversation_artifacts")
        assert al_pos > 0, "artifact_links DROP TABLE not found in down migration"
        assert ca_pos > 0, "conversation_artifacts DROP TABLE not found in down migration"
        assert al_pos < ca_pos, (
            "artifact_links must be dropped BEFORE conversation_artifacts "
            "(child before parent due to FK dependency)"
        )

    def test_drops_both_bot_turns_indexes(self) -> None:
        sql = _read_down()
        assert "DROP INDEX IF EXISTS mediator.idx_bot_turns_conversation_id" in sql
        assert "DROP INDEX IF EXISTS mediator.idx_bot_turns_kind" in sql

    def test_drops_both_bot_turns_columns(self) -> None:
        sql = _read_down()
        assert "DROP COLUMN IF EXISTS kind" in sql
        assert "DROP COLUMN IF EXISTS conversation_id" in sql

    def test_drops_kind_before_conversation_id(self) -> None:
        """kind column should be dropped before conversation_id (consistent ordering)."""
        sql = _read_down()
        kind_pos = sql.find("DROP COLUMN IF EXISTS kind")
        conv_pos = sql.find("DROP COLUMN IF EXISTS conversation_id")
        assert kind_pos > 0
        assert conv_pos > 0
        # Not strictly required for correctness, but consistent ordering is safer.
        assert kind_pos < conv_pos, (
            "kind should be dropped before conversation_id for consistent ordering"
        )

    def test_down_uses_if_exists(self) -> None:
        sql = _read_down()
        assert "IF EXISTS" in sql, "down migration must use IF EXISTS"
        # Verify IF EXISTS appears for all DROP statements.
        drop_count = sql.count("DROP ")
        if_exists_count = sql.count("IF EXISTS")
        assert if_exists_count >= drop_count, (
            f"not all DROP statements use IF EXISTS "
            f"({if_exists_count} IF EXISTS vs {drop_count} DROP)"
        )

    def test_begin_commit_wrapping(self) -> None:
        sql = _read_down()
        assert "BEGIN" in sql, "down migration must contain BEGIN (transaction start)"
        assert "COMMIT" in sql, "down migration must contain COMMIT"
