"""Tests for connection-scoped local delete primitives in the health repo.

These exercises cover the destructive lifecycle path added in the product
hardening work: deleting every local Withings artefact for a user's
connection while keeping manual testimony and other users' rows intact, and
keeping the multi-statement cleanup transactional.

A self-contained in-memory pool (``_HealthDeletePool``) is used so the tests
do not depend on the heavyweight shared conftest FakePool fixture.  The pool
implements ``acquire`` / connection ``transaction`` with snapshot/rollback
semantics so transactional safety can be asserted directly.
"""

from __future__ import annotations

import copy
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.services.health_sync.repository import HealthSyncRepository


# ---------------------------------------------------------------------------
# In-memory fake pool with transactional snapshot/rollback
# ---------------------------------------------------------------------------


class _FakeAcquire:
    def __init__(self, pool: "_HealthDeletePool") -> None:
        self.pool = pool

    async def __aenter__(self) -> "_FakeConn":
        return _FakeConn(self.pool)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _FakeConn:
    def __init__(self, pool: "_HealthDeletePool") -> None:
        self.pool = pool

    def transaction(self) -> "_FakeTx":
        return _FakeTx(self.pool)

    async def fetchrow(self, sql: str, *args):
        return await self.pool.fetchrow(sql, *args)

    async def fetch(self, sql: str, *args):
        return await self.pool.fetch(sql, *args)

    async def execute(self, sql: str, *args):
        return await self.pool.execute(sql, *args)


class _FakeTx:
    def __init__(self, pool: "_HealthDeletePool") -> None:
        self.pool = pool
        self._snapshot = None

    async def __aenter__(self) -> None:
        self._snapshot = self.pool._snapshot_tables()
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if exc_type is not None:
            # Roll back on any exception so a mid-transaction failure leaves
            # all tables exactly as they were on entry.
            self.pool._restore_tables(self._snapshot)
        return False


def _compact(sql: str) -> str:
    return " ".join(sql.split())


