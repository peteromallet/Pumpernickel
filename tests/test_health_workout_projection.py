"""Repository / FakePool projection transaction tests.

Covers default-off behaviour, exactly-one event creation, retry
idempotency, concurrent replay defence where simulatable,
revision / rematch supersession, tombstone reversal, and
manual-event isolation — all exercised through the repository
primitives and the FakePool simulation layer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.services.health_sync.models import NormalizedWorkout
from app.services.health_sync.projection_applicator import apply_workout_projection
from app.services.health_sync.repository import (
    HealthProjectionRecord,
    HealthSyncRepository,
    repository_for,
)
from tests.conftest import FakePool


# ── Helpers ─────────────────────────────────────────────────────────────────


def _rid() -> UUID:
    return uuid4()


def _make_workout(
    *,
    local_date: date | None = date(2025, 6, 16),
    workout_type: str = "running",
    started_at: datetime | None = None,
) -> NormalizedWorkout:
    if started_at is None:
        started_at = datetime(2025, 6, 16, 8, 0, 0, tzinfo=timezone.utc)
    return NormalizedWorkout(
        started_at=started_at,
        local_date=local_date,
        workout_type=workout_type,
        attribution={"provider": "withings"},
    )


def _make_commitment(
    *,
    id: str | None = None,
    bot_id: str = "hector",
    topic_slug: str = "fitness",
    topic_id: UUID | None = None,
    cadence: str = "daily",
    start_date: date | str | None = date(2025, 1, 1),
    end_date: date | str | None = None,
    days_of_week: list[int] | None = None,
) -> dict:
    if id is None:
        id = str(uuid4())
    if topic_id is None:
        topic_id = uuid4()
    return {
        "id": id,
        "bot_id": bot_id,
        "topic_slug": topic_slug,
        "topic_id": topic_id,
        "label": "Test Commitment",
        "cadence": cadence,
        "start_date": start_date.isoformat() if isinstance(start_date, date) else start_date,
        "end_date": end_date.isoformat() if isinstance(end_date, date) else end_date,
        "days_of_week": days_of_week or [],
        "schedule_rule": {},
        "user_id": "u001",
        "status": "active",
    }


# ═════════════════════════════════════════════════════════════════════════════
# FakePool projection simulation tests
# ═════════════════════════════════════════════════════════════════════════════


class TestFakePoolProjectionSimulation:
    """Verify the FakePool correctly simulates the projection ledger,
    including insert, find-active, supersede, remove, detach, and
    event-ownership lookups."""

    async def test_insert_projection_populates_all_fields(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        cid = uuid4()
        now = datetime(2025, 6, 16, 12, 0, tzinfo=timezone.utc)

        record = await repo.insert_projection(
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitment_id=cid,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
            note="test note",
            decision_reason="matched",
            matched_local_date=date(2025, 6, 16),
            projected_at=now,
        )

        assert record.projection_id is not None
        assert record.source_record_id == src_id
        assert record.connection_id == conn_id
        assert record.user_id == user_id
        assert record.commitment_id == cid
        assert record.projection_version == 1
        assert record.projection_status == "projected"
        assert record.match_rule == "workout_auto_projection"
        assert record.note == "test note"
        assert record.decision_reason == "matched"
        assert record.matched_local_date == date(2025, 6, 16)
        assert record.supersedes_projection_id is None
        assert record.event_id is None

        # Verify it's in the FakePool dict.
        assert record.projection_id in pool.health_source_to_event_projections

    async def test_find_active_projection_returns_only_active(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()

        # Insert a 'pending' row.
        pending = await repo.insert_projection(
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            projection_version=1,
            projection_status="pending",
            match_rule="workout_auto_projection",
        )
        # Pending must be found by find_active_projection.
        found = await repo.find_active_projection(
            source_record_id=src_id, user_id=user_id
        )
        assert found is not None
        assert found.projection_id == pending.projection_id
        assert found.projection_status == "pending"

        # Mark it 'removed' — it should no longer be found.
        await repo.remove_projection(
            projection_id=pending.projection_id,
            user_id=user_id,
        )
        gone = await repo.find_active_projection(
            source_record_id=src_id, user_id=user_id
        )
        assert gone is None

    async def test_find_active_projection_returns_none_for_different_user(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        src_id = _rid()
        conn_id = _rid()
        user_a = _rid()
        user_b = _rid()

        await repo.insert_projection(
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_a,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        # User B must not see User A's projection.
        found = await repo.find_active_projection(
            source_record_id=src_id, user_id=user_b
        )
        assert found is None

    async def test_find_active_projection_for_update_locks(self) -> None:
        """FOR UPDATE variant returns the same result — the FakePool
        doesn't simulate row locking but must return the correct row."""
        pool = FakePool()
        repo = repository_for(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()

        await repo.insert_projection(
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        found = await repo.find_active_projection(
            source_record_id=src_id, user_id=user_id, for_update=True
        )
        assert found is not None
        assert found.source_record_id == src_id

    async def test_find_projection_by_event_returns_owner(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_id = _rid()
        cid = uuid4()
        tid = uuid4()

        # Create an event first.
        event = await repo.create_projection_event(
            commitment_id=cid,
            user_id=user_id,
            topic_id=tid,
            bot_id="hector",
            metric_key="workout",
            adherence_status="done",
        )
        event_id = event["id"]

        # Link it via a projection.
        await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            event_id=event_id,
            commitment_id=cid,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        # find_projection_by_event must return the projection.
        owned = await repo.find_projection_by_event(event_id=event_id)
        assert owned is not None
        assert owned.event_id == event_id

    async def test_find_projection_by_event_returns_none_for_manual(self) -> None:
        """Manual events (not linked to any projection row) must return None."""
        pool = FakePool()
        repo = repository_for(pool)

        orphan_event_id = uuid4()
        pool.events[orphan_event_id] = {
            "id": orphan_event_id,
            "commitment_id": uuid4(),
            "user_id": _rid(),
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, tzinfo=timezone.utc),
            "note": "Manual log",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
        }

        owned = await repo.find_projection_by_event(event_id=orphan_event_id)
        assert owned is None

    async def test_supersede_projection_changes_status(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        now = datetime(2025, 6, 17, 12, 0, tzinfo=timezone.utc)

        proj = await repo.insert_projection(
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        result = await repo.supersede_projection(
            existing_projection_id=proj.projection_id,
            user_id=user_id,
            now=now,
        )
        assert result.projection_status == "superseded"
        assert result.removed_at == now

        # Must no longer be found as active.
        active = await repo.find_active_projection(
            source_record_id=src_id, user_id=user_id
        )
        assert active is None

    async def test_supersede_projection_wrong_user_raises(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_a = _rid()
        user_b = _rid()

        proj = await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_a,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        with pytest.raises(LookupError):
            await repo.supersede_projection(
                existing_projection_id=proj.projection_id,
                user_id=user_b,
            )

    async def test_remove_projection_changes_status_and_detaches_event(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_id = _rid()
        cid = uuid4()
        tid = uuid4()
        now = datetime(2025, 6, 18, 12, 0, tzinfo=timezone.utc)

        event = await repo.create_projection_event(
            commitment_id=cid,
            user_id=user_id,
            topic_id=tid,
            bot_id="hector",
            metric_key="workout",
            adherence_status="done",
        )
        event_id = event["id"]

        proj = await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            event_id=event_id,
            commitment_id=cid,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        result = await repo.remove_projection(
            projection_id=proj.projection_id,
            user_id=user_id,
            now=now,
        )
        assert result.projection_status == "removed"
        assert result.event_id is None
        assert result.removed_at == now

    async def test_remove_projection_wrong_user_raises(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_a = _rid()
        user_b = _rid()

        proj = await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_a,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        with pytest.raises(LookupError):
            await repo.remove_projection(
                projection_id=proj.projection_id,
                user_id=user_b,
            )

    async def test_detach_projection_event_sets_null(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_id = _rid()
        cid = uuid4()
        tid = uuid4()
        now = datetime(2025, 6, 19, 12, 0, tzinfo=timezone.utc)

        event = await repo.create_projection_event(
            commitment_id=cid,
            user_id=user_id,
            topic_id=tid,
        )
        event_id = event["id"]

        proj = await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            event_id=event_id,
            commitment_id=cid,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        result_id = await repo.detach_projection_event(
            projection_id=proj.projection_id,
            user_id=user_id,
            now=now,
        )
        assert result_id == proj.projection_id

        # Check the FakePool row directly.
        row = pool.health_source_to_event_projections[proj.projection_id]
        assert row["event_id"] is None

    async def test_detach_projection_event_wrong_user_raises(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_a = _rid()
        user_b = _rid()

        proj = await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_a,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        with pytest.raises(LookupError):
            await repo.detach_projection_event(
                projection_id=proj.projection_id,
                user_id=user_b,
            )

    async def test_multiple_versions_only_one_active(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()

        # Insert v1 as projected.
        v1 = await repo.insert_projection(
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        # Mark v1 superseded.
        await repo.supersede_projection(
            existing_projection_id=v1.projection_id,
            user_id=user_id,
        )

        # Insert v2 (the successor).
        v2 = await repo.insert_projection(
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            projection_version=2,
            projection_status="projected",
            match_rule="workout_auto_projection",
            supersedes_projection_id=v1.projection_id,
        )

        # Only v2 should be active.
        active = await repo.find_active_projection(
            source_record_id=src_id, user_id=user_id
        )
        assert active is not None
        assert active.projection_id == v2.projection_id
        assert active.projection_version == 2

        # v1 must still exist in the ledger (superseded).
        assert v1.projection_id in pool.health_source_to_event_projections
        v1_row = pool.health_source_to_event_projections[v1.projection_id]
        assert v1_row["projection_status"] == "superseded"

    async def test_create_projection_event_populates_all_fields(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_id = _rid()
        cid = uuid4()
        tid = uuid4()
        observed = datetime(2025, 6, 16, 8, 30, tzinfo=timezone.utc)

        result = await repo.create_projection_event(
            commitment_id=cid,
            user_id=user_id,
            topic_id=tid,
            bot_id="hector",
            metric_key="workout",
            adherence_status="done",
            value_numeric=42.5,
            value_text="test value",
            unit="km",
            observed_at=observed,
            note="Auto-projected from Withings workout",
            source_message_ids=[],
        )

        assert "id" in result
        assert result["commitment_id"] == cid
        assert result["metric_key"] == "workout"
        assert result["adherence_status"] == "done"
        assert result["observed_at"] == observed

        # Verify in FakePool.
        assert result["id"] in pool.events
        event = pool.events[result["id"]]
        assert event["commitment_id"] == cid
        assert event["user_id"] == user_id
        assert event["topic_id"] == tid
        assert event["bot_id"] == "hector"
        assert event["metric_key"] == "workout"
        assert event["adherence_status"] == "done"
        assert event["value_numeric"] == 42.5
        assert event["value_text"] == "test value"
        assert event["unit"] == "km"
        assert event["observed_at"] == observed
        assert event["note"] == "Auto-projected from Withings workout"

    async def test_delete_projection_event_removes_from_pool(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_id = _rid()
        cid = uuid4()
        tid = uuid4()

        event = await repo.create_projection_event(
            commitment_id=cid,
            user_id=user_id,
            topic_id=tid,
            bot_id="hector",
            metric_key="workout",
            adherence_status="done",
        )
        event_id = event["id"]

        # Link it.
        proj = await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            event_id=event_id,
            commitment_id=cid,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        # Verify ownership, then delete.
        owned = await repo.find_projection_by_event(event_id=event_id)
        assert owned is not None

        deleted = await repo.delete_projection_event(
            event_id=event_id, user_id=user_id
        )
        assert deleted is True
        assert event_id not in pool.events

    async def test_delete_projection_event_wrong_user_fails(self) -> None:
        pool = FakePool()
        repo = repository_for(pool)
        user_a = _rid()
        user_b = _rid()
        cid = uuid4()
        tid = uuid4()

        event = await repo.create_projection_event(
            commitment_id=cid,
            user_id=user_a,
            topic_id=tid,
            bot_id="hector",
            metric_key="workout",
            adherence_status="done",
        )
        event_id = event["id"]

        deleted = await repo.delete_projection_event(
            event_id=event_id, user_id=user_b
        )
        assert deleted is False
        assert event_id in pool.events

    async def test_fake_pool_copy_includes_projections(self) -> None:
        """Verify that deep-copying the FakePool preserves projection rows."""
        pool = FakePool()
        repo = repository_for(pool)
        user_id = _rid()

        await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        # FakePool doesn't expose .snapshot() directly, but health
        # source record tests use the seed pattern to verify copy
        # semantics.  We verify that the projection dict is populated
        # and survives a manual dict copy.
        copied = dict(pool.health_source_to_event_projections)
        assert len(copied) == 1

    async def test_fake_pool_replace_restores_empty_projections(self) -> None:
        """Replacing the projection dict restores the previous state."""
        pool = FakePool()
        repo = repository_for(pool)
        user_id = _rid()

        await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )
        assert len(pool.health_source_to_event_projections) == 1

        # Save the current dict, then add another projection.
        saved = dict(pool.health_source_to_event_projections)
        await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            projection_version=1,
            projection_status="pending",
            match_rule="workout_auto_projection",
        )
        assert len(pool.health_source_to_event_projections) == 2

        # Replace to simulate rollback.
        pool.health_source_to_event_projections = saved
        assert len(pool.health_source_to_event_projections) == 1


# ═════════════════════════════════════════════════════════════════════════════
# Projection transaction tests (via applicator through FakePool)
# ═════════════════════════════════════════════════════════════════════════════


class TestDefaultOffBehavior:
    """When the feature flag is off, zero side effects must occur at
    every level — no events, no ledger rows, no FakePool mutations."""

    async def test_disabled_returns_none(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()
        commitment = _make_commitment()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=False,
        )
        assert result is None

    async def test_disabled_no_events_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        initial_event_count = len(pool.events)

        await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[_make_commitment()],
            enabled=False,
        )
        assert len(pool.events) == initial_event_count

    async def test_disabled_no_projection_rows_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        initial_proj_count = len(pool.health_source_to_event_projections)

        await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[_make_commitment()],
            enabled=False,
        )
        assert len(pool.health_source_to_event_projections) == initial_proj_count


class TestExactlyOneEventCreation:
    """A successful first-time projection must create exactly one event
    and exactly one ledger row — no more, no less."""

    async def test_exactly_one_event_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()

        result = await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            commitments=[_make_commitment()],
            enabled=True,
        )
        assert result is not None
        assert len(pool.events) == 1

    async def test_exactly_one_ledger_row_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[_make_commitment()],
            enabled=True,
        )
        assert len(pool.health_source_to_event_projections) == 1

    async def test_event_linked_correctly_in_ledger_row(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        cid = uuid4()
        tid = uuid4()

        result = await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            commitments=[_make_commitment(id=str(cid), topic_id=tid)],
            enabled=True,
        )
        assert result is not None
        assert result.event_id is not None
        assert result.event_id in pool.events

        # The event must have the correct fields.
        event = pool.events[result.event_id]
        assert event["metric_key"] == "workout"
        assert event["adherence_status"] == "done"
        assert event["commitment_id"] == cid
        assert event["user_id"] == user_id
        assert event["topic_id"] == tid
        assert event["bot_id"] == "hector"


class TestRetryIdempotency:
    """Calling the applicator again with the same inputs must return
    the existing projection without creating duplicates."""

    async def test_same_source_same_version_returns_existing(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        assert first is not None

        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        assert second is not None
        assert second.projection_id == first.projection_id

    async def test_idempotent_replay_no_duplicate_event(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        count_after_first = len(pool.events)

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        assert len(pool.events) == count_after_first

    async def test_idempotent_replay_no_duplicate_ledger(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        count_after_first = len(pool.health_source_to_event_projections)

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        assert len(pool.health_source_to_event_projections) == count_after_first


class TestConcurrentReplayDefense:
    """When multiple calls race against the same source record, the
    FakePool's active-projection lookup prevents duplicate creation."""

    async def test_different_sources_each_get_own_projection(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        conn_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        p1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        p2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),  # different source
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert p1 is not None
        assert p2 is not None
        assert p1.projection_id != p2.projection_id
        assert len(pool.events) == 2
        assert len(pool.health_source_to_event_projections) == 2

    async def test_same_source_different_users_independent(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_a = _rid()
        user_b = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        p_a = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_a,
            commitments=[commitment],
            enabled=True,
        )
        p_b = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_b,
            commitments=[commitment],
            enabled=True,
        )

        assert p_a is not None
        assert p_b is not None
        # Different users → different projections.
        assert p_a.projection_id != p_b.projection_id


class TestRevisionSupersession:
    """When projection_version is bumped, the old projection is
    superseded, its event is cleaned up, and a new projection is
    created — all visible in the FakePool."""

    async def test_revision_supersedes_old_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        v1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        old_proj_id = v1.projection_id

        v2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )
        assert v2 is not None
        assert v2.projection_version == 2
        assert v2.supersedes_projection_id == old_proj_id

        # Old row in FakePool must be superseded.
        old_row = pool.health_source_to_event_projections[old_proj_id]
        assert old_row["projection_status"] == "superseded"

    async def test_revision_old_event_removed_from_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        v1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        old_event_id = v1.event_id
        assert old_event_id in pool.events

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )

        # Old event must be gone, new event must exist.
        assert old_event_id not in pool.events
        assert len(pool.events) == 1

    async def test_revision_chain_visible_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        commitment = _make_commitment()
        workout = _make_workout()

        v1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        v2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )
        v3 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=3,
        )

        # Chain linkage.
        assert v3.supersedes_projection_id == v2.projection_id
        assert v2.supersedes_projection_id == v1.projection_id

        # Statuses in pool.
        assert pool.health_source_to_event_projections[v1.projection_id]["projection_status"] == "superseded"
        assert pool.health_source_to_event_projections[v2.projection_id]["projection_status"] == "superseded"
        assert pool.health_source_to_event_projections[v3.projection_id]["projection_status"] == "projected"


class TestRematchSupersession:
    """When commitments change and version is bumped, a rematch occurs
    with proper supersession of the old projection."""

    async def test_rematch_different_commitment_supersedes_old(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        workout = _make_workout()

        c_a = _make_commitment(id=str(uuid4()), topic_id=uuid4())
        c_b = _make_commitment(id=str(uuid4()), topic_id=uuid4())

        v1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[c_a],
            enabled=True,
            projection_version=1,
        )
        v2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[c_b],
            enabled=True,
            projection_version=2,
        )

        assert str(v2.commitment_id) == c_b["id"]
        assert v2.supersedes_projection_id == v1.projection_id

        old_row = pool.health_source_to_event_projections[v1.projection_id]
        assert old_row["projection_status"] == "superseded"

    async def test_rematch_no_eligible_commitment_cleanup_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        workout = _make_workout()

        v1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[_make_commitment()],
            enabled=True,
            projection_version=1,
        )
        old_event_id = v1.event_id

        # Rematch with no viable commitment.
        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[_make_commitment(bot_id="sage")],
            enabled=True,
            projection_version=2,
        )
        assert result is None

        # Old must be superseded, event cleaned up.
        old_row = pool.health_source_to_event_projections[v1.projection_id]
        assert old_row["projection_status"] == "superseded"
        assert old_event_id not in pool.events


class TestTombstoneReversal:
    """When ``is_tombstone=True``, the active projection is removed
    and its event deleted — all visible in the FakePool."""

    async def test_tombstone_removes_projection_in_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        # The projection row must now be 'removed'.
        proj_rows = [
            r for r in pool.health_source_to_event_projections.values()
            if r["source_record_id"] == src_id
        ]
        assert len(proj_rows) == 1
        assert proj_rows[0]["projection_status"] == "removed"
        assert proj_rows[0]["event_id"] is None

    async def test_tombstone_deletes_projection_event_from_pool(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        event_id = first.event_id
        assert event_id in pool.events

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        assert event_id not in pool.events

    async def test_tombstone_no_existing_projection_is_noop(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        initial_events = len(pool.events)
        initial_projs = len(pool.health_source_to_event_projections)

        result = await apply_workout_projection(
            repository=repo,
            workout=_make_workout(),
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[_make_commitment()],
            enabled=True,
            is_tombstone=True,
        )

        assert result is None
        assert len(pool.events) == initial_events
        assert len(pool.health_source_to_event_projections) == initial_projs

    async def test_tombstone_disabled_is_noop(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        src_id = _rid()
        conn_id = _rid()
        user_id = _rid()
        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None

        # Tombstone with disabled flag.
        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=False,
            is_tombstone=True,
        )

        # Existing projection must still be active.
        proj_rows = [
            r for r in pool.health_source_to_event_projections.values()
            if r["source_record_id"] == src_id
        ]
        assert len(proj_rows) == 1
        assert proj_rows[0]["projection_status"] == "projected"


class TestManualEventIsolation:
    """Manual ``log_event`` testimony must never be touched by projection
    code — not during tombstone, revision, or rematch.  The FakePool
    must correctly distinguish projection-owned from manual events."""

    async def test_manual_event_survives_tombstone(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        src_id = _rid()
        conn_id = _rid()

        # Insert a manual event.
        manual_id = uuid4()
        pool.events[manual_id] = {
            "id": manual_id,
            "commitment_id": uuid4(),
            "user_id": user_id,
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, tzinfo=timezone.utc),
            "note": "Manual log",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
        }

        workout = _make_workout()
        commitment = _make_commitment()

        # Create a projection (uses a different event).
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None
        assert first.event_id != manual_id

        # Tombstone.
        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        # Manual event must survive.
        assert manual_id in pool.events
        assert pool.events[manual_id]["note"] == "Manual log"

    async def test_manual_event_survives_revision(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        src_id = _rid()
        conn_id = _rid()

        manual_id = uuid4()
        pool.events[manual_id] = {
            "id": manual_id,
            "commitment_id": uuid4(),
            "user_id": user_id,
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, tzinfo=timezone.utc),
            "note": "Manual testimony",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
        }

        workout = _make_workout()
        commitment = _make_commitment()

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=src_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )

        assert manual_id in pool.events
        assert pool.events[manual_id]["note"] == "Manual testimony"

    async def test_find_projection_by_event_isolates_manual(self) -> None:
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        manual_id = uuid4()
        pool.events[manual_id] = {
            "id": manual_id,
            "commitment_id": uuid4(),
            "user_id": _rid(),
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, tzinfo=timezone.utc),
            "note": "Orphan",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
        }

        owned = await repo.find_projection_by_event(event_id=manual_id)
        assert owned is None  # Not linked → manual → never touched.

    async def test_delete_projection_event_fails_on_wrong_user(self) -> None:
        """``delete_projection_event`` does a user-scoped guard — it
        must refuse to delete when the event belongs to a different user.
        The ownership guard against manual events is performed by
        ``find_projection_by_event`` *before* calling delete."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_a = _rid()
        user_b = _rid()
        cid = uuid4()
        tid = uuid4()

        event = await repo.create_projection_event(
            commitment_id=cid,
            user_id=user_a,
            topic_id=tid,
            bot_id="hector",
            metric_key="workout",
            adherence_status="done",
        )
        event_id = event["id"]

        await repo.insert_projection(
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_a,
            event_id=event_id,
            commitment_id=cid,
            projection_version=1,
            projection_status="projected",
            match_rule="workout_auto_projection",
        )

        # User B tries to delete — must be refused.
        deleted = await repo.delete_projection_event(
            event_id=event_id, user_id=user_b
        )
        assert deleted is False
        assert event_id in pool.events
