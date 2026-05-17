#!/usr/bin/env bash
#
# scripts/run_sql_tests.sh
#
# SD-008 fallback path for Project B.1.
#
# The preferred entry point is `pytest -m postgres`, which uses the
# tests/fixtures/postgres.py fixture to provision (via Docker) or attach
# to (via TEST_DATABASE_URL) a real Postgres instance and apply every
# forward migration in migrations/*.sql.
#
# Use THIS script when:
#   - You don't have Docker locally and can't easily install testcontainers.
#   - You want to point pytest at an existing dev / scratch database
#     (e.g. the running `veas_live_pg` container, or a Supabase shadow DB).
#
# Usage:
#   DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/scratch \
#       scripts/run_sql_tests.sh
#
# The script:
#   1. Ensures Supabase-style roles (anon / authenticated / service_role) exist.
#   2. Re-creates a fresh database (default name: veas_sql_tests).
#   3. Applies all forward migrations in migrations/*.sql via psql.
#   4. Runs `pytest -m postgres` against the fresh DB.
#
# It DOES NOT spin up a container.  That is the fixture's job.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

ADMIN_URL="${DATABASE_URL:-postgresql://postgres:postgres@127.0.0.1:5432/postgres}"
TEST_DB_NAME="${VEAS_SQL_TEST_DB:-veas_sql_tests}"

echo "[run_sql_tests] admin DSN: $ADMIN_URL"
echo "[run_sql_tests] test DB:   $TEST_DB_NAME"

# 1. Roles.
for role in anon authenticated service_role; do
    psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c \
        "DO \$\$ BEGIN CREATE ROLE $role; EXCEPTION WHEN duplicate_object THEN NULL; END \$\$;" \
        >/dev/null
done

# 2. Fresh database.
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c \
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$TEST_DB_NAME' AND pid <> pg_backend_pid();" \
    >/dev/null || true
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$TEST_DB_NAME\";"
psql "$ADMIN_URL" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$TEST_DB_NAME\";"

# Rewrite the path component of the DSN to point at the fresh database.
TEST_DB_URL="$(python3 -c "
import sys
from urllib.parse import urlsplit, urlunsplit

src = sys.argv[1]
name = sys.argv[2]
parts = urlsplit(src)
new = parts._replace(path='/' + name)
print(urlunsplit(new))
" "$ADMIN_URL" "$TEST_DB_NAME")"

echo "[run_sql_tests] test DSN:  $TEST_DB_URL"

# 3. Schema + migrations.
psql "$TEST_DB_URL" -v ON_ERROR_STOP=1 -c \
    "CREATE SCHEMA IF NOT EXISTS mediator; ALTER DATABASE \"$TEST_DB_NAME\" SET search_path TO mediator, public;"

for f in migrations/*.sql; do
    bn="$(basename "$f")"
    case "$bn" in
        teardown.sql) continue ;;
        *.down.sql)   continue ;;
    esac

    # Mirror the fixture's seed: 0025 backfill needs at least one dyad.
    if [ "$bn" = "0025_backfill_legacy_scope_columns.sql" ]; then
        psql "$TEST_DB_URL" -v ON_ERROR_STOP=1 -c "
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
        " >/dev/null
    fi

    psql "$TEST_DB_URL" -v ON_ERROR_STOP=1 -f "$f" >/dev/null
done

echo "[run_sql_tests] migrations applied; running pytest -m postgres"

# 4. Pytest.  Hand the test DSN to the fixture via TEST_DATABASE_URL so it
# uses our hand-built DB instead of spinning up its own.  The fixture will
# carve out its own per-session DB on top of this one, which is fine — this
# script's purpose is to prove SQL tests run against a real DB end-to-end.
TEST_DATABASE_URL="$ADMIN_URL" exec pytest -m postgres "$@"