class _HealthDeletePool:
    """Self-contained fake pool for the health delete primitive tests.

    Tracks every health table touched by the connection-scoped delete path
    and supports transactional snapshot/rollback.  Every delete statement is
    dispatched by table name and is double-scoped by ``connection_id`` and
    ``user_id`` to mirror the production SQL.
    """

    def __init__(self) -> None:
        self.connections: dict[UUID, dict] = {}
        self.source_records: dict[UUID, dict] = {}
        self.normalized_measurements: dict[UUID, dict] = {}
        self.normalized_sleep: dict[UUID, dict] = {}
        self.normalized_workouts: dict[UUID, dict] = {}
        self.dirty_categories: dict[UUID, dict] = {}
        self.webhook_receipts: dict[UUID, dict] = {}
        self.projections: dict[UUID, dict] = {}
        self.events: dict[UUID, dict] = {}
        # Compact-SQL strings whose execution should raise, for rollback tests.
        self.fail_on: set[str] = set()
        # Connection that ``mark_connection_deleted`` should report as missing
        # (returns no row) to simulate the "not found / not owned" case.
        self.missing_connection_id: UUID | None = None

    # -- helpers -----------------------------------------------------------

    def _snapshot_tables(self) -> dict:
        return {
            "connections": copy.deepcopy(self.connections),
            "source_records": copy.deepcopy(self.source_records),
            "normalized_measurements": copy.deepcopy(self.normalized_measurements),
            "normalized_sleep": copy.deepcopy(self.normalized_sleep),
            "normalized_workouts": copy.deepcopy(self.normalized_workouts),
            "dirty_categories": copy.deepcopy(self.dirty_categories),
            "webhook_receipts": copy.deepcopy(self.webhook_receipts),
            "projections": copy.deepcopy(self.projections),
            "events": copy.deepcopy(self.events),
        }

    def _restore_tables(self, snap: dict) -> None:
        self.connections = snap["connections"]
        self.source_records = snap["source_records"]
        self.normalized_measurements = snap["normalized_measurements"]
        self.normalized_sleep = snap["normalized_sleep"]
        self.normalized_workouts = snap["normalized_workouts"]
        self.dirty_categories = snap["dirty_categories"]
        self.webhook_receipts = snap["webhook_receipts"]
        self.projections = snap["projections"]
        self.events = snap["events"]

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self)

    # -- async db surface --------------------------------------------------

    async def fetchrow(self, sql: str, *args):
        compact = _compact(sql)
        if compact.startswith(
            "UPDATE mediator.health_connections SET status = 'deleted'"
        ):
            return self._mark_connection_deleted(compact, args)
        raise AssertionError(f"unhandled fetchrow SQL: {compact!r}")

    async def fetch(self, sql: str, *args):
        raise AssertionError(f"unexpected fetch in delete path: {_compact(sql)!r}")

    async def execute(self, sql: str, *args):
        compact = _compact(sql)
        if compact in self.fail_on:
            raise RuntimeError(f"injected failure for: {compact!r}")
        if compact.startswith(
            "DELETE FROM mediator.health_connections"
        ):
            # Not used by the delete path, but guard anyway.
            return
        if compact.startswith(
            "DELETE FROM mediator.events WHERE user_id = $2 AND id IN"
        ):
            return self._delete_projection_owned_events(compact, args)
        if compact.startswith(
            "DELETE FROM mediator.health_source_to_event_projections"
        ):
            return self._delete_rows(self.projections, args)
        if compact.startswith("DELETE FROM mediator.health_source_records"):
            return self._delete_rows(self.source_records, args)
        if compact.startswith("DELETE FROM mediator.health_normalized_measurements"):
            return self._delete_rows(self.normalized_measurements, args)
        if compact.startswith("DELETE FROM mediator.health_normalized_sleep"):
            return self._delete_rows(self.normalized_sleep, args)
        if compact.startswith("DELETE FROM mediator.health_normalized_workouts"):
            return self._delete_rows(self.normalized_workouts, args)
        if compact.startswith("DELETE FROM mediator.health_dirty_categories"):
            return self._delete_rows(self.dirty_categories, args)
        if compact.startswith("DELETE FROM mediator.health_webhook_receipts"):
            return self._delete_rows(self.webhook_receipts, args)
        raise AssertionError(f"unhandled execute SQL: {compact!r}")

    # -- per-statement handlers -------------------------------------------

    def _delete_rows(self, table: dict, args: tuple) -> None:
        connection_id, user_id = args[0], args[1]
        for key in list(table.keys()):
            row = table[key]
            if (
                row.get("connection_id") == connection_id
                and row.get("user_id") == user_id
            ):
                del table[key]

    def _delete_projection_owned_events(self, compact: str, args: tuple) -> None:
        connection_id, user_id = args[0], args[1]
        # Resolve event_ids from the ledger for this connection+user.
        target_event_ids = {
            proj["event_id"]
            for proj in self.projections.values()
            if (
                proj.get("connection_id") == connection_id
                and proj.get("user_id") == user_id
                and proj.get("event_id") is not None
            )
        }
        for event_id in list(target_event_ids):
            ev = self.events.get(event_id)
            if ev is not None and ev.get("user_id") == user_id:
                del self.events[event_id]

    def _mark_connection_deleted(self, compact: str, args: tuple) -> dict | None:
        connection_id, user_id, now = args[0], args[1], args[2]
        conn = self.connections.get(connection_id)
        if (
            conn is None
            or conn.get("user_id") != user_id
            or conn.get("deleted_at") is not None
            or self.missing_connection_id == connection_id
        ):
            return None
        conn["status"] = "deleted"
        conn["access_token_encrypted"] = None
        conn["refresh_token_encrypted"] = None
        conn["access_token_expires_at"] = None
        conn["refresh_token_expires_at"] = None
        conn["refresh_token_rotated_at"] = None
        conn["deleted_at"] = conn.get("deleted_at") or now
        conn["revoked_at"] = conn.get("revoked_at") or now
        conn["updated_at"] = now
        return dict(conn)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def pool() -> _HealthDeletePool:
    return _HealthDeletePool()


@pytest.fixture
def repo(pool: _HealthDeletePool) -> HealthSyncRepository:
    return HealthSyncRepository(pool=pool)


