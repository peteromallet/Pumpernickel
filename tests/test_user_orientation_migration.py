"""Static coverage for migration 0060 (User Orientation storage contract).

These tests inspect the migration text directly (no live database required)
to lock the durable contract documented in the plan:

  * Three ``mediator.user_orientation_*`` tables and their columns/FKs/CHECK
    literals/indexes/RLS posture.
  * FK-safe down ordering that drops policies before tables and children
    before parents.
  * Safe, idempotent policy removal.
  * No durable ``compass_*`` tables, no ``conversation_artifacts`` snapshot
    storage of orientation, and no ``commitments.orientation_goal_id``
    column.

The migration is wrapped in ``BEGIN;`` / ``COMMIT;`` so transactionality is
verified too.
"""

from pathlib import Path


MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
MIGRATION_NUMBER = "0060"
UP_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_user_orientation.sql"
DOWN_PATH = MIGRATIONS_DIR / f"{MIGRATION_NUMBER}_user_orientation.down.sql"
MANIFESTATIONS_UP_PATH = MIGRATIONS_DIR / "0062_orientation_manifestations.sql"
MANIFESTATIONS_DOWN_PATH = MIGRATIONS_DIR / "0062_orientation_manifestations.down.sql"
UP_SQL = UP_PATH.read_text()
DOWN_SQL = DOWN_PATH.read_text()
MANIFESTATIONS_UP_SQL = MANIFESTATIONS_UP_PATH.read_text()
MANIFESTATIONS_DOWN_SQL = MANIFESTATIONS_DOWN_PATH.read_text()


def _compact(sql: str) -> str:
    """Collapse whitespace and lowercase so assertions are layout-agnostic."""
    return " ".join(sql.lower().split())


def _ddl_only(sql: str) -> str:
    """Return the SQL with ``--`` line comments stripped.

    Forbidden tokens (compass_*, conversation_artifacts, commitments column
    adds) are *allowed* in boundary-documenting comments — that is exactly how
    the migration documents the locked boundary.  Stripping comments lets us
    assert that none of those tokens appear in executable DDL.
    """
    kept_lines = []
    for line in sql.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("--"):
            continue
        # Strip trailing inline comments after code (none expected here, but
        # be defensive: only the leading-dash comment style is used).
        if "--" in line:
            line = line.split("--", 1)[0]
        kept_lines.append(line)
    return _compact("\n".join(kept_lines))


# ---------------------------------------------------------------------------
# Existence + transactionality
# ---------------------------------------------------------------------------


def test_0060_files_exist_and_are_numbered_pair() -> None:
    numbered = sorted(
        path.name
        for path in MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")
        if not path.name.endswith(".down.sql")
    )
    # 0060 is the user-orientation migration; later sprints may add higher
    # numbered migrations, so we only assert it is present and unique.
    assert sum(1 for name in numbered if name.startswith(f"{MIGRATION_NUMBER}_")) == 1
    assert UP_PATH.exists()
    assert DOWN_PATH.exists()


def test_0060_up_is_wrapped_in_single_transaction() -> None:
    lowered = UP_SQL.lower()
    assert "begin;" in lowered
    assert "commit;" in lowered
    assert lowered.index("begin;") < lowered.index("create table mediator.user_orientation_items")
    assert lowered.index("create table mediator.user_orientation_item_reviews") < lowered.index("commit;")


def test_0060_down_is_wrapped_in_single_transaction() -> None:
    lowered = DOWN_SQL.lower()
    assert "begin;" in lowered
    assert "commit;" in lowered
    assert lowered.index("begin;") < lowered.index("drop policy")
    assert lowered.index("drop table") < lowered.index("commit;")


# ---------------------------------------------------------------------------
# Tables + columns
# ---------------------------------------------------------------------------


