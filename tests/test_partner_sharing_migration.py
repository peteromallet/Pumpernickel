from __future__ import annotations

from pathlib import Path


MIGRATION = Path("migrations/0035_per_bot_partner_sharing.sql")


def _migration_sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_per_bot_partner_sharing_migration_is_transactional_and_ordered():
    sql = _migration_sql()
    normalized = " ".join(sql.split()).lower()

    assert normalized.startswith("begin;")
    assert normalized.endswith("commit;")
    assert "values ('tante_rosi', 'tante rosi')" in normalized
    assert "add column if not exists partner_share text" in normalized
    assert "user_bot_state_partner_share_check" in normalized
    assert "partner_share in ('opt_in', 'opt_out')" in normalized

    backfill_pos = normalized.index("insert into user_bot_state")
    legacy_drop_pos = normalized.index("drop column cross_thread_sharing_default")
    assert backfill_pos < legacy_drop_pos
    assert "where u.cross_thread_sharing_default is not null" in normalized
    assert "on conflict (user_id, bot_id) do update" in normalized
    assert "set partner_share = excluded.partner_share" in normalized


def test_per_bot_partner_sharing_migration_adds_memory_visibility_contract():
    sql = _migration_sql()
    normalized = " ".join(sql.split()).lower()

    assert (
        "add column if not exists visibility text not null default 'private'"
        in normalized
    )
    assert "add column if not exists shareable_summary text" in normalized
    assert "add column if not exists shareable_summary_encrypted bytea" in normalized
    assert "memories_visibility_check" in normalized
    assert "visibility in ('private', 'dyad_shareable')" in normalized
    assert "memories_shareable_summary_required_check" in normalized
    assert "visibility <> 'dyad_shareable'" in normalized
    assert "length(btrim(shareable_summary)) > 0" in normalized
    assert "idx_memories_shareable_about_bot_recent" in normalized