@pytest.fixture
def populated_world(pool: _HealthDeletePool) -> dict:
    """Seed two users, one of which owns two Withings connections.

    The target connection (``conn_a``) is fully populated across every
    table; a sibling connection (``conn_b``) for the same user and a
    completely separate user (``user2``) are seeded so cross-connection and
    cross-user isolation can be asserted.
    """
    user1 = uuid4()
    user2 = uuid4()
    conn_a = uuid4()  # target of deletion
    conn_b = uuid4()  # same user, different connection - must survive

    pool.connections[conn_a] = {
        "id": conn_a,
        "user_id": user1,
        "provider": "withings",
        "external_user_id": "ext-a",
        "status": "active",
        "granted_scopes": ["weight", "sleep"],
        "cursor_state": {"offset": 5},
        "access_token_encrypted": "enc-access-a",
        "refresh_token_encrypted": "enc-refresh-a",
        "access_token_expires_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
        "refresh_token_expires_at": datetime(2031, 1, 1, tzinfo=timezone.utc),
        "refresh_token_rotated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "deleted_at": None,
        "revoked_at": None,
    }
    pool.connections[conn_b] = {
        "id": conn_b,
        "user_id": user1,
        "provider": "withings",
        "external_user_id": "ext-b",
        "status": "active",
        "granted_scopes": ["workout"],
        "cursor_state": {},
        "access_token_encrypted": "enc-access-b",
        "refresh_token_encrypted": "enc-refresh-b",
        "access_token_expires_at": None,
        "refresh_token_expires_at": None,
        "refresh_token_rotated_at": None,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "deleted_at": None,
        "revoked_at": None,
    }
    # user2 connection sharing external id shape, but different user.
    conn_c = uuid4()
    pool.connections[conn_c] = {
        "id": conn_c,
        "user_id": user2,
        "provider": "withings",
        "external_user_id": "ext-c",
        "status": "active",
        "granted_scopes": ["weight"],
        "cursor_state": {},
        "access_token_encrypted": "enc-access-c",
        "refresh_token_encrypted": "enc-refresh-c",
        "access_token_expires_at": None,
        "refresh_token_expires_at": None,
        "refresh_token_rotated_at": None,
        "updated_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
        "deleted_at": None,
        "revoked_at": None,
    }

    def _add(table: dict, conn_id: UUID, uid: UUID, **extra) -> UUID:
        rid = uuid4()
        table[rid] = {
            "id": rid,
            "connection_id": conn_id,
            "user_id": uid,
            **extra,
        }
        return rid

    # conn_a source records
    _add(pool.source_records, conn_a, user1, payload="raw-a-1")
    _add(pool.source_records, conn_a, user1, payload="raw-a-2")
    # conn_b and conn_c source records must survive
    _add(pool.source_records, conn_b, user1, payload="raw-b-1")
    _add(pool.source_records, conn_c, user2, payload="raw-c-1")

    _add(pool.normalized_measurements, conn_a, user1, value=80.0)
    _add(pool.normalized_measurements, conn_b, user1, value=70.0)
    _add(pool.normalized_measurements, conn_c, user2, value=90.0)

    _add(pool.normalized_sleep, conn_a, user1, duration=300)
    _add(pool.normalized_sleep, conn_c, user2, duration=200)

    _add(pool.normalized_workouts, conn_a, user1, distance=5.0)
    _add(pool.normalized_workouts, conn_b, user1, distance=3.0)

    _add(pool.dirty_categories, conn_a, user1, resource_type="weight")
    _add(pool.dirty_categories, conn_a, user1, resource_type="sleep")
    _add(pool.dirty_categories, conn_b, user1, resource_type="workout")
    _add(pool.dirty_categories, conn_c, user2, resource_type="weight")

    _add(pool.webhook_receipts, conn_a, user1, signature="sig-a")
    _add(pool.webhook_receipts, conn_c, user2, signature="sig-c")

    # Projection ledger rows linking conn_a to projection-owned adherence
    # events.  Each ledger row carries its own user_id.
    proj_event_a1 = uuid4()
    proj_event_a2 = uuid4()
    proj_event_b1 = uuid4()  # owned by conn_b - must survive
    proj_event_c1 = uuid4()  # owned by conn_c (user2) - must survive
    manual_event = uuid4()   # manual log_event testimony - must survive
    manual_event_other = uuid4()  # user2 manual event - must survive

    pool.projections[uuid4()] = {
        "id": uuid4(),
        "connection_id": conn_a,
        "user_id": user1,
        "event_id": proj_event_a1,
    }
    pool.projections[uuid4()] = {
        "id": uuid4(),
        "connection_id": conn_a,
        "user_id": user1,
        "event_id": proj_event_a2,
    }
    pool.projections[uuid4()] = {
        "id": uuid4(),
        "connection_id": conn_b,
        "user_id": user1,
        "event_id": proj_event_b1,
    }
    pool.projections[uuid4()] = {
        "id": uuid4(),
        "connection_id": conn_c,
        "user_id": user2,
        "event_id": proj_event_c1,
    }
    # A projection row with a NULL event_id should be tolerated and deleted.
    pool.projections[uuid4()] = {
        "id": uuid4(),
        "connection_id": conn_a,
        "user_id": user1,
        "event_id": None,
    }

    for event_id, uid in [
        (proj_event_a1, user1),
        (proj_event_a2, user1),
        (proj_event_b1, user1),
        (proj_event_c1, user2),
        (manual_event, user1),
        (manual_event_other, user2),
    ]:
        pool.events[event_id] = {
            "id": event_id,
            "user_id": uid,
            "metric_key": "weight",
            "adherence_status": "committed",
        }

    return {
        "user1": user1,
        "user2": user2,
        "conn_a": conn_a,
        "conn_b": conn_b,
        "conn_c": conn_c,
        "proj_event_a1": proj_event_a1,
        "proj_event_a2": proj_event_a2,
        "proj_event_b1": proj_event_b1,
        "proj_event_c1": proj_event_c1,
        "manual_event": manual_event,
        "manual_event_other": manual_event_other,
    }