def test_0060_creates_exactly_three_user_orientation_tables() -> None:
    ddl = _ddl_only(UP_SQL)
    assert "create table mediator.user_orientation_items" in ddl
    assert "create table mediator.user_orientation_item_links" in ddl
    assert "create table mediator.user_orientation_item_reviews" in ddl
    # No other user_orientation_* tables sneak in.
    assert ddl.count("create table mediator.user_orientation_") == 3


def test_0060_items_table_has_required_columns() -> None:
    ddl = _ddl_only(UP_SQL)
    # Slice to the items table body so we don't accidentally assert against
    # the links/reviews tables.
    start = ddl.index("create table mediator.user_orientation_items")
    body = ddl[start : ddl.index("create table mediator.user_orientation_item_links")]
    for column in (
        "id",
        "user_id",
        "topic_id",
        "bot_id",
        "created_by_turn_id",
        "kind",
        "status",
        "source",
        "review_state",
        "label",
        "detail",
        "started_at",
        "effective_at",
        "target_date",
        "completed_at",
        "closed_reason",
        "outcome_note",
        "supersedes_item_id",
        "priority_rank",
        "created_at",
        "updated_at",
    ):
        assert column in body, f"missing column {column} in user_orientation_items"


def test_0060_links_table_has_required_columns() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_item_links")
    body = ddl[start : ddl.index("create table mediator.user_orientation_item_reviews")]
    for column in (
        "id",
        "item_id",
        "user_id",
        "topic_id",
        "target_table",
        "target_id",
        "relation",
        "note",
        "created_at",
    ):
        assert column in body, f"missing column {column} in user_orientation_item_links"


def test_0060_reviews_table_has_required_columns() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_item_reviews")
    # End of the reviews table body is the start of the indexes section.
    body = ddl[start : ddl.index("create index")]
    for column in (
        "id",
        "item_id",
        "user_id",
        "reviewed_by_turn_id",
        "verdict",
        "previous_status",
        "new_status",
        "note",
        "created_at",
    ):
        assert column in body, f"missing column {column} in user_orientation_item_reviews"


# ---------------------------------------------------------------------------
# Foreign keys
# ---------------------------------------------------------------------------


def test_0060_items_table_foreign_keys() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_items")
    body = ddl[start : ddl.index("create table mediator.user_orientation_item_links")]
    assert "references mediator.users(id)" in body
    assert "references mediator.topics(id)" in body
    assert "references mediator.bots(id)" in body
    assert "references mediator.bot_turns(id) on delete set null" in body
    # Self-reference for supersession chains.
    assert "references mediator.user_orientation_items(id) on delete set null" in body


def test_0060_links_table_foreign_keys() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_item_links")
    body = ddl[start : ddl.index("create table mediator.user_orientation_item_reviews")]
    assert (
        "references mediator.user_orientation_items(id) on delete cascade" in body
    )
    assert "references mediator.users(id)" in body
    assert "references mediator.topics(id)" in body


def test_0060_reviews_table_foreign_keys() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_item_reviews")
    body = ddl[start : ddl.index("create index")]
    assert (
        "references mediator.user_orientation_items(id) on delete cascade" in body
    )
    assert "references mediator.users(id)" in body
    assert "references mediator.bot_turns(id) on delete set null" in body


def test_0060_links_target_is_untyped_uuid_not_hard_fk() -> None:
    """Links point at commitments/events as evidence only — they are NOT a
    hard FK to those tables, because the durable execution tables are
    authoritative and the link is evidence/progress metadata."""
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_item_links")
    body = ddl[start : ddl.index("create table mediator.user_orientation_item_reviews")]
    assert "target_id uuid not null" in body
    # Crucially, target_id must NOT carry a hard FK to commitments or events.
    assert "target_id uuid not null references mediator.commitments" not in body
    assert "target_id uuid not null references mediator.events" not in body


# ---------------------------------------------------------------------------
# CHECK constraints (literal forms)
# ---------------------------------------------------------------------------


