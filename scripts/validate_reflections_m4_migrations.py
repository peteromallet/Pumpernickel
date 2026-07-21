#!/usr/bin/env python3
"""Validate the reflections M4 migration surface.

This script reuses the repository's existing migration pytest suites instead of
introducing a parallel SQL harness. It adds a small amount of static coverage
for the M4-specific checks that are easy to regress in reviews:

* reflection failure-class CHECK constraint values
* encrypted/derived searchable field boundaries
* 0064 down-migration cleanup before legacy CHECK restoration

Live scratch-Postgres apply/rollback validation runs automatically when a safe
database URL is available via ``--database-url`` or one of the usual
``TEST_DATABASE_URL`` / ``EVAL_DATABASE_URL`` / ``DATABASE_URL`` environment
variables. Otherwise the script runs the static checks only and makes the
missing live prerequisite explicit.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping

from evals.db import ensure_safe_database_url


REPO_ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = REPO_ROOT / "migrations"
PYTEST_TARGETS = (
    "tests/test_reflection_foundation_migration.py",
    "tests/test_migration_0064_reflections_searchable.py",
    "tests/test_migration_0065_reflection_revision_leaf.py",
)
DB_ENV_KEYS = ("TEST_DATABASE_URL", "EVAL_DATABASE_URL", "DATABASE_URL")
EXPECTED_REFLECTION_FAILURE_CLASSES = (
    "retryable_processor",
    "terminal_input",
    "terminal_internal",
    "stale_claim",
)


def _compact(sql: str) -> str:
    return " ".join(sql.lower().split())


def _load_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def resolve_database_url(env: Mapping[str, str]) -> tuple[str | None, str | None]:
    for key in DB_ENV_KEYS:
        value = env.get(key, "").strip()
        if value:
            return key, value
    return None, None


def build_child_env(
    env: Mapping[str, str],
    *,
    database_url: str | None,
    static_only: bool,
) -> dict[str, str]:
    child_env = dict(env)
    for key in DB_ENV_KEYS:
        child_env.pop(key, None)

    if static_only or not database_url:
        return child_env

    for key in DB_ENV_KEYS:
        child_env[key] = database_url
    return child_env


def run_static_checks(repo_root: Path = REPO_ROOT) -> list[str]:
    up_0063 = _compact(_load_sql(repo_root / "migrations/0063_reflection_foundation.sql"))
    down_0064 = _compact(_load_sql(repo_root / "migrations/0064_reflections_searchable_content.down.sql"))
    up_0064 = _compact(_load_sql(repo_root / "migrations/0064_reflections_searchable_content.sql"))
    up_0065 = _compact(_load_sql(repo_root / "migrations/0065_reflection_revision_leaf_semantics.sql"))

    expected_failure_check = (
        "check (failure_class is null or failure_class in ("
        + ", ".join(f"'{value}'" for value in EXPECTED_REFLECTION_FAILURE_CLASSES)
        + "))"
    )
    if expected_failure_check not in up_0063:
        raise AssertionError("0063 failure_class CHECK constraint drifted")

    for token in (
        "payload_encrypted bytea",
        "summary_encrypted bytea",
        "candidate_payload_encrypted bytea",
        "plaintext_searchable text",
        "create index idx_reflection_sessions_failed_retry",
        "create index idx_reflection_entries_current",
    ):
        if token not in up_0063:
            raise AssertionError(f"0063 missing required token: {token}")

    reflection_arm_start = up_0064.index("'reflection'::text as source_type")
    reflection_arm_end = up_0064.index(";", reflection_arm_start)
    reflection_arm = up_0064[reflection_arm_start:reflection_arm_end]
    if "payload_encrypted" in reflection_arm or "summary_encrypted" in reflection_arm:
        raise AssertionError("0064 reflection search arm leaked encrypted columns")
    for token in (
        "re.plaintext_searchable as canonical_text",
        "re.plaintext_searchable is not null",
        "btrim(re.plaintext_searchable) <> ''",
    ):
        if token not in reflection_arm:
            raise AssertionError(f"0064 reflection search arm missing token: {token}")

    delete_embed = "delete from mediator.embed_jobs where source_type = 'reflection'"
    delete_content = "delete from mediator.content_embeddings where source_type = 'reflection'"
    tighten_content = "add constraint content_embeddings_source_type_check"
    tighten_jobs = "add constraint embed_jobs_source_type_check"
    for token in (delete_embed, delete_content, tighten_content, tighten_jobs):
        if token not in down_0064:
            raise AssertionError(f"0064 down migration missing token: {token}")
    if down_0064.index(delete_embed) > down_0064.index(tighten_jobs):
        raise AssertionError("0064 down migration tightens embed_jobs before cleanup")
    if down_0064.index(delete_content) > down_0064.index(tighten_content):
        raise AssertionError("0064 down migration tightens content_embeddings before cleanup")

    for token in (
        "where previous.source_type <> 'reflection'",
        "where successor.supersedes_entry_id = re.id",
        "re.plaintext_searchable is not null",
    ):
        if token not in up_0065:
            raise AssertionError(f"0065 leaf-semantics migration missing token: {token}")
    if "where re.supersedes_entry_id is null" in up_0065:
        raise AssertionError("0065 still selects the original reflection revision")

    return [
        "0063 failure_class CHECK constraint matches the approved reflection taxonomy.",
        "0063 still defines encrypted fields and the retry/current indexes used by cleanup and recovery paths.",
        "0064 searchable-content reflection arm stays plaintext-only.",
        "0064 down migration deletes reflection embed rows before restoring legacy CHECK constraints.",
        "0065 selects the append-only reflection leaf for search and embedding.",
    ]


def run_pytest_suite(child_env: Mapping[str, str]) -> int:
    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *PYTEST_TARGETS,
        "-v",
        "--tb=short",
    ]
    completed = subprocess.run(cmd, cwd=REPO_ROOT, env=dict(child_env))
    return completed.returncode


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        help="Scratch/admin Postgres DSN for live migration apply/rollback validation.",
    )
    parser.add_argument(
        "--static-only",
        action="store_true",
        help="Run the SQL text checks and pytest static checks only.",
    )
    parser.add_argument(
        "--require-live",
        action="store_true",
        help="Fail instead of falling back to static-only validation when no DB URL is available.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    for line in run_static_checks():
        print(f"[validate_reflections_m4_migrations] {line}")

    source_key = None
    database_url = args.database_url
    if not database_url:
        source_key, database_url = resolve_database_url(os.environ)

    if database_url and not args.static_only:
        ensure_safe_database_url(database_url)
        print(
            "[validate_reflections_m4_migrations] "
            f"live scratch validation enabled via {source_key or '--database-url'}"
        )
    elif args.require_live and not args.static_only:
        print(
            "[validate_reflections_m4_migrations] no safe TEST_DATABASE_URL, "
            "EVAL_DATABASE_URL, DATABASE_URL, or --database-url was provided",
            file=sys.stderr,
        )
        return 2
    else:
        print(
            "[validate_reflections_m4_migrations] no scratch Postgres DSN detected; "
            "running static validation only"
        )
        database_url = None

    child_env = build_child_env(
        os.environ,
        database_url=database_url,
        static_only=args.static_only,
    )
    return run_pytest_suite(child_env)


if __name__ == "__main__":
    raise SystemExit(main())
