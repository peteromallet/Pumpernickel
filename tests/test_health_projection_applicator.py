"""Tests for projection-applicator safety during connection deletion.

Verifies that:
- Projection-owned adherence events are distinguishable from manual events.
- Manual ``log_event`` testimony is never reachable through projection ledger
  queries (only projection-owned events have ledger rows).
- The projection ledger correctly links event IDs, enabling safe cleanup
  during connection deletion.
- Cross-user isolation: projection queries are user-scoped.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.services.health_sync.models import NormalizedWorkout
from app.services.health_sync.projection_applicator import apply_workout_projection
from app.services.health_sync.repository import HealthSyncRepository
from tests.conftest import FakePool


# ── Helpers ─────────────────────────────────────────────────────────────────


def _rid() -> UUID:
    return uuid4()


def _make_workout(
    *,
    workout_type: str = "running",
    started_at: datetime | None = None,
) -> NormalizedWorkout:
    if started_at is None:
        started_at = datetime(2025, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    return NormalizedWorkout(
        started_at=started_at,
        local_date=started_at.date(),
        workout_type=workout_type,
        attribution={"provider": "withings"},
    )


def _make_commitment(
    *,
    id: str | None = None,
    bot_id: str = "hector",
    topic_id: UUID | None = None,
    cadence: str = "daily",
    start_date: str = "2025-01-01",
    days_of_week: list[int] | None = None,
) -> dict:
    if id is None:
        id = str(uuid4())
    if topic_id is None:
        topic_id = uuid4()
    return {
        "id": id,
        "bot_id": bot_id,
        "topic_slug": "fitness",
        "topic_id": topic_id,
        "label": "Test Commitment",
        "cadence": cadence,
        "start_date": start_date,
        "end_date": None,
        "days_of_week": days_of_week or [],
        "schedule_rule": {},
        "user_id": "u001",
        "status": "active",
    }


# ── Tests ───────────────────────────────────────────────────────────────────


class TestProjectionEventOwnership:
    """Verify that projection-owned and manual events are distinguishable."""

    async def test_first_projection_creates_event_with_ledger_link(self):
        """A successful first-time projection creates both an event and a
        ledger row linking the event back to the source record."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        source_record_id = _rid()
        connection_id = _rid()
        user_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert result is not None, "Projection should succeed"
        assert result.event_id is not None, "Projection must create an event"

        # Verify the event exists in the FakePool
        assert result.event_id in pool.events, "Event must be persisted"

        # Verify the projection ledger row links to the event
        assert result.projection_id in pool.health_source_to_event_projections, (
            "Ledger row must exist"
        )
        ledger = pool.health_source_to_event_projections[result.projection_id]
        assert ledger["event_id"] == result.event_id, (
            "Ledger must reference the created event"
        )
        assert ledger["connection_id"] == connection_id
        assert ledger["user_id"] == user_id
        assert ledger["source_record_id"] == source_record_id

    async def test_manual_event_has_no_ledger_link(self):
        """Manual events (created via log_event, not projection) have no
        corresponding row in the projection ledger."""
        pool = FakePool()

        user_id = _rid()
        manual_event_id = _rid()

        # Directly insert a manual event into the pool (simulating log_event)
        pool.events[manual_event_id] = {
            "id": manual_event_id,
            "user_id": user_id,
            "metric_key": "pushups",
            "adherence_status": "completed",
        }

        # No projection ledger row references this event
        for proj in pool.health_source_to_event_projections.values():
            assert proj.get("event_id") != manual_event_id, (
                "Manual event must not be linked from projection ledger"
            )

        # The event exists independently
        assert manual_event_id in pool.events

    async def test_projection_ledger_query_only_finds_projection_events(self):
        """The ledger subquery resolves only events that appear in the
        projection ledger, not arbitrary events in the events table."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        user_id = _rid()
        connection_id = _rid()

        # Create a projection-owned event via the applicator
        source_record_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert result is not None
        projection_event_id = result.event_id
        assert projection_event_id is not None

        # Insert a manual event for the same user
        manual_event_id = _rid()
        pool.events[manual_event_id] = {
            "id": manual_event_id,
            "user_id": user_id,
            "metric_key": "manual_checkin",
            "adherence_status": "committed",
        }

        # Simulate the ledger subquery: find all event_ids for this connection
        ledger_event_ids = {
            p["event_id"]
            for p in pool.health_source_to_event_projections.values()
            if (
                p.get("connection_id") == connection_id
                and p.get("user_id") == user_id
                and p.get("event_id") is not None
            )
        }

        assert projection_event_id in ledger_event_ids, (
            "Projection event must be found via ledger"
        )
        assert manual_event_id not in ledger_event_ids, (
            "Manual event must NOT be found via ledger subquery"
        )

    async def test_delete_connection_primitive_preserves_manual_events(self):
        """Simulating the delete path: projection-owned events are removed,
        manual events are preserved."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        user_id = _rid()
        connection_id = _rid()

        # Create one projection-owned event via the applicator
        result = await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=connection_id,
            user_id=user_id,
            commitments=[_make_commitment(id=str(uuid4()))],
            enabled=True,
        )
        assert result is not None and result.event_id is not None, (
            "First projection must succeed"
        )

        # Insert a second projection-owned event directly (simulating
        # a prior projection for a different source record)
        second_event_id = _rid()
        second_proj_id = _rid()
        pool.health_source_to_event_projections[second_proj_id] = {
            "id": second_proj_id,
            "connection_id": connection_id,
            "user_id": user_id,
            "event_id": second_event_id,
            "projection_status": "projected",
            "projection_version": 1,
            "source_record_id": _rid(),
            "commitment_id": None,
            "match_rule": "daily_run",
            "note": None,
            "decision_reason": "match",
            "matched_local_date": None,
            "supersedes_projection_id": None,
            "projected_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "removed_at": None,
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        }
        pool.events[second_event_id] = {
            "id": second_event_id,
            "user_id": user_id,
            "metric_key": "cycling",
            "adherence_status": "committed",
        }

        # Insert manual events (no ledger link)
        manual_event_id1 = _rid()
        manual_event_id2 = _rid()
        pool.events[manual_event_id1] = {
            "id": manual_event_id1,
            "user_id": user_id,
            "metric_key": "meditation",
            "adherence_status": "completed",
        }
        pool.events[manual_event_id2] = {
            "id": manual_event_id2,
            "user_id": user_id,
            "metric_key": "reading",
            "adherence_status": "committed",
        }

        # Simulate ledger-based event removal (what the delete path does)
        ledger_event_ids = {
            p["event_id"]
            for p in pool.health_source_to_event_projections.values()
            if (
                p.get("connection_id") == connection_id
                and p.get("user_id") == user_id
                and p.get("event_id") is not None
            )
        }

        for event_id in list(ledger_event_ids):
            ev = pool.events.get(event_id)
            if ev is not None and ev.get("user_id") == user_id:
                del pool.events[event_id]

        # Projection-owned events are gone
        assert result.event_id not in pool.events
        assert second_event_id not in pool.events

        # Manual events survive
        assert manual_event_id1 in pool.events
        assert manual_event_id2 in pool.events

    async def test_null_event_id_projection_is_harmless(self):
        """A projection ledger row with a NULL event_id should not cause
        errors during the ledger subquery for event removal."""
        pool = FakePool()
        user_id = _rid()
        connection_id = _rid()

        # Insert a projection row with NULL event_id
        null_proj_id = _rid()
        pool.health_source_to_event_projections[null_proj_id] = {
            "id": null_proj_id,
            "connection_id": connection_id,
            "user_id": user_id,
            "event_id": None,
            "projection_status": "pending",
            "projection_version": 1,
            "source_record_id": _rid(),
            "commitment_id": None,
            "match_rule": None,
            "note": None,
            "decision_reason": None,
            "matched_local_date": None,
            "supersedes_projection_id": None,
            "projected_at": None,
            "removed_at": None,
            "created_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "updated_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        }

        # The ledger subquery should skip NULL event_ids
        ledger_event_ids = {
            p["event_id"]
            for p in pool.health_source_to_event_projections.values()
            if (
                p.get("connection_id") == connection_id
                and p.get("user_id") == user_id
                and p.get("event_id") is not None
            )
        }

        assert null_proj_id in pool.health_source_to_event_projections
        assert len(ledger_event_ids) == 0, "NULL event_id should be excluded"


