from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
UP = (ROOT / "migrations/0069_reflection_opened_at_compat.sql").read_text(encoding="utf-8")
DOWN = (ROOT / "migrations/0069_reflection_opened_at_compat.down.sql").read_text(
    encoding="utf-8"
)


def _compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def test_0069_backfills_early_reflection_sessions_safely() -> None:
    sql = _compact(UP)
    assert "add column if not exists opened_at timestamptz" in sql
    assert "set opened_at = created_at" in sql
    assert "where opened_at is null" in sql
    assert "alter column opened_at set default now()" in sql
    assert "alter column opened_at set not null" in sql


def test_0069_is_transactional_and_rollback_preserves_canonical_column() -> None:
    assert "begin;" in _compact(UP)
    assert "commit;" in _compact(UP)
    down = _compact(DOWN)
    assert "begin;" in down
    assert "commit;" in down
    assert "drop column" not in down
