from scripts.validate_reflections_m4_migrations import (
    build_child_env,
    resolve_database_url,
    run_static_checks,
)


def test_prefers_test_database_url() -> None:
    key, value = resolve_database_url(
        {
            "DATABASE_URL": "postgresql://db.example.com/dev",
            "EVAL_DATABASE_URL": "postgresql://db.example.com/eval",
            "TEST_DATABASE_URL": "postgresql://db.example.com/test",
        }
    )

    assert key == "TEST_DATABASE_URL"
    assert value == "postgresql://db.example.com/test"


def test_build_child_env_sets_all_db_vars() -> None:
    env = build_child_env(
        {"PATH": "/tmp/bin", "DATABASE_URL": "postgresql://stale/dev"},
        database_url="postgresql://db.example.com/scratch",
        static_only=False,
    )

    assert env["PATH"] == "/tmp/bin"
    assert env["TEST_DATABASE_URL"] == "postgresql://db.example.com/scratch"
    assert env["EVAL_DATABASE_URL"] == "postgresql://db.example.com/scratch"
    assert env["DATABASE_URL"] == "postgresql://db.example.com/scratch"


def test_static_only_clears_db_env() -> None:
    env = build_child_env(
        {
            "PATH": "/tmp/bin",
            "TEST_DATABASE_URL": "postgresql://db.example.com/test",
            "EVAL_DATABASE_URL": "postgresql://db.example.com/eval",
            "DATABASE_URL": "postgresql://db.example.com/dev",
        },
        database_url="postgresql://db.example.com/scratch",
        static_only=True,
    )

    assert env == {"PATH": "/tmp/bin"}


def test_static_checks_cover_m4_contract() -> None:
    lines = run_static_checks()

    assert len(lines) == 5
    assert any("failure_class CHECK constraint" in line for line in lines)
    assert any("encrypted fields" in line for line in lines)
    assert any("plaintext-only" in line for line in lines)
    assert any("down migration deletes reflection embed rows" in line for line in lines)
    assert any("append-only reflection leaf" in line for line in lines)