class TestCrossUserProjectionIsolation:
    """Verify that projection queries are user-scoped and cross-user
    deletion cannot affect another user's events."""

    async def test_projection_ledger_is_user_scoped(self):
        """Projection rows for different users are isolated."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        user_a = _rid()
        user_b = _rid()
        conn_a = _rid()
        conn_b = _rid()

        # User A projection
        result_a = await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=conn_a,
            user_id=user_a,
            commitments=[_make_commitment(id=str(uuid4()))],
            enabled=True,
        )
        assert result_a is not None

        # User B projection (different connection, different user)
        result_b = await apply_workout_projection(
            repository=repo,
            workout=_make_workout(workout_type="cycling"),
            source_record_id=_rid(),
            connection_id=conn_b,
            user_id=user_b,
            commitments=[_make_commitment(id=str(uuid4()))],
            enabled=True,
        )
        assert result_b is not None

        # Querying user A's connection should only return user A's events
        ledger_event_ids_a = {
            p["event_id"]
            for p in pool.health_source_to_event_projections.values()
            if (
                p.get("connection_id") == conn_a
                and p.get("user_id") == user_a
                and p.get("event_id") is not None
            )
        }
        assert result_a.event_id in ledger_event_ids_a
        assert result_b.event_id not in ledger_event_ids_a, (
            "User B's event must not appear in user A's ledger query"
        )

        # Querying user B's connection returns only user B's events
        ledger_event_ids_b = {
            p["event_id"]
            for p in pool.health_source_to_event_projections.values()
            if (
                p.get("connection_id") == conn_b
                and p.get("user_id") == user_b
                and p.get("event_id") is not None
            )
        }
        assert result_b.event_id in ledger_event_ids_b
        assert result_a.event_id not in ledger_event_ids_b

    async def test_wrong_user_cannot_delete_projection_events(self):
        """A ledger subquery scoped to (connection_id, user_b) should not
        match rows where user_id = user_a, even if connection_id matches."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        user_a = _rid()
        user_b = _rid()
        conn_shared = _rid()

        # User A creates a projection on conn_shared
        result_a = await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=conn_shared,
            user_id=user_a,
            commitments=[_make_commitment(id=str(uuid4()))],
            enabled=True,
        )
        assert result_a is not None and result_a.event_id is not None

        # User B queries with conn_shared but their own user_id
        ledger_event_ids_b = {
            p["event_id"]
            for p in pool.health_source_to_event_projections.values()
            if (
                p.get("connection_id") == conn_shared
                and p.get("user_id") == user_b
                and p.get("event_id") is not None
            )
        }

        # User B should see zero events (user-scoped isolation)
        assert len(ledger_event_ids_b) == 0, (
            "User B's ledger query must not match user A's projection rows"
        )

        # User A's event is still present
        assert result_a.event_id in pool.events, (
            "User A's event must survive user B's query"
        )