def test_0060_items_check_literals() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_items")
    body = ddl[start : ddl.index("create table mediator.user_orientation_item_links")]
    assert "check (kind in ('principle', 'goal', 'priority', 'anti_pattern'))" in body
    assert (
        "check (status in ('pending', 'active', 'completed', 'retired', 'superseded', 'rejected'))"
        in body
    )
    assert (
        "check (source in ('user_stated', 'user_confirmed', 'bot_proposed'))" in body
    )
    assert (
        "check (review_state in ('unreviewed', 'reviewed', 'excluded'))" in body
    )
    # Label must be non-blank.
    assert "check (length(btrim(label)) > 0)" in body
    # priority_rank is NULL or >= 1.
    assert "check (priority_rank is null or priority_rank >= 1)" in body
    # completed requires completed_at.
    assert "status <> 'completed'" in body
    assert "completed_at is not null" in body
    # bot_proposed rows must stay unreviewed/excluded.
    assert "source <> 'bot_proposed'" in body
    assert "review_state in ('unreviewed', 'excluded')" in body
    # Self-supersession guard.
    assert (
        "check (supersedes_item_id is null or supersedes_item_id <> id)" in body
    )


def test_0062_manifestations_widens_orientation_kind_check() -> None:
    ddl = _ddl_only(MANIFESTATIONS_UP_SQL)
    assert "drop constraint if exists user_orientation_items_kind_check" in ddl
    assert (
        "check (kind in ('principle', 'manifestation', 'goal', 'priority', 'anti_pattern'))"
        in ddl
    )
    assert (
        "add constraint user_orientation_items_manifestation_target_date_check"
        in ddl
    )
    assert "check (kind <> 'manifestation' or target_date is not null)" in ddl


def test_0062_down_restores_prior_orientation_kind_check() -> None:
    ddl = _ddl_only(MANIFESTATIONS_DOWN_SQL)
    assert (
        "drop constraint if exists user_orientation_items_manifestation_target_date_check"
        in ddl
    )
    assert "drop constraint if exists user_orientation_items_kind_check" in ddl
    assert "check (kind in ('principle', 'goal', 'priority', 'anti_pattern'))" in ddl


def test_0060_links_check_literals() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_item_links")
    body = ddl[start : ddl.index("create table mediator.user_orientation_item_reviews")]
    assert (
        "check (target_table in ('commitments', 'events'))" in body
    )
    assert (
        "check (relation in ('evidence', 'progress', 'supports', 'contradicts', 'completes'))"
        in body
    )
    # Evidence link uniqueness.
    assert "unique (item_id, target_table, target_id, relation)" in body


def test_0060_reviews_check_literals() -> None:
    ddl = _ddl_only(UP_SQL)
    start = ddl.index("create table mediator.user_orientation_item_reviews")
    body = ddl[start : ddl.index("create index")]
    assert (
        "check (verdict in ('accepted', 'corrected', 'rejected', 'retired', 'superseded', 'completed'))"
        in body
    )
    assert (
        "check (previous_status is null or previous_status in ('pending', 'active', 'completed', 'retired', 'superseded', 'rejected'))"
        in body
    )
    assert (
        "check (new_status in ('pending', 'active', 'completed', 'retired', 'superseded', 'rejected'))"
        in body
    )


# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------


