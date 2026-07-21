from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UP = (ROOT / "migrations/0068_reflection_revision_leaf_semantics.sql").read_text(encoding="utf-8")
DOWN = (ROOT / "migrations/0068_reflection_revision_leaf_semantics.down.sql").read_text(encoding="utf-8")


def _compact(value: str) -> str:
    return " ".join(value.lower().split())


def test_0068_replaces_only_the_reflection_arm() -> None:
    sql = _compact(UP)
    assert "alter view mediator.v_searchable_content rename to v_searchable_content_pre_0068" in sql
    assert "from mediator.v_searchable_content_pre_0068 previous" in sql
    assert "where previous.source_type <> 'reflection'" in sql


def test_0068_selects_append_only_leaf_revision() -> None:
    sql = _compact(UP)
    assert "where successor.supersedes_entry_id = re.id" in sql
    assert "where re.supersedes_entry_id is null" not in sql
    assert "rs.status = 'processed'" in sql
    assert "re.plaintext_searchable is not null" in sql


def test_0068_rollback_restores_0067_view_object() -> None:
    sql = _compact(DOWN)
    assert "drop view mediator.v_searchable_content" in sql
    assert "alter view mediator.v_searchable_content_pre_0068 rename to v_searchable_content" in sql
