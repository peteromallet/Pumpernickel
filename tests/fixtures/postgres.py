"""Real-Postgres pytest fixture for SQL-heavy integration tests (Project B.1).

Goal
----
Give a test a fresh Postgres instance with the full production migration set
applied, fast enough to run on every PR.  Tests opt in via:

    pytestmark = pytest.mark.postgres        # at module level, or
    @pytest.mark.postgres                    # at function level

and ask for the ``pg_pool`` fixture (an ``asyncpg`` pool) or ``pg_dsn`` (the
raw connection string).

Backends (auto-selected, in order)
----------------------------------
1. ``TEST_DATABASE_URL`` env var set        → use that DB directly (CI path).
2. Docker available on host                 → spin up an ephemeral
   ``postgres:16`` container per test session.
3. Neither                                  → skip the test.

In CI the GitHub Actions ``services:`` Postgres container provides
``TEST_DATABASE_URL``; locally on macOS we run the container directly through
the Docker CLI (no extra Python dependency).

Schema model
------------
Production uses a Postgres schema named ``mediator`` and sets
``search_path TO mediator, public`` on each connection.  The migrations
reference ``mediator.<table>`` directly, so the fixture creates that schema
and pins ``search_path`` on the test database before applying migrations.

Seeding between migrations
--------------------------
Migration ``0025_backfill_legacy_scope_columns.sql`` reads
``mediator.dyads LIMIT 1`` with ``SELECT INTO STRICT``.  On a fresh DB no
dyad exists, so we seed two placeholder users + one dyad immediately before
migration 0025 runs.  This mirrors what production was bootstrapped with
before any backfill migration was authored.

Per SD-008 this is timeboxed: if Docker is unavailable AND
``TEST_DATABASE_URL`` is not set, fixture-using tests are skipped rather
than failing, and the fallback path documented in ``tests/README.md``
(``scripts/run_sql_tests.sh``) is available.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
POSTGRES_IMAGE = "postgres:16"  # Match Railway production major version.

# Seed SQL injected between migration 0024 and 0025 so 0025's STRICT lookups
# against mediator.dyads succeed on a fresh database.
_SEED_BEFORE_0025 = """
INSERT INTO mediator.users (name, phone, timezone)
    VALUES ('Test A', '+15555550100', 'UTC')
    ON CONFLICT (phone) DO NOTHING;
INSERT INTO mediator.users (name, phone, timezone)
    VALUES ('Test B', '+15555550101', 'UTC')
    ON CONFLICT (phone) DO NOTHING;
INSERT INTO mediator.dyads DEFAULT VALUES
    ON CONFLICT DO NOTHING;
INSERT INTO mediator.bot_bindings (bot_id, dyad_id)
    SELECT 'mediator', id FROM mediator.dyads LIMIT 1
    ON CONFLICT DO NOTHING;