def test_0060_creates_required_indexes_including_compass_partials() -> None:
    ddl = _ddl_only(UP_SQL)
    for index_fragment in (
        # Compass primary lookup (active rows only).
        "create index idx_user_orientation_items_active_user_topic on mediator.user_orientation_items (user_id, topic_id, kind) where status = 'active'",
        # Review/unreviewed lookup (pending + active).
        "create index idx_user_orientation_items_open_user on mediator.user_orientation_items (user_id, status) where status in ('pending', 'active')",
        # Supersession chain traversal.
        "create index idx_user_orientation_items_supersedes on mediator.user_orientation_items (supersedes_item_id) where supersedes_item_id is not null",
        # Deterministic per-user listing.
        "create index idx_user_orientation_items_user_kind_status on mediator.user_orientation_items (user_id, kind, status, created_at)",
        # Reverse link lookup.
        "create index idx_user_orientation_item_links_target on mediator.user_orientation_item_links (target_table, target_id)",
        # Forward evidence lookup.
        "create index idx_user_orientation_item_links_item on mediator.user_orientation_item_links (item_id, relation)",
        # Review history.
        "create index idx_user_orientation_item_reviews_item_created on mediator.user_orientation_item_reviews (item_id, created_at)",
    ):
        assert index_fragment in ddl, f"missing index: {index_fragment}"


# ---------------------------------------------------------------------------
# RLS / revokes / policies
# ---------------------------------------------------------------------------


def test_0060_enables_and_forces_rls_on_all_three_tables() -> None:
    ddl = _ddl_only(UP_SQL)
    for table in (
        "user_orientation_items",
        "user_orientation_item_links",
        "user_orientation_item_reviews",
    ):
        assert (
            f"alter table mediator.{table} enable row level security" in ddl
        ), f"RLS not enabled on {table}"
        assert (
            f"alter table mediator.{table} force row level security" in ddl
        ), f"RLS not forced on {table}"


def test_0060_revokes_all_from_anon_and_authenticated() -> None:
    ddl = _ddl_only(UP_SQL)
    for table in (
        "user_orientation_items",
        "user_orientation_item_links",
        "user_orientation_item_reviews",
    ):
        assert (
            f"revoke all on table mediator.{table} from anon, authenticated" in ddl
        ), f"REVOKE ALL missing on {table}"


def test_0060_creates_deny_and_owner_scoped_policies_on_all_three_tables() -> None:
    ddl = _ddl_only(UP_SQL)
    for table in (
        "user_orientation_items",
        "user_orientation_item_links",
        "user_orientation_item_reviews",
    ):
        deny_policy = f"create policy deny_anon_{table} on mediator.{table} for all to anon, authenticated using (false) with check (false)"
        owner_policy = (
            f"create policy owner_scoped_{table} on mediator.{table} for all using (user_id = auth.uid()) with check (user_id = auth.uid())"
        )
        assert deny_policy in ddl, f"missing deny policy on {table}"
        assert owner_policy in ddl, f"missing owner-scoped policy on {table}"


def test_0060_owner_policies_bind_directly_on_user_id_not_conversation() -> None:
    """Orientation state is per-user and must NEVER mix participants.  The
    policies scope DIRECTLY on user_id (not via conversations.user_id /
    partner_user_id the way conversation_artifacts does)."""
    ddl = _ddl_only(UP_SQL)
    # owner-scoped policies must reference auth.uid() against user_id.
    owner_block_count = ddl.count("using (user_id = auth.uid())")
    assert owner_block_count == 3
    # They must NOT scope through conversations.
    assert "conversations.user_id = auth.uid()" not in ddl
    assert "conversations.partner_user_id = auth.uid()" not in ddl


# ---------------------------------------------------------------------------
# Down migration: FK-safe ordering + safe policy removal
# ---------------------------------------------------------------------------


def test_0060_down_drops_policies_before_tables() -> None:
    ddl = _ddl_only(DOWN_SQL)
    first_drop_policy = ddl.index("drop policy")
    first_drop_table = ddl.index("drop table")
    assert first_drop_policy < first_drop_table


def test_0060_down_drops_tables_in_fk_safe_child_first_order() -> None:
    ddl = _ddl_only(DOWN_SQL)
    items_pos = ddl.index("drop table if exists mediator.user_orientation_items")
    links_pos = ddl.index("drop table if exists mediator.user_orientation_item_links")
    reviews_pos = ddl.index(
        "drop table if exists mediator.user_orientation_item_reviews"
    )
    # reviews and links both FK to items (with ON DELETE CASCADE), so they
    # must be dropped before items.
    assert reviews_pos < items_pos
    assert links_pos < items_pos


