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

from datetime import UTC, date, datetime, timedelta
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
