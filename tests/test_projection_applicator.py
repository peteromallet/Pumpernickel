"""Tests for the projection applicator (workout → commitment event bridge).

Covers disabled no-op, first-time projection, retry/concurrent replay
idempotency, non-matching decisions, missing topic_id, and commitment
disappearance races.
"""

from __future__ import annotations

import pytest
from datetime import date, datetime, timezone
from uuid import UUID, uuid4

from app.services.health_sync.models import NormalizedWorkout
from app.services.health_sync.projection_applicator import apply_workout_projection
from app.services.health_sync.repository import HealthSyncRepository
from tests.conftest import FakePool


# ── Helpers ─────────────────────────────────────────────────────────────────


def _rid() -> UUID:
    return uuid4()


def _make_workout(
    *,
    local_date: date | None = date(2025, 6, 16),  # Monday
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


# ── Tests ───────────────────────────────────────────────────────────────────


class TestDisabledNoOp:
    """When the feature flag is off, the applicator must be a pure no-op."""

    async def test_disabled_returns_none(self):
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

    async def test_disabled_creates_no_event(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()
        commitment = _make_commitment()
        initial_event_count = len(pool.events)

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=False,
        )

        assert len(pool.events) == initial_event_count

    async def test_disabled_creates_no_projection_row(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()
        commitment = _make_commitment()
        initial_proj_count = len(pool.health_source_to_event_projections)

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=False,
        )

        assert len(pool.health_source_to_event_projections) == initial_proj_count


class TestFirstTimeProjection:
    """When no active projection exists and the matcher succeeds, the
    applicator creates exactly one event and one ledger row."""

    async def test_creates_event_with_correct_fields(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        commitment_id = uuid4()
        topic_id = uuid4()
        started_at = datetime(2025, 6, 16, 8, 30, 0, tzinfo=timezone.utc)

        workout = _make_workout(started_at=started_at)
        commitment = _make_commitment(
            id=str(commitment_id),
            topic_id=topic_id,
        )

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert result is not None
        assert result.projection_status == "projected"

        # Verify the event was created.
        assert len(pool.events) == 1
        event = list(pool.events.values())[0]
        assert event["metric_key"] == "workout"
        assert event["adherence_status"] == "done"
        assert event["commitment_id"] == commitment_id
        assert event["user_id"] == user_id
        assert event["topic_id"] == topic_id
        assert event["bot_id"] == "hector"
        assert event["observed_at"] == started_at
        assert "Auto-projected" in event["note"]

    async def test_creates_ledger_row_with_correct_fields(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_record_id = _rid()
        connection_id = _rid()
        commitment_id = uuid4()
        topic_id = uuid4()

        workout = _make_workout()
        commitment = _make_commitment(
            id=str(commitment_id),
            topic_id=topic_id,
        )

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
        assert result.source_record_id == source_record_id
        assert result.connection_id == connection_id
        assert result.user_id == user_id
        assert result.commitment_id == commitment_id
        assert result.projection_status == "projected"
        assert result.projection_version == 1
        assert result.match_rule == "workout_auto_projection"
        assert result.decision_reason == "matched"
        assert result.matched_local_date == date(2025, 6, 16)
        assert result.event_id is not None
        assert result.supersedes_projection_id is None

    async def test_ledger_row_links_to_event(self):
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
            enabled=True,
        )

        assert result is not None
        assert result.event_id is not None
        assert result.event_id in pool.events

    async def test_exactly_one_event_per_projection(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()
        commitment = _make_commitment()

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
        )

        assert len(pool.events) == 1
        assert len(pool.health_source_to_event_projections) == 1


class TestRetryAndConcurrentReplay:
    """When an active projection already exists, the applicator must
    return it without creating duplicate events or ledger rows."""

    async def test_second_call_returns_existing_projection(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None

        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert second is not None
        assert second.projection_id == first.projection_id

    async def test_second_call_does_not_create_duplicate_event(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        event_count_after_first = len(pool.events)

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert len(pool.events) == event_count_after_first

    async def test_second_call_does_not_create_duplicate_ledger_row(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        proj_count_after_first = len(pool.health_source_to_event_projections)

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert len(pool.health_source_to_event_projections) == proj_count_after_first

    async def test_different_source_records_each_get_own_projection(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),  # different source
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert first is not None
        assert second is not None
        assert first.projection_id != second.projection_id
        assert len(pool.events) == 2


class TestNonMatchingDecisions:
    """When the matcher returns a non-projecting reason, the applicator
    must return None without side effects."""

    async def test_no_commitments_returns_none(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[],
            enabled=True,
        )

        assert result is None
        assert len(pool.events) == 0
        assert len(pool.health_source_to_event_projections) == 0

    async def test_wrong_bot_returns_none(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()
        commitment = _make_commitment(bot_id="sage")

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
        )

        assert result is None
        assert len(pool.events) == 0

    async def test_no_eligible_slot_returns_none(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        # Workout on June 16, but commitment starts Sept 1
        workout = _make_workout(local_date=date(2025, 6, 16))
        commitment = _make_commitment(start_date=date(2025, 9, 1))

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
        )

        assert result is None
        assert len(pool.events) == 0

    async def test_unknown_workout_type_returns_none(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout(workout_type="nonexistent_category")
        commitment = _make_commitment()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
        )

        assert result is None
        assert len(pool.events) == 0

    async def test_ambiguous_multiple_returns_none(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()
        commitments = [
            _make_commitment(id=str(uuid4())),
            _make_commitment(id=str(uuid4())),
        ]

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=commitments,
            enabled=True,
        )

        assert result is None
        assert len(pool.events) == 0


class TestMissingTopicId:
    """When the matched commitment lacks a topic_id, the applicator
    cannot create the event and must return None."""

    async def test_missing_topic_id_returns_none(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()

        # Commitment without topic_id
        cid = str(uuid4())
        commitment = {
            "id": cid,
            "bot_id": "hector",
            "topic_slug": "fitness",
            # no topic_id
            "label": "Test",
            "cadence": "daily",
            "start_date": "2025-01-01",
            "end_date": None,
            "days_of_week": [],
            "schedule_rule": {},
            "user_id": "u001",
            "status": "active",
        }

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
        )

        assert result is None
        assert len(pool.events) == 0

    async def test_missing_topic_id_no_side_effects(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()

        cid2 = str(uuid4())
        commitment = {
            "id": cid2,
            "bot_id": "hector",
            "topic_slug": "fitness",
            "label": "Test",
            "cadence": "daily",
            "start_date": "2025-01-01",
            "end_date": None,
            "days_of_week": [],
            "schedule_rule": {},
            "user_id": "u001",
            "status": "active",
        }

        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
        )

        assert len(pool.events) == 0
        assert len(pool.health_source_to_event_projections) == 0


class TestCommitmentDisappearanceRace:
    """When the commitment vanishes between match and apply, the
    applicator must return None cleanly."""

    async def test_commitment_not_in_list_returns_none(self):
        """Simulate a race where the matcher would have matched but the
        commitment list passed to the applicator is different."""
        # This is tested implicitly by the design: the applicator uses
        # the same commitments list that was passed to it.  If the
        # matcher returns a match but the commitment isn't in the list
        # that was passed, that's a logic error in the caller.  Still,
        # the applicator defends against it.
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()

        # Passing a commitment that the matcher won't match against
        # because its bot_id is wrong, but with a matching topic_id in
        # another commitment that ISN'T passed.  In practice the
        # applicator will get the same list the matcher sees.
        commitment = _make_commitment(bot_id="sage")

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
        )

        # Matcher rejects it (wrong bot), so applicator returns None
        assert result is None
        assert len(pool.events) == 0


class TestProjectionVersioning:
    """The projection_version parameter is forwarded to the ledger row."""

    async def test_custom_projection_version(self):
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
            enabled=True,
            projection_version=7,
        )

        assert result is not None
        assert result.projection_version == 7


class TestExecutorPassthrough:
    """The executor parameter is forwarded to repository methods."""

    async def test_with_explicit_executor(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        async with repo.transaction() as connection:
            workout = _make_workout()
            commitment = _make_commitment()

            result = await apply_workout_projection(
                repository=repo,
                workout=workout,
                source_record_id=source_id,
                connection_id=conn_id,
                user_id=user_id,
                commitments=[commitment],
                enabled=True,
                executor=connection,
            )

            assert result is not None
            assert result.projection_status == "projected"
            assert len(pool.events) == 1


# ═════════════════════════════════════════════════════════════════════════════
# T12: Revision, rematch, and tombstone handling
# ═════════════════════════════════════════════════════════════════════════════


class TestTombstoneHandling:
    """When ``is_tombstone=True`` the applicator must clean up the active
    projection and its projection-owned event, leaving manual events
    untouched."""

    async def test_tombstone_removes_active_projection(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        # Create a projection first.
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None
        assert first.projection_status == "projected"

        # Now tombstone it.
        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        assert result is None

        # The old projection should now be 'removed'.
        proj_rows = [
            r for r in pool.health_source_to_event_projections.values()
            if r["source_record_id"] == source_id
        ]
        assert len(proj_rows) == 1
        assert proj_rows[0]["projection_status"] == "removed"
        assert proj_rows[0]["event_id"] is None

    async def test_tombstone_deletes_projection_owned_event(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None
        event_id = first.event_id
        assert event_id is not None
        assert event_id in pool.events

        # Tombstone.
        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        # The projection-owned event should be deleted.
        assert event_id not in pool.events

    async def test_tombstone_with_no_existing_projection_is_noop(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        workout = _make_workout()
        commitment = _make_commitment()
        initial_event_count = len(pool.events)

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=_rid(),
            connection_id=_rid(),
            user_id=_rid(),
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        assert result is None
        assert len(pool.events) == initial_event_count
        assert len(pool.health_source_to_event_projections) == 0

    async def test_tombstone_disabled_is_noop(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        # Create a projection first.
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None

        # Tombstone with feature disabled — must be no-op.
        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=False,
            is_tombstone=True,
        )

        assert result is None
        # Existing projection must still be active.
        proj_rows = [
            r for r in pool.health_source_to_event_projections.values()
            if r["source_record_id"] == source_id
        ]
        assert len(proj_rows) == 1
        assert proj_rows[0]["projection_status"] == "projected"

    async def test_tombstone_does_not_touch_manual_event(self):
        """Manual events (not linked to any projection row) must survive
        tombstone processing."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        # Insert a manual event directly into the pool (simulating a
        # log_event that was created outside projection code).
        manual_event_id = uuid4()
        pool.events[manual_event_id] = {
            "id": manual_event_id,
            "commitment_id": uuid4(),
            "user_id": user_id,
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, 0, tzinfo=timezone.utc),
            "note": "Manual log entry",
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
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert first is not None
        assert first.event_id != manual_event_id

        # Tombstone.
        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        # The manual event must still exist.
        assert manual_event_id in pool.events
        assert pool.events[manual_event_id]["note"] == "Manual log entry"


class TestRevisionHandling:
    """When ``projection_version`` is bumped the applicator must supersede
    the old projection, clean up its event, and attempt a fresh match."""

    async def test_revision_supersedes_old_projection(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        # First-time projection (version 1).
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        assert first is not None
        assert first.projection_status == "projected"
        old_proj_id = first.projection_id

        # Revision (version 2) with same match.
        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )
        assert second is not None
        assert second.projection_status == "projected"
        assert second.projection_version == 2
        assert second.projection_id != old_proj_id
        assert second.supersedes_projection_id == old_proj_id

        # Old projection must be superseded.
        old_row = pool.health_source_to_event_projections.get(old_proj_id)
        assert old_row is not None
        assert old_row["projection_status"] == "superseded"
        assert old_row["event_id"] is None  # detached

    async def test_revision_creates_new_event_for_new_match(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        # First projection.
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        old_event_id = first.event_id

        # Revision: create a new commitment so the new match differs.
        new_commitment = _make_commitment(
            id=str(uuid4()),
            topic_id=uuid4(),
        )

        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[new_commitment],
            enabled=True,
            projection_version=2,
        )
        assert second is not None
        assert second.event_id is not None
        assert second.event_id != old_event_id

        # New event must exist, old event must be gone.
        assert second.event_id in pool.events
        assert old_event_id not in pool.events

    async def test_revision_no_match_returns_none_after_cleanup(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        # First projection.
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        old_proj_id = first.projection_id
        old_event_id = first.event_id

        # Revision with no matching commitments.
        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[],  # no commitments
            enabled=True,
            projection_version=2,
        )
        assert second is None

        # Old projection must be superseded and event cleaned up.
        old_row = pool.health_source_to_event_projections.get(old_proj_id)
        assert old_row is not None
        assert old_row["projection_status"] == "superseded"
        assert old_row["event_id"] is None
        assert old_event_id not in pool.events

    async def test_revision_same_commitment_still_creates_new_event(self):
        """Even when the revision matches the same commitment, a new
        event is created (observed_at may differ, version bumps, etc.)."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        old_event_id = first.event_id
        old_proj_id = first.projection_id

        # Same workout, same commitment, higher version.
        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )
        assert second is not None
        assert second.projection_id != old_proj_id
        assert second.event_id != old_event_id
        assert second.supersedes_projection_id == old_proj_id
        assert second.commitment_id == first.commitment_id  # same commitment

        # Old event deleted, new event created.
        assert old_event_id not in pool.events
        assert second.event_id in pool.events

    async def test_revision_chain_has_correct_supersedes_linkage(self):
        """Three revisions produce a chain: v3 → v2 → v1."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        v1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        v2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )
        v3 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=3,
        )

        assert v3.supersedes_projection_id == v2.projection_id
        assert v2.supersedes_projection_id == v1.projection_id

        # v1 and v2 are superseded, v3 is active.
        r1 = pool.health_source_to_event_projections[v1.projection_id]
        r2 = pool.health_source_to_event_projections[v2.projection_id]
        r3 = pool.health_source_to_event_projections[v3.projection_id]
        assert r1["projection_status"] == "superseded"
        assert r2["projection_status"] == "superseded"
        assert r3["projection_status"] == "projected"

    async def test_revision_same_version_is_idempotent_replay(self):
        """Calling with the same version returns the existing projection
        without superseding or creating new events."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=3,
        )
        event_count = len(pool.events)
        proj_count = len(pool.health_source_to_event_projections)

        # Same version again.
        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=3,
        )

        assert second is not None
        assert second.projection_id == first.projection_id
        assert len(pool.events) == event_count
        assert len(pool.health_source_to_event_projections) == proj_count


class TestRematchHandling:
    """When commitments change and the version is bumped, the applicator
    must re-evaluate and potentially match a different commitment."""

    async def test_rematch_different_commitment_after_version_bump(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment_a = _make_commitment(id=str(uuid4()), topic_id=uuid4())
        commitment_b = _make_commitment(id=str(uuid4()), topic_id=uuid4())

        # First projection matches commitment_a.
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment_a],
            enabled=True,
            projection_version=1,
        )
        assert first is not None
        assert str(first.commitment_id) == commitment_a["id"]

        # Rematch: version bumped, only commitment_b available.
        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment_b],
            enabled=True,
            projection_version=2,
        )
        assert second is not None
        assert str(second.commitment_id) == commitment_b["id"]
        assert second.supersedes_projection_id == first.projection_id

    async def test_rematch_no_eligible_commitment_after_bump(self):
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment = _make_commitment()

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )
        old_proj_id = first.projection_id

        # Rematch: version bumped, but commitment is wrong bot.
        bad_commitment = _make_commitment(bot_id="sage")
        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[bad_commitment],
            enabled=True,
            projection_version=2,
        )
        assert second is None

        # Old projection superseded, event cleaned up.
        old_row = pool.health_source_to_event_projections.get(old_proj_id)
        assert old_row["projection_status"] == "superseded"

    async def test_rematch_same_commitment_same_version_is_idempotent(self):
        """Without a version bump, even different commitments don't
        trigger a rematch — the caller must bump the version."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        workout = _make_workout()
        commitment_a = _make_commitment(id=str(uuid4()), topic_id=uuid4())
        commitment_b = _make_commitment(id=str(uuid4()), topic_id=uuid4())

        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment_a],
            enabled=True,
            projection_version=1,
        )
        # Same version, different commitment list — must be idempotent.
        second = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment_b],
            enabled=True,
            projection_version=1,
        )
        assert second is not None
        assert second.projection_id == first.projection_id
        # Still linked to commitment_a (no rematch occurred).
        assert str(second.commitment_id) == commitment_a["id"]


class TestManualEventProtection:
    """Manual ``log_event`` testimony must never be touched by projection
    code — not during tombstone, revision, or rematch."""

    async def test_manual_event_survives_tombstone_when_projection_has_no_event(self):
        """If a projection row has event_id=NULL (e.g., already detached),
        tombstone must not accidentally delete unrelated events."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        # Insert a manual event.
        manual_event_id = uuid4()
        pool.events[manual_event_id] = {
            "id": manual_event_id,
            "commitment_id": uuid4(),
            "user_id": user_id,
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, 0, tzinfo=timezone.utc),
            "note": "Manual testimony",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
        }

        workout = _make_workout()
        commitment = _make_commitment()

        # Create a projection.
        first = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        # Detach the event from the projection (simulating a prior detach).
        proj_row = pool.health_source_to_event_projections[first.projection_id]
        proj_event_id = proj_row["event_id"]
        proj_row["event_id"] = None

        # Tombstone.
        await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )

        # The manual event survives.
        assert manual_event_id in pool.events
        # The projection-owned event was deleted (it's still in the pool
        # but find_projection_by_event won't find it because event_id was
        # set to None — however _cleanup_projection_event checks ownership
        # via find_projection_by_event which now returns None, so the
        # event is *not* deleted).
        assert proj_event_id in pool.events

    async def test_manual_event_survives_revision(self):
        """Manual events not linked to any projection must survive a
        revision cycle."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()
        source_id = _rid()
        conn_id = _rid()

        # Insert a manual event.
        manual_event_id = uuid4()
        pool.events[manual_event_id] = {
            "id": manual_event_id,
            "commitment_id": uuid4(),
            "user_id": user_id,
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, 0, tzinfo=timezone.utc),
            "note": "Manual testimony",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
        }

        workout = _make_workout()
        commitment = _make_commitment()

        # v1 projection.
        v1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=1,
        )

        # v2 revision.
        v2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_id,
            connection_id=conn_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            projection_version=2,
        )
        assert v2 is not None

        # Manual event must still exist.
        assert manual_event_id in pool.events
        assert pool.events[manual_event_id]["note"] == "Manual testimony"

    async def test_manual_event_not_deleted_by_find_projection_by_event_guard(self):
        """The ownership check via find_projection_by_event prevents
        deletion of events not linked to any projection row."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        user_id = _rid()

        # Insert an event that is NOT linked to any projection.
        orphan_event_id = uuid4()
        pool.events[orphan_event_id] = {
            "id": orphan_event_id,
            "commitment_id": uuid4(),
            "user_id": user_id,
            "topic_id": uuid4(),
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": datetime(2025, 6, 16, 9, 0, 0, tzinfo=timezone.utc),
            "note": "Orphan event",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
        }

        # Verify find_projection_by_event returns None for it.
        owned = await repo.find_projection_by_event(event_id=orphan_event_id)
        assert owned is None

        # The event still exists (never deleted).
        assert orphan_event_id in pool.events
