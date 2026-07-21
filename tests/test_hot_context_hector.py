"""Integration tests for Hector fitness block in solo hot context.

T15 (SC15):
- Seed a user with active commitments and events in FakePool.
- Call build_hot_context_solo with bot_id='hector'.
- Verify fitness_block is populated with current focus, active commitments,
  per-day status, and recent events.
- Verify ## Fitness section renders correctly via render_hot_context_solo.
- Assert ## Fitness block is absent for coach/mediator/tante_rosi.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.hot_context_solo import (
    build_hot_context_solo,
    render_hot_context_solo,
)


# ── fixed topic IDs ──────────────────────────────────────────────────

_FITNESS_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000010")
_RELATIONSHIP_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000001")


def _make_user(
    *,
    user_id: str | None = None,
    name: str = "Alex",
    timezone: str = "America/New_York",
) -> User:
    uid = user_id or str(uuid4())
    return User(
        id=uid,
        name=name,
        phone="+155****0100",
        timezone=timezone,
        onboarding_state="completed",
    )


def _seed_commitment(
    fake_pool,
    *,
    cid: UUID | None = None,
    user_id: UUID,
    topic_id: UUID = _FITNESS_TOPIC_ID,
    bot_id: str = "hector",
    label: str = "Morning workout",
    cadence: str = "weekdays",
    days_of_week: list[int] | None = None,
    target_count: int | None = None,
    pressure_style: str = "low_key",
    status: str = "active",
) -> dict:
    """Insert a row into fake_pool.commitments and return the row dict."""
    row_id = cid or uuid4()
    row = {
        "id": row_id,
        "user_id": user_id,
        "topic_id": topic_id,
        "bot_id": bot_id,
        "label": label,
        "kind": "workout",
        "status": status,
        "cadence": cadence,
        "days_of_week": days_of_week,
        "target_count": target_count,
        "start_date": None,
        "end_date": None,
        "schedule_rule": None,
        "pressure_style": pressure_style,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fake_pool.commitments[row_id] = row
    return row


def _seed_event(
    fake_pool,
    *,
    commitment_id: UUID | None = None,
    user_id: UUID,
    topic_id: UUID = _FITNESS_TOPIC_ID,
    bot_id: str = "hector",
    metric_key: str = "workout",
    adherence_status: str | None = None,
    value_numeric: float | None = None,
    value_text: str | None = None,
    note: str | None = None,
    observed_at: datetime | None = None,
) -> dict:
    """Insert a row into fake_pool.events and return the row dict."""
    row_id = uuid4()
    row = {
        "id": row_id,
        "commitment_id": commitment_id,
        "user_id": user_id,
        "topic_id": topic_id,
        "bot_id": bot_id,
        "metric_key": metric_key,
        "adherence_status": adherence_status,
        "value_numeric": value_numeric,
        "value_text": value_text,
        "unit": None,
        "observed_at": observed_at or datetime.now(UTC),
        "note": note,
        "source_message_ids": [],
        "created_at": datetime.now(UTC),
    }
    fake_pool.events[row_id] = row
    return row


# ── integration tests ────────────────────────────────────────────────


class TestHectorHotContextBuild:
    """Call build_hot_context_solo with seeded commitments + events."""

    @pytest.mark.asyncio
    async def test_build_with_active_commitments_populates_fitness_block(
        self, fake_pool, app_env
    ):
        """Seed two active commitments and a few events; fitness_block is populated."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        # Seed two active commitments
        c1 = _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="weekdays",
            pressure_style="low_key",
        )
        c2 = _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Protein tracking",
            cadence="daily",
            pressure_style="very_gentle",
        )

        # Seed a few events for this week
        today = datetime.now(UTC)
        mon = today - timedelta(days=today.weekday())  # most recent Monday
        _seed_event(
            fake_pool,
            commitment_id=c1["id"],
            user_id=uid,
            metric_key="workout",
            adherence_status="done",
            observed_at=mon + timedelta(hours=7),
            note="Lifted heavy",
        )
        _seed_event(
            fake_pool,
            commitment_id=c1["id"],
            user_id=uid,
            metric_key="workout",
            adherence_status="missed",
            observed_at=mon + timedelta(days=1, hours=7),
            note="Slept in",
        )
        _seed_event(
            fake_pool,
            commitment_id=c2["id"],
            user_id=uid,
            metric_key="nutrition",
            adherence_status="done",
            observed_at=mon + timedelta(hours=9),
            note="Hit macros",
        )

        # Seed topics so primary_topic_id_for works
        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.fitness_block is not None, (
            "fitness_block should be populated when active commitments exist"
        )
        assert "Current focus:" in hc.fitness_block
        assert "Active commitments:" in hc.fitness_block
        assert "Morning workout" in hc.fitness_block
        assert "Protein tracking" in hc.fitness_block
        assert "This week:" in hc.fitness_block

    @pytest.mark.asyncio
    async def test_fitness_block_renders_in_full_output(self, fake_pool, app_env):
        """render_hot_context_solo includes ## Fitness when fitness_block is set."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="weekdays",
        )

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        rendered = render_hot_context_solo(hc)
        assert "## Fitness" in rendered, (
            "## Fitness section should appear in rendered output"
        )
        assert "Current focus:" in rendered
        assert "Active commitments:" in rendered
        assert "This week:" in rendered

    @pytest.mark.asyncio
    async def test_no_fitness_block_when_no_active_commitments(
        self, fake_pool, app_env
    ):
        """fitness_block is None when there are no active commitments."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.fitness_block is None, (
            "fitness_block should be None when no active commitments exist"
        )

    @pytest.mark.asyncio
    async def test_recent_events_appear_in_fitness_block(
        self, fake_pool, app_env
    ):
        """Recent events (last 14 days) appear in the fitness block."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        c1 = _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="weekdays",
        )

        today = datetime.now(UTC)
        mon = today - timedelta(days=today.weekday())
        _seed_event(
            fake_pool,
            commitment_id=c1["id"],
            user_id=uid,
            metric_key="workout",
            adherence_status="done",
            observed_at=mon + timedelta(hours=7),
            note="Lifted heavy",
        )

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.fitness_block is not None
        assert "Recent events:" in hc.fitness_block


class TestFitnessBlockAbsentForOtherBots:
    """## Fitness block must NOT appear for coach, mediator, or tante_rosi."""

    @pytest.mark.asyncio
    async def test_coach_hot_context_no_fitness_block(
        self, fake_pool, app_env
    ):
        """Coach turns never include ## Fitness."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        # Seed fitness commitments (should NOT be read by coach)
        _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="weekdays",
        )

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="coach",
        )

        assert hc.fitness_block is None, (
            "Coach should never have a fitness_block"
        )
        rendered = render_hot_context_solo(hc)
        assert "## Fitness" not in rendered

    @pytest.mark.asyncio
    async def test_mediator_hot_context_no_fitness_block(
        self, fake_pool, app_env
    ):
        """Mediator turns never include ## Fitness."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="weekdays",
        )

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="mediator",
        )

        assert hc.fitness_block is None, (
            "Mediator should never have a fitness_block"
        )
        rendered = render_hot_context_solo(hc)
        assert "## Fitness" not in rendered

    @pytest.mark.asyncio
    async def test_tante_rosi_hot_context_no_fitness_block(
        self, fake_pool, app_env
    ):
        """Tante Rosi turns never include ## Fitness."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="weekdays",
        )

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="tante_rosi",
        )

        assert hc.fitness_block is None, (
            "Tante Rosi should never have a fitness_block"
        )
        rendered = render_hot_context_solo(hc)
        assert "## Fitness" not in rendered


class TestFitnessBlockContentDetail:
    """Detailed checks on fitness block content."""

    @pytest.mark.asyncio
    async def test_pressure_styles_appear(self, fake_pool, app_env):
        """Commitment pressure_style is included in the fitness block."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="weekdays",
            pressure_style="firm",
        )

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.fitness_block is not None
        assert "(pressure=firm)" in hc.fitness_block

    @pytest.mark.asyncio
    async def test_per_day_status_in_this_week(self, fake_pool, app_env):
        """This week section includes per-day adherence status."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        c1 = _seed_commitment(
            fake_pool,
            user_id=uid,
            label="Morning workout",
            cadence="daily",
        )

        today = datetime.now(UTC)
        mon = today - timedelta(days=today.weekday())
        _seed_event(
            fake_pool,
            commitment_id=c1["id"],
            user_id=uid,
            adherence_status="done",
            observed_at=mon + timedelta(hours=7),
        )
        _seed_event(
            fake_pool,
            commitment_id=c1["id"],
            user_id=uid,
            adherence_status="missed",
            observed_at=mon + timedelta(days=1, hours=7),
        )

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.fitness_block is not None
        # daily cadence should show per-day status
        assert "This week:" in hc.fitness_block
        assert "Mon" in hc.fitness_block
        assert "done" in hc.fitness_block


# ── workout block helpers ────────────────────────────────────────────

def _seed_withings_connection(
    fake_pool,
    *,
    user_id: UUID,
    connection_id: UUID | None = None,
) -> UUID:
    """Seed a Withings health connection and return its id."""
    cid = connection_id or uuid4()
    fake_pool.health_connections[cid] = {
        "id": cid,
        "user_id": user_id,
        "provider": "withings",
        "external_user_id": "withings-user-12345",
        "status": "active",
        "granted_scopes": ["workouts", "sleep", "weight"],
        "granted_at": datetime.now(UTC),
        "consented_measurements_at": datetime.now(UTC),
        "consented_workouts_at": datetime.now(UTC),
        "consented_sleep_at": datetime.now(UTC),
        "access_token_encrypted": b"enc-token",
        "refresh_token_encrypted": b"enc-refresh",
        "access_token_expires_at": datetime.now(UTC) + timedelta(hours=1),
        "refresh_token_expires_at": datetime.now(UTC) + timedelta(days=90),
        "refresh_token_rotated_at": None,
        "last_success_at": datetime.now(UTC),
        "last_error_at": None,
        "last_error_code": None,
        "cursor_state": {},
        "disconnected_at": None,
        "revoked_at": None,
        "deleted_at": None,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    fake_pool.health_connections_by_user_provider[(user_id, "withings")] = cid
    return cid


def _seed_normalized_workout(
    fake_pool,
    *,
    user_id: UUID,
    connection_id: UUID,
    source_record_id: UUID | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    workout_type: str = "running",
    duration_seconds: int = 1800,
    distance_meters: float | None = 5000.0,
    average_heart_rate_bpm: float | None = 142.0,
    max_heart_rate_bpm: float | None = 165.0,
    source_device_id: str = "withings-scanwatch-abc123",
    source_device_model: str = "ScanWatch",
) -> UUID:
    """Seed a single normalized workout row and return its id."""
    if source_record_id is None:
        source_record_id = uuid4()
    if started_at is None:
        started_at = datetime.now(UTC) - timedelta(hours=4)
    if ended_at is None:
        ended_at = started_at + timedelta(seconds=duration_seconds) if duration_seconds else started_at + timedelta(minutes=30)

    row_id = uuid4()
    now = datetime.now(UTC)
    fake_pool.health_normalized_workouts[row_id] = {
        "id": row_id,
        "source_record_id": source_record_id,
        "connection_id": connection_id,
        "user_id": user_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "local_timezone": "America/New_York",
        "local_offset_seconds": -14400,
        "workout_type": workout_type,
        "duration_seconds": duration_seconds,
        "pause_duration_seconds": None,
        "distance_meters": distance_meters,
        "steps": None,
        "energy_kcal": 320.0,
        "elevation_gain_meters": None,
        "average_heart_rate_bpm": average_heart_rate_bpm,
        "max_heart_rate_bpm": max_heart_rate_bpm,
        "source_device_id": source_device_id,
        "source_device_model": source_device_model,
        "attribution": {},
        "created_at": now,
        "updated_at": now,
    }
    return row_id


# ── workout block integration tests ──────────────────────────────────


class TestWorkoutBlockOmission:
    """workout_block must be None when no Withings connection or no data."""

    @pytest.mark.asyncio
    async def test_no_workout_block_without_withings_connection(
        self, fake_pool, app_env
    ):
        """When the user has no Withings connection, workout_block is None."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }
        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is None, (
            "workout_block should be None without Withings connection"
        )

    @pytest.mark.asyncio
    async def test_no_workout_block_with_connection_but_no_workouts(
        self, fake_pool, app_env
    ):
        """Withings connection exists but no normalized workout rows → None."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        _seed_withings_connection(fake_pool, user_id=uid)

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None
        assert "past_24h: no_workout_data" in hc.workout_block
        assert hc.workout_block.count("no_workout_data") == 8

    @pytest.mark.asyncio
    async def test_no_workout_block_for_non_hector_with_data(
        self, fake_pool, app_env
    ):
        """Non-Hector bots never get workout_block even when workout data exists."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)
        _seed_normalized_workout(fake_pool, user_id=uid, connection_id=conn_id)

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }
        fake_pool.topics["relationship"] = {
            "id": _RELATIONSHIP_TOPIC_ID,
            "slug": "relationship",
            "display_name": "Relationship",
        }

        for bot_id in ("coach", "mediator", "tante_rosi"):
            hc = await build_hot_context_solo(
                fake_pool,
                user,
                [],
                {"kind": "test"},
                primary_topic_id=_FITNESS_TOPIC_ID,
                bot_id=bot_id,
            )
            assert hc.workout_block is None, (
                f"{bot_id} should never have a workout_block"
            )

    @pytest.mark.asyncio
    async def test_workout_block_none_not_rendered(self, fake_pool, app_env):
        """When workout_block is None, ## Fitness section does not include workout text."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        # No connection → workout_block=None
        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is None
        rendered = render_hot_context_solo(hc)
        assert "Recent workouts (7d)" not in rendered


class TestWorkoutBlockWithData:
    """workout_block contains compact summaries when workout data exists."""

    @pytest.mark.asyncio
    async def test_workout_block_populated_with_recent_workouts(
        self, fake_pool, app_env
    ):
        """When workouts exist, workout_block shows compact per-day summaries."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)

        # Seed workouts on different days
        today = datetime.now(UTC)
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)

        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=two_days_ago.replace(hour=8, minute=0),
            workout_type="running",
            duration_seconds=1800,
        )
        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=yesterday.replace(hour=7, minute=30),
            workout_type="cycling",
            duration_seconds=2700,
        )

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None, (
            "workout_block should be populated when workout data exists"
        )
        assert "Recent workouts (7d):" in hc.workout_block
        assert "count=1" in hc.workout_block
        # workout types should appear
        assert "running" in hc.workout_block
        assert "cycling" in hc.workout_block
        # duration should appear
        assert "min total" in hc.workout_block

    @pytest.mark.asyncio
    async def test_workout_block_shows_projected_count(
        self, fake_pool, app_env
    ):
        """When projections exist, the projected count is shown."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)

        source_id = uuid4()
        today = datetime.now(UTC)
        workout_start = today - timedelta(hours=4)

        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            source_record_id=source_id,
            started_at=workout_start,
            workout_type="running",
        )

        # Seed a projection for this workout
        cid = uuid4()
        _seed_commitment(fake_pool, user_id=uid, cid=cid, label="Morning workout", cadence="weekdays")

        proj_id = uuid4()
        event_id = uuid4()
        fake_pool.health_source_to_event_projections[proj_id] = {
            "id": proj_id,
            "source_record_id": source_id,
            "connection_id": conn_id,
            "user_id": uid,
            "event_id": event_id,
            "commitment_id": cid,
            "projection_version": 1,
            "projection_status": "projected",
            "decision_reason": "matched",
            "matched_local_date": workout_start.astimezone(
                timezone(timedelta(hours=-4))
            ).date(),
            "supersedes_projection_id": None,
            "created_at": datetime.now(UTC),
            "updated_at": datetime.now(UTC),
        }
        fake_pool.events[event_id] = {
            "id": event_id,
            "commitment_id": cid,
            "user_id": uid,
            "topic_id": _FITNESS_TOPIC_ID,
            "bot_id": "hector",
            "metric_key": "workout",
            "adherence_status": "done",
            "observed_at": workout_start,
            "note": "Auto-projected from Withings workout",
            "value_numeric": None,
            "value_text": None,
            "unit": None,
            "source_message_ids": [],
            "created_at": datetime.now(UTC),
        }

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None
        assert "projected" in hc.workout_block, (
            "workout_block should show projected count when projections exist"
        )

    @pytest.mark.asyncio
    async def test_workout_block_includes_in_rendered_fitness_section(
        self, fake_pool, app_env
    ):
        """The workout_block text appears inside the ## Fitness rendered section."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)
        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=datetime.now(UTC) - timedelta(hours=3),
            workout_type="swimming",
            duration_seconds=2400,
        )

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        rendered = render_hot_context_solo(hc)
        assert "## Fitness" in rendered
        assert "Recent workouts (7d):" in rendered
        assert "swimming" in rendered