def test_0060_down_removes_all_six_policies_idempotently() -> None:
    ddl = _ddl_only(DOWN_SQL)
    for table in (
        "user_orientation_items",
        "user_orientation_item_links",
        "user_orientation_item_reviews",
    ):
        assert (
            f"drop policy if exists deny_anon_{table} on mediator.{table}" in ddl
        ), f"missing deny policy drop for {table}"
        assert (
            f"drop policy if exists owner_scoped_{table} on mediator.{table}" in ddl
        ), f"missing owner-scoped policy drop for {table}"


def test_0060_down_uses_if_exists_everywhere() -> None:
    """Down migration must be safe to re-apply — every DROP statement uses
    IF EXISTS."""
    ddl = _ddl_only(DOWN_SQL)
    # Parse statements by semicolon (DDL is semicolon-terminated).
    statements = [stmt.strip() for stmt in ddl.split(";") if stmt.strip()]
    drop_statements = [stmt for stmt in statements if stmt.startswith("drop ")]
    assert drop_statements, "expected at least one DROP statement in down migration"
    for stmt in drop_statements:
        assert stmt.startswith("drop policy if exists") or stmt.startswith(
            "drop table if exists"
        ), f"DROP without IF EXISTS: {stmt[:80]}"


# ---------------------------------------------------------------------------
# Forbidden durable Compass storage / snapshot / commitment column
# ---------------------------------------------------------------------------


def test_0060_no_durable_compass_tables() -> None:
    """Compass is a read/service layer — there must be NO durable
    ``compass_*`` tables in either migration."""
    for sql in (UP_SQL, DOWN_SQL):
        ddl = _ddl_only(sql)
        assert "create table mediator.compass_" not in ddl
        assert "create table compass_" not in ddl
        assert "create table if not exists mediator.compass_" not in ddl
        assert "create table if not exists compass_" not in ddl


def test_0060_no_compass_snapshot_storage_in_conversation_artifacts() -> None:
    """Orientation must NOT be stored as a conversation_artifacts snapshot."""
    for sql in (UP_SQL, DOWN_SQL):
        ddl = _ddl_only(sql)
        assert "insert into mediator.conversation_artifacts" not in ddl
        assert "conversation_artifacts.artifact_type" not in ddl
        # No new artifact_type arm representing compass/orientation.
        assert "'compass'" not in ddl.replace("'compass' is a service/read model", "")
        # No new artifact_type literals that would store orientation snapshots.
        assert "artifact_type in ('compass'" not in ddl
        assert "artifact_type = 'compass'" not in ddl


def test_0060_does_not_add_commitments_orientation_goal_id_column() -> None:
    """Goal<->commitment and goal<->event relationships are represented ONLY
    through user_orientation_item_links.  No hard
    ``commitments.orientation_goal_id`` column is added in M1."""
    for sql in (UP_SQL, DOWN_SQL):
        ddl = _ddl_only(sql)
        assert "alter table mediator.commitments" not in ddl
        assert "add column orientation_goal_id" not in ddl
        assert "orientation_goal_id uuid" not in ddl


def test_0060_forbidden_tokens_only_appear_in_comments() -> None:
    """Belt-and-braces: every occurrence of the forbidden boundary tokens
    in the full migration text (including comments) is acceptable because it
    documents the locked boundary.  We re-assert the DDL view is clean and
    that the full text only references them in boundary documentation."""
    forbidden = ("compass_", "conversation_artifacts", "orientation_goal_id")
    for token in forbidden:
        # In DDL-stripped view, the token must NOT appear as executable DDL.
        for sql in (UP_SQL, DOWN_SQL):
            assert token not in _ddl_only(sql), (
                f"forbidden token {token!r} appears in executable DDL of migration"
            )
