# Veas test suite

Two flavours of tests live here:

1. **Default (unit) tests** — run against the in-process `FakePool` defined
   in `conftest.py`. Fast, hermetic, no external services. This is what
   `pytest` runs by default and what most existing tests use.
2. **Postgres-backed tests** — opt in with the `postgres` marker. These
   require a real Postgres 16 instance (matching Railway production) with
   every forward migration applied. Project B.1 work item.

## Running the default suite

```bash
pytest                       # everything except `postgres`-marked tests
pytest -m "not postgres"     # same thing, explicit
```

## Running the Postgres-backed tests

The fixture (`tests/fixtures/postgres.py`) auto-selects a backend in this
order:

1. If `TEST_DATABASE_URL` is set, the fixture connects there as the admin
   user, creates a per-session test database, applies migrations, and yields
   an `asyncpg` pool. **This is the CI path.**
2. Otherwise, if Docker is available on the host, the fixture spins up an
   ephemeral `postgres:16` container, applies migrations, and tears it down
   on session exit. **This is the macOS dev path.**
3. Otherwise, all `postgres`-marked tests are skipped (not failed).

### macOS / local dev

Docker Desktop must be running. Then:

```bash
pytest -m postgres -v
```

First run takes ~15–30 seconds (container start + 42 migrations). Subsequent
runs in the same session reuse the database.

### Pointing at an existing dev DB

If you already have a Postgres instance running (for example the
`veas_live_pg` container on port 54322), set `TEST_DATABASE_URL` to its
admin DSN:

```bash
TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres \
    pytest -m postgres -v
```

The fixture will create a uniquely-named throwaway database on that
server, run migrations into it, and drop it afterwards.

### SD-008 fallback: `scripts/run_sql_tests.sh`

If for whatever reason neither Docker nor the testcontainers-style fixture
works on a particular machine — the documented fallback per SD-008 — use
the helper script:

```bash
DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
    scripts/run_sql_tests.sh
```

It creates a fresh database, applies migrations via raw `psql` (the same
pattern the existing `.github/workflows/evals.yml` job uses), then runs
`pytest -m postgres` against it. Same CI workflow, same SQL surface
area — just no Python orchestration of the container.

## CI

`.github/workflows/postgres-tests.yml` runs `pytest -m postgres` against
a GitHub Actions Postgres 16 service container on every PR that touches
`app/`, `migrations/`, `tests/`, or the workflow itself.

## Schema notes

Production uses a Postgres schema named `mediator` and sets
`search_path TO mediator, public` on every connection.  The fixture
mirrors that: tables created by `CREATE TABLE IF NOT EXISTS <name>`
inside the migrations end up in the `mediator` schema, and later
migrations that reference `mediator.<table>` directly continue to work.

Migration `0025_backfill_legacy_scope_columns.sql` does a
`SELECT INTO STRICT ... FROM mediator.dyads LIMIT 1` and would fail on a
truly empty database.  The fixture inserts two placeholder users + one
dyad + a `mediator` bot binding immediately before migration 0025 runs.
This matches what production was bootstrapped with before any backfill
migration was authored.

## Markers

| Marker     | Meaning                                          |
| ---------- | ------------------------------------------------ |
| `postgres` | Test needs the real-Postgres fixture (B.1).      |
| `requires_postgres` | Older marker used by `migrations/validation/`; not the same as `postgres`. |

The `postgres` marker is registered in `tests/conftest.py::pytest_configure`,
so `pytest --strict-markers` is happy.

## Adding more scenario fixtures

This is Project B.1 — the bare provisioning fixture. Higher-level scenario
fixtures (replied / silent / failed turns; full `bot_actions` audit harness)
land in B.2 on top of this.