# ---------------------------------------------------------------------------
# delete_connection_data - the transactional orchestrator
# ---------------------------------------------------------------------------


class TestDeleteConnectionData:
    @pytest.mark.asyncio
    async def test_clears_all_target_connection_tables(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        conn_a = world["conn_a"]
        user1 = world["user1"]

        await repo.delete_connection_data(
            connection_id=conn_a, user_id=user1, now=datetime(2026, 7, 21, tzinfo=timezone.utc)
        )

        # Source records, normalized rows, dirty categories, webhook receipts
        # and projection ledger rows for conn_a are all gone.
        assert not any(
            r["connection_id"] == conn_a for r in pool.source_records.values()
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.normalized_measurements.values()
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.normalized_sleep.values()
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.normalized_workouts.values()
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.dirty_categories.values()
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.webhook_receipts.values()
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.projections.values()
        )

    @pytest.mark.asyncio
    async def test_marks_connection_deleted_and_clears_tokens(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        now = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
        record = await repo.delete_connection_data(
            connection_id=world["conn_a"], user_id=world["user1"], now=now
        )

        conn = pool.connections[world["conn_a"]]
        assert conn["status"] == "deleted"
        assert conn["deleted_at"] == now
        assert conn["revoked_at"] == now
        assert conn["access_token_encrypted"] is None
        assert conn["refresh_token_encrypted"] is None
        assert conn["access_token_expires_at"] is None
        assert conn["refresh_token_expires_at"] is None
        assert conn["refresh_token_rotated_at"] is None
        # The returned record reflects the new status.
        assert record.status == "deleted"
        assert record.connection_id == world["conn_a"]

    @pytest.mark.asyncio
    async def test_deletes_only_projection_owned_events_for_connection(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        await repo.delete_connection_data(
            connection_id=world["conn_a"], user_id=world["user1"]
        )

        # conn_a's projection-owned events are gone.
        assert world["proj_event_a1"] not in pool.events
        assert world["proj_event_a2"] not in pool.events
        # Manual events (no ledger link) are preserved.
        assert world["manual_event"] in pool.events
        # conn_b's projection event (same user, different connection) survives.
        assert world["proj_event_b1"] in pool.events
        # user2's events survive entirely.
        assert world["proj_event_c1"] in pool.events
        assert world["manual_event_other"] in pool.events

    @pytest.mark.asyncio
    async def test_preserves_other_connections_and_users_rows(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        before = {
            "source_records": len(pool.source_records),
            "normalized_measurements": len(pool.normalized_measurements),
            "normalized_sleep": len(pool.normalized_sleep),
            "normalized_workouts": len(pool.normalized_workouts),
            "dirty_categories": len(pool.dirty_categories),
            "webhook_receipts": len(pool.webhook_receipts),
            "projections": len(pool.projections),
            "events": len(pool.events),
        }
        await repo.delete_connection_data(
            connection_id=world["conn_a"], user_id=world["user1"]
        )

        # conn_b (same user) and conn_c (user2) rows all survive.
        assert pool.connections[world["conn_b"]]["status"] == "active"
        assert pool.connections[world["conn_c"]]["status"] == "active"
        assert any(
            r["connection_id"] == world["conn_b"] for r in pool.source_records.values()
        )
        assert any(
            r["connection_id"] == world["conn_c"] for r in pool.source_records.values()
        )
        assert any(
            r["connection_id"] == world["conn_b"]
            for r in pool.dirty_categories.values()
        )
        assert any(
            r["connection_id"] == world["conn_c"]
            for r in pool.dirty_categories.values()
        )
        assert any(
            r["connection_id"] == world["conn_c"]
            for r in pool.webhook_receipts.values()
        )
        # The expected survivor counts: subtract only conn_a's owned rows.
        assert len(pool.source_records) == before["source_records"] - 2
        assert len(pool.normalized_measurements) == before["normalized_measurements"] - 1
        assert len(pool.normalized_sleep) == before["normalized_sleep"] - 1
        assert len(pool.normalized_workouts) == before["normalized_workouts"] - 1
        assert len(pool.dirty_categories) == before["dirty_categories"] - 2
        assert len(pool.webhook_receipts) == before["webhook_receipts"] - 1
        # 2 conn_a projection-ledger rows + 1 null-event_id row removed.
        assert len(pool.projections) == before["projections"] - 3
        # 2 conn_a projection-owned events removed.
        assert len(pool.events) == before["events"] - 2

    @pytest.mark.asyncio
    async def test_raises_lookup_error_when_connection_not_owned(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        # Caller passes user2 but the connection belongs to user1.
        with pytest.raises(LookupError):
            await repo.delete_connection_data(
                connection_id=world["conn_a"], user_id=world["user2"]
            )

    @pytest.mark.asyncio
    async def test_raises_lookup_error_when_connection_missing(
        self, repo: HealthSyncRepository, populated_world
    ):
        with pytest.raises(LookupError):
            await repo.delete_connection_data(
                connection_id=uuid4(), user_id=populated_world["user1"]
            )

    @pytest.mark.asyncio
    async def test_transactional_rollback_on_mid_failure(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        conn_a = world["conn_a"]
        user1 = world["user1"]

        # Snapshot before any mutation.
        before = pool._snapshot_tables()

        # Inject a failure on the projection-ledger delete (the 2nd statement
        # of the orchestrator).  Everything must roll back untouched.
        from app.services.health_sync import repository as repo_mod

        pool.fail_on.add(_compact(repo_mod._DELETE_CONNECTION_PROJECTION_LEDGER_SQL))

        with pytest.raises(RuntimeError):
            await repo.delete_connection_data(
                connection_id=conn_a, user_id=user1
            )

        after = pool._snapshot_tables()
        # No table changed despite partial execution.
        for table_name in before:
            assert (
                before[table_name] == after[table_name]
            ), f"{table_name} changed despite rollback"

    @pytest.mark.asyncio
    async def test_already_deleted_connection_raises_and_is_idempotent(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        await repo.delete_connection_data(
            connection_id=world["conn_a"], user_id=world["user1"]
        )
        # A second invocation must raise because the connection is now deleted.
        with pytest.raises(LookupError):
            await repo.delete_connection_data(
                connection_id=world["conn_a"], user_id=world["user1"]
            )
        # And no further damage to other rows occurred.
        assert world["manual_event"] in pool.events


# ---------------------------------------------------------------------------
# mark_connection_deleted - the ownership-guard primitive
# ---------------------------------------------------------------------------


class TestMarkConnectionDeleted:
    @pytest.mark.asyncio
    async def test_clears_token_fields_only(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        now = datetime(2026, 7, 21, tzinfo=timezone.utc)
        await repo.mark_connection_deleted(
            connection_id=world["conn_a"], user_id=world["user1"], now=now
        )
        conn = pool.connections[world["conn_a"]]
        assert conn["status"] == "deleted"
        assert conn["deleted_at"] == now
        assert conn["access_token_encrypted"] is None
        assert conn["refresh_token_encrypted"] is None

    @pytest.mark.asyncio
    async def test_preserves_sibling_connection_tokens(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        await repo.mark_connection_deleted(
            connection_id=world["conn_a"], user_id=world["user1"]
        )
        # conn_b still has its encrypted tokens.
        assert pool.connections[world["conn_b"]]["access_token_encrypted"] == "enc-access-b"
        assert pool.connections[world["conn_b"]]["status"] == "active"

    @pytest.mark.asyncio
    async def test_wrong_user_raises(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        with pytest.raises(LookupError):
            await repo.mark_connection_deleted(
                connection_id=world["conn_a"], user_id=world["user2"]
            )
        # Connection row untouched on the failed attempt.
        assert pool.connections[world["conn_a"]]["status"] == "active"

    @pytest.mark.asyncio
    async def test_missing_connection_raises(
        self, repo: HealthSyncRepository
    ):
        with pytest.raises(LookupError):
            await repo.mark_connection_deleted(
                connection_id=uuid4(), user_id=uuid4()
            )

    @pytest.mark.asyncio
    async def test_already_deleted_is_idempotent_at_sql_layer(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        first_now = datetime(2026, 7, 21, tzinfo=timezone.utc)
        await repo.mark_connection_deleted(
            connection_id=world["conn_a"], user_id=world["user1"], now=first_now
        )
        first_deleted_at = pool.connections[world["conn_a"]]["deleted_at"]
        # Second mark should raise (deleted_at IS NOT NULL guard) and NOT clobber.
        with pytest.raises(LookupError):
            await repo.mark_connection_deleted(
                connection_id=world["conn_a"],
                user_id=world["user1"],
                now=datetime(2030, 1, 1, tzinfo=timezone.utc),
            )
        assert pool.connections[world["conn_a"]]["deleted_at"] == first_deleted_at


# ---------------------------------------------------------------------------
# Individual connection-scoped delete primitives
# ---------------------------------------------------------------------------


class TestConnectionScopedPrimitives:
    @pytest.mark.asyncio
    async def test_each_primitive_only_clears_its_table_and_user(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        conn_a, conn_b, user1, user2 = (
            world["conn_a"],
            world["conn_b"],
            world["user1"],
            world["user2"],
        )

        await repo.delete_connection_source_records(
            connection_id=conn_a, user_id=user1
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.source_records.values()
        )
        assert any(
            r["connection_id"] == conn_b for r in pool.source_records.values()
        )

        await repo.delete_connection_normalized_measurements(
            connection_id=conn_a, user_id=user1
        )
        assert not any(
            r["connection_id"] == conn_a
            for r in pool.normalized_measurements.values()
        )

        await repo.delete_connection_normalized_sleep(
            connection_id=conn_a, user_id=user1
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.normalized_sleep.values()
        )

        await repo.delete_connection_normalized_workouts(
            connection_id=conn_a, user_id=user1
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.normalized_workouts.values()
        )
        # conn_b workout survives.
        assert any(
            r["connection_id"] == conn_b for r in pool.normalized_workouts.values()
        )

        await repo.delete_connection_dirty_categories(
            connection_id=conn_a, user_id=user1
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.dirty_categories.values()
        )

        await repo.delete_connection_webhook_receipts(
            connection_id=conn_a, user_id=user1
        )
        assert not any(
            r["connection_id"] == conn_a for r in pool.webhook_receipts.values()
        )

    @pytest.mark.asyncio
    async def test_projection_owned_events_preserves_manual_events(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        await repo.delete_connection_projection_owned_events(
            connection_id=world["conn_a"], user_id=world["user1"]
        )
        assert world["proj_event_a1"] not in pool.events
        assert world["proj_event_a2"] not in pool.events
        # Manual event for the same user is preserved.
        assert world["manual_event"] in pool.events
        # Other connections' projection events survive.
        assert world["proj_event_b1"] in pool.events

    @pytest.mark.asyncio
    async def test_projection_ledger_clears_only_target_connection(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        await repo.delete_connection_projection_ledger(
            connection_id=world["conn_a"], user_id=world["user1"]
        )
        assert not any(
            r["connection_id"] == world["conn_a"]
            for r in pool.projections.values()
        )
        # Other connections' ledger rows survive.
        assert any(
            r["connection_id"] == world["conn_b"]
            for r in pool.projections.values()
        )
        assert any(
            r["connection_id"] == world["conn_c"]
            for r in pool.projections.values()
        )

    @pytest.mark.asyncio
    async def test_wrong_user_primitive_deletes_nothing(
        self, repo: HealthSyncRepository, pool: _HealthDeletePool, populated_world
    ):
        world = populated_world
        before = len(pool.source_records)
        # user2 tries to delete conn_a's source records - user_id mismatch.
        await repo.delete_connection_source_records(
            connection_id=world["conn_a"], user_id=world["user2"]
        )
        assert len(pool.source_records) == before