class TestWorkoutBlockPrivacy:
    """workout_block must never leak raw/provider/private details."""

    @pytest.mark.asyncio
    async def test_workout_block_excludes_device_ids(self, fake_pool, app_env):
        """Device IDs must not appear in the workout block."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)
        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=datetime.now(UTC) - timedelta(hours=2),
            workout_type="running",
            source_device_id="withings-scanwatch-abc123",
            source_device_model="ScanWatch",
        )

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None
        assert "withings-scanwatch" not in hc.workout_block, (
            "Device ID must not leak into workout_block"
        )
        assert "ScanWatch" not in hc.workout_block, (
            "Device model must not leak into workout_block"
        )

    @pytest.mark.asyncio
    async def test_workout_block_excludes_heart_rate_detail(
        self, fake_pool, app_env
    ):
        """Heart-rate data must not appear in the workout block."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)
        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=datetime.now(UTC) - timedelta(hours=1),
            workout_type="running",
            average_heart_rate_bpm=142.0,
            max_heart_rate_bpm=165.0,
        )

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None
        assert "heart" not in hc.workout_block.lower(), (
            "Heart-rate detail must not leak into workout_block"
        )
        assert "bpm" not in hc.workout_block.lower(), (
            "BPM values must not leak into workout_block"
        )

    @pytest.mark.asyncio
    async def test_workout_block_excludes_raw_payload_data(
        self, fake_pool, app_env
    ):
        """No raw payloads, tokens, or attribution internals should leak."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)
        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=datetime.now(UTC) - timedelta(hours=5),
            workout_type="strength",
            duration_seconds=3600,
        )

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None
        # These must not appear
        forbidden = [
            "payload",
            "token",
            "access_token",
            "refresh_token",
            "encrypted",
            "attribution",
            "source_record_id",
        ]
        block_lower = hc.workout_block.lower()
        for term in forbidden:
            assert term not in block_lower, (
                f"'{term}' must not leak into workout_block"
            )

    @pytest.mark.asyncio
    async def test_workout_block_excludes_partner_sharing_info(
        self, fake_pool, app_env
    ):
        """No partner-sharing language should appear in workout_block."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)
        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=datetime.now(UTC) - timedelta(hours=6),
            workout_type="yoga",
            duration_seconds=2700,
        )

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None
        partner_terms = ["partner", "share", "sharing"]
        block_lower = hc.workout_block.lower()
        for term in partner_terms:
            assert term not in block_lower, (
                f"'{term}' must not appear in workout_block"
            )

    @pytest.mark.asyncio
    async def test_workout_block_does_not_imply_commitment_creation(
        self, fake_pool, app_env
    ):
        """workout_block must not contain language implying workouts create commitments."""
        user = _make_user()
        uid = user.id
        fake_pool.users[uid] = {
            "id": uid,
            "name": user.name,
            "phone": user.phone,
            "timezone": user.timezone,
            "onboarding_state": "completed",
            "style_notes": "",
            "pacing_preferences": {},
            "pregnancy_edd": None,
            "pregnancy_dating_basis": None,
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": None,
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }

        conn_id = _seed_withings_connection(fake_pool, user_id=uid)
        _seed_normalized_workout(
            fake_pool,
            user_id=uid,
            connection_id=conn_id,
            started_at=datetime.now(UTC) - timedelta(hours=3),
            workout_type="hiking",
            duration_seconds=5400,
        )

        _seed_commitment(fake_pool, user_id=uid, label="Morning workout", cadence="weekdays")

        fake_pool.topics["fitness"] = {
            "id": _FITNESS_TOPIC_ID,
            "slug": "fitness",
            "display_name": "Fitness",
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_FITNESS_TOPIC_ID,
            bot_id="hector",
        )

        assert hc.workout_block is not None
        block_lower = hc.workout_block.lower()
        # The workout_block is purely informational - must NOT contain commitment creation language
        assert "create" not in block_lower, "workout_block must not mention 'create'"
        assert "commitment" not in block_lower, "workout_block must not mention 'commitment'"
        assert "missed" not in block_lower, "workout_block must not mention 'missed'"
        assert "excused" not in block_lower, "workout_block must not mention 'excused'"