"""


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _wait_for_pg_ready(container_name: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_err: str = ""
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["docker", "exec", container_name, "pg_isready", "-U", "postgres"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                return
            last_err = result.stdout + result.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_err = repr(exc)
        time.sleep(0.5)
    raise RuntimeError(f"Postgres container {container_name} not ready: {last_err}")


def _migration_files() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    out: list[Path] = []
    for path in files:
        name = path.name
        if name == "teardown.sql":
            continue
        if name.endswith(".down.sql"):
            continue
        out.append(path)
    return out


async def _apply_migrations(dsn: str, db_name: str) -> int:
    """Apply all forward migrations against ``dsn``. Returns count applied."""
    import asyncpg  # local import to keep import-time cost off non-PG tests

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute("CREATE SCHEMA IF NOT EXISTS mediator;")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS auth;")
        await conn.execute(
            """
            CREATE OR REPLACE FUNCTION auth.uid()
            RETURNS uuid
            LANGUAGE sql
            STABLE
            AS $$ SELECT NULL::uuid $$;
            """
        )
        # Pin search_path for THIS connection while applying migrations.  We
        # also set it on the database so subsequent connections inherit it.
        await conn.execute(
            f'ALTER DATABASE "{db_name}" SET search_path TO mediator, public;'
        )
        await conn.execute("SET search_path TO mediator, public;")

        applied = 0
        for path in _migration_files():
            if path.name == "0025_backfill_legacy_scope_columns.sql":
                # Seed users + dyad so 0025's STRICT lookups succeed.
                await conn.execute(_SEED_BEFORE_0025)
            await conn.execute(path.read_text())
            applied += 1
        return applied
    finally:
        await conn.close()


def _create_db(admin_dsn: str, db_name: str) -> None:
    """Create a fresh database using a synchronous psycopg-less path via asyncpg."""
    import asyncpg

    async def _do() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            # asyncpg can't run CREATE DATABASE inside its implicit txn unless
            # we drop it manually; the simple form below works because asyncpg
            # uses unprepared simple-query mode for .execute() with no params.
            await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            await conn.execute(f'CREATE DATABASE "{db_name}";')
        finally:
            await conn.close()

    asyncio.run(_do())


def _ensure_roles(admin_dsn: str) -> None:
    """Create Supabase-style roles required by migration 0007/0011."""
    import asyncpg

    async def _do() -> None:
        conn = await asyncpg.connect(admin_dsn)
        try:
            for role in ("anon", "authenticated", "service_role"):
                await conn.execute(
                    f"DO $$ BEGIN CREATE ROLE {role}; "
                    f"EXCEPTION WHEN duplicate_object THEN NULL; END $$;"
                )
        finally:
            await conn.close()

    asyncio.run(_do())


# ---------------------------------------------------------------------------
# Container lifecycle (session-scoped)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def _pg_container() -> Iterator[str]:
    """Yield an admin DSN to a session-scoped Postgres instance.

    Resolution order:
      1. ``TEST_DATABASE_URL`` env var → use that DSN (CI / shared dev DB).
      2. Docker available → spin up a fresh ``postgres:16`` container.
      3. Otherwise pytest.skip()s all dependents.
    """
    existing = os.environ.get("TEST_DATABASE_URL")
    if existing:
        yield existing
        return

    if not _docker_available():
        pytest.skip(
            "TEST_DATABASE_URL not set and Docker is unavailable; "
            "real-Postgres tests require one of the two. "
            "See tests/README.md for the SD-008 fallback."
        )

    container_name = f"veas_test_pg_{uuid.uuid4().hex[:8]}"
    port = _pick_free_port()
    cmd = [
        "docker", "run", "-d", "--rm",
        "--name", container_name,
        "-e", "POSTGRES_USER=postgres",
        "-e", "POSTGRES_PASSWORD=postgres",
        "-e", "POSTGRES_DB=postgres",
        "-p", f"{port}:5432",
        POSTGRES_IMAGE,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        pytest.skip(
            f"Failed to start Postgres container ({POSTGRES_IMAGE}): "
            f"{exc.stderr or exc.stdout}"
        )

    try:
        _wait_for_pg_ready(container_name)
        dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
        yield dsn
    finally:
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


# ---------------------------------------------------------------------------
# Per-session migrated database (built once, reused by all postgres tests).
#
# This is the fast path: spinning up a container + applying all migrations
# can take a few seconds, so we do it once per session.  Tests that need
# isolation can truncate/clean tables themselves; B.2 will add finer-grained
# scenario fixtures on top of this.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_dsn(_pg_container: str) -> Iterator[str]:
    """A DSN to a fresh database with all migrations applied."""
    admin_dsn = _pg_container

    _ensure_roles(admin_dsn)

    # Carve out a uniquely-named database for this test session.
    db_name = f"veas_test_{uuid.uuid4().hex[:8]}"
    _create_db(admin_dsn, db_name)

    # Build the DSN to the new database.
    if "?" in admin_dsn:
        base, _, qs = admin_dsn.partition("?")
        # Replace the last path segment.
        head, _, _ = base.rpartition("/")
        test_dsn = f"{head}/{db_name}?{qs}"
    else:
        head, _, _ = admin_dsn.rpartition("/")
        test_dsn = f"{head}/{db_name}"

    applied = asyncio.run(_apply_migrations(test_dsn, db_name))
    assert applied > 0, "No migrations were applied — check migrations/*.sql"

    try:
        yield test_dsn
    finally:
        # Drop the database so the container can be reused cleanly if shared.
        async def _drop() -> None:
            import asyncpg

            conn = await asyncpg.connect(admin_dsn)
            try:
                # Terminate any lingering connections first.
                await conn.execute(
                    "SELECT pg_terminate_backend(pid) "
                    "FROM pg_stat_activity WHERE datname = $1 AND pid <> pg_backend_pid();",
                    db_name,
                )
                await conn.execute(f'DROP DATABASE IF EXISTS "{db_name}";')
            finally:
                await conn.close()

        try:
            asyncio.run(_drop())
        except Exception:
            # Best-effort cleanup; container teardown will reclaim the storage.
            pass


async def _pg_init_connection(connection: Any) -> None:
    """Register the same jsonb/json codec that production uses (app/db.py).

    Without this, asyncpg returns ``jsonb`` columns as raw text and tests
    that read ``tool_calls``/``audit_events`` off the ``v_bot_actions`` view
    would get strings instead of Python lists.
    """
    for type_name in ("jsonb", "json"):
        await connection.set_type_codec(
            type_name,
            schema="pg_catalog",
            encoder=json.dumps,
            decoder=json.loads,
            format="text",
        )


@pytest.fixture
async def pg_pool(pg_dsn: str) -> AsyncIterator[Any]:
    """Yield an asyncpg pool against the migrated test database.

    Each pool is function-scoped so concurrent tests don't trip over a shared
    connection; the underlying DB is session-scoped so we don't pay migration
    cost per test.
    """
    import asyncpg

    pool = await asyncpg.create_pool(
        pg_dsn,
        min_size=1,
        max_size=4,
        statement_cache_size=0,
        server_settings={"search_path": "mediator,public"},
        init=_pg_init_connection,
    )
    try:
        yield pool
    finally:
        await pool.close()
