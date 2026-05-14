"""Test pregnancy write tools: set_pregnancy_edd, correct_pregnancy_edd, end_pregnancy.

Covers:
- Happy paths for all three tools
- Precondition errors (already-active, no-active, invalid enum, EDD >1y out)
- Double-end-pregnancy error message
- ctx.user.pregnancy_edd is set after set_pregnancy_edd (mid-turn refresh)
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from uuid import uuid4

import pytest

from tests.conftest import FakePool
from app.models.user import User
from app.services.turn_context import TurnContext
from app.services.tools.write_tools import (
    ToolCallRejected,
    set_pregnancy_edd,
    correct_pregnancy_edd,
    end_pregnancy,
)

# Re-import schemas for test usage
from tool_schemas import (
    SetPregnancyEddInput,
    CorrectPregnancyEddInput,
    EndPregnancyInput,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_pool() -> FakePool:
    return FakePool()


def _make_user(**overrides) -> User:
    return User(
        id=overrides.get("id", uuid4()),
        name=overrides.get("name", "TestUser"),
        phone=overrides.get("phone", "15555550100"),
        timezone=overrides.get("timezone", "UTC"),
        pregnancy_edd=overrides.get("pregnancy_edd"),
        pregnancy_dating_basis=overrides.get("pregnancy_dating_basis"),
        pregnancy_lmp_date=overrides.get("pregnancy_lmp_date"),
        pregnancy_scan_date=overrides.get("pregnancy_scan_date"),
        pregnancy_scan_corrected_at=overrides.get("pregnancy_scan_corrected_at"),
        pregnancy_started_at=overrides.get("pregnancy_started_at"),
        pregnancy_ended_at=overrides.get("pregnancy_ended_at"),
        pregnancy_outcome=overrides.get("pregnancy_outcome"),
    )


def _make_ctx(pool: FakePool, user: User | None = None) -> TurnContext:
    if user is None:
        user = _make_user()
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
        "pacing_preferences": {},
        "pregnancy_edd": user.pregnancy_edd,
        "pregnancy_dating_basis": user.pregnancy_dating_basis,
        "pregnancy_lmp_date": user.pregnancy_lmp_date,
        "pregnancy_scan_date": user.pregnancy_scan_date,
        "pregnancy_scan_corrected_at": user.pregnancy_scan_corrected_at,
        "pregnancy_started_at": user.pregnancy_started_at,
        "pregnancy_ended_at": user.pregnancy_ended_at,
        "pregnancy_outcome": user.pregnancy_outcome,
    }
    return TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="tante_rosi",
        primary_topic_id=uuid4(),
        primary_topic_slug="pregnancy",
    )


# ---------------------------------------------------------------------------
# set_pregnancy_edd — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_pregnancy_edd_happy_path():
    """set_pregnancy_edd stores the EDD, dating_basis, and provenance dates."""
    pool = _fresh_pool()
    user = _make_user()
    ctx = _make_ctx(pool, user)

    args = SetPregnancyEddInput(
        edd="2026-10-22",
        dating_basis="lmp",
        lmp_date="2026-01-15",
        started_at="2026-01-15T10:30:00",
    )
    result = await set_pregnancy_edd(ctx, args)

    assert result.ok is True
    assert result.edd == "2026-10-22"
    assert result.gestational_age  # e.g. "16w5d"

    # Mid-turn refresh: ctx.user should now carry pregnancy data
    assert ctx.user.pregnancy_edd == date(2026, 10, 22)
    assert ctx.user.pregnancy_dating_basis == "lmp"
    assert ctx.user.pregnancy_lmp_date == date(2026, 1, 15)
    assert ctx.user.pregnancy_started_at is not None

    # Pool should also reflect the update
    stored = pool.users[user.id]
    assert stored["pregnancy_edd"] == date(2026, 10, 22)
    assert stored["pregnancy_dating_basis"] == "lmp"


@pytest.mark.asyncio
async def test_set_pregnancy_edd_defaults_started_at():
    """When started_at is omitted, it defaults to now()."""
    pool = _fresh_pool()
    user = _make_user()
    ctx = _make_ctx(pool, user)

    args = SetPregnancyEddInput(edd="2026-10-22", dating_basis="lmp")
    result = await set_pregnancy_edd(ctx, args)

    assert result.ok is True
    assert ctx.user.pregnancy_started_at is not None


@pytest.mark.asyncio
async def test_set_pregnancy_edd_with_scan_basis():
    """dating_basis='scan' with scan_date works."""
    pool = _fresh_pool()
    user = _make_user()
    ctx = _make_ctx(pool, user)

    args = SetPregnancyEddInput(
        edd="2026-11-01",
        dating_basis="scan",
        scan_date="2026-03-15",
    )
    result = await set_pregnancy_edd(ctx, args)

    assert result.ok is True
    assert ctx.user.pregnancy_dating_basis == "scan"
    assert ctx.user.pregnancy_scan_date == date(2026, 3, 15)


# ---------------------------------------------------------------------------
# set_pregnancy_edd — precondition errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_pregnancy_edd_rejects_already_active():
    """Calling set_pregnancy_edd when pregnancy is already active raises an error."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 8, 1),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    ctx = _make_ctx(pool, user)

    args = SetPregnancyEddInput(edd="2026-10-22", dating_basis="lmp")

    with pytest.raises(ToolCallRejected) as exc_info:
        await set_pregnancy_edd(ctx, args)

    assert exc_info.value.result["error"] == "pregnancy_already_active"


@pytest.mark.asyncio
async def test_set_pregnancy_edd_allows_after_ended():
    """After a pregnancy has ended, set_pregnancy_edd for a new pregnancy should work."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 3, 1),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2025, 7, 1, tzinfo=timezone.utc),
        pregnancy_ended_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
        pregnancy_outcome="birth",
    )
    ctx = _make_ctx(pool, user)

    args = SetPregnancyEddInput(edd="2026-12-01", dating_basis="lmp")
    result = await set_pregnancy_edd(ctx, args)

    assert result.ok is True
    assert ctx.user.pregnancy_edd == date(2026, 12, 1)


@pytest.mark.asyncio
async def test_set_pregnancy_edd_rejects_edd_too_far_future():
    """EDD more than 1 year in the future raises an error."""
    pool = _fresh_pool()
    user = _make_user()
    ctx = _make_ctx(pool, user)

    far_future = (date.today().replace(year=date.today().year + 2)).isoformat()
    args = SetPregnancyEddInput(edd=far_future, dating_basis="lmp")

    with pytest.raises(ToolCallRejected) as exc_info:
        await set_pregnancy_edd(ctx, args)

    assert exc_info.value.result["error"] == "edd_too_far_future"


# ---------------------------------------------------------------------------
# correct_pregnancy_edd — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correct_pregnancy_edd_happy_path():
    """correct_pregnancy_edd revises the EDD for an active pregnancy."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 10, 1),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    ctx = _make_ctx(pool, user)

    args = CorrectPregnancyEddInput(
        edd="2026-10-22", dating_basis="scan", scan_date="2026-04-15"
    )
    result = await correct_pregnancy_edd(ctx, args)

    assert result.ok is True
    assert result.edd == "2026-10-22"
    assert ctx.user.pregnancy_edd == date(2026, 10, 22)
    assert ctx.user.pregnancy_dating_basis == "scan"
    assert ctx.user.pregnancy_scan_date == date(2026, 4, 15)
    assert ctx.user.pregnancy_scan_corrected_at is not None  # flip to scan recorded


@pytest.mark.asyncio
async def test_correct_pregnancy_edd_no_scan_date():
    """correct_pregnancy_edd with lmp basis does not record scan_corrected_at."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 10, 1),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    ctx = _make_ctx(pool, user)

    args = CorrectPregnancyEddInput(edd="2026-10-15", dating_basis="lmp")
    result = await correct_pregnancy_edd(ctx, args)

    assert result.ok is True
    assert (
        ctx.user.pregnancy_scan_corrected_at is None
    )  # lmp → lmp: no correction timestamp


# ---------------------------------------------------------------------------
# correct_pregnancy_edd — precondition errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_correct_pregnancy_edd_rejects_no_active():
    """correct_pregnancy_edd when no pregnancy exists raises an error."""
    pool = _fresh_pool()
    user = _make_user()
    ctx = _make_ctx(pool, user)

    args = CorrectPregnancyEddInput(edd="2026-10-22", dating_basis="lmp")

    with pytest.raises(ToolCallRejected) as exc_info:
        await correct_pregnancy_edd(ctx, args)

    assert exc_info.value.result["error"] == "no_active_pregnancy"


@pytest.mark.asyncio
async def test_correct_pregnancy_edd_rejects_ended():
    """correct_pregnancy_edd when pregnancy has ended raises an error."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 3, 1),
        pregnancy_dating_basis="lmp",
        pregnancy_ended_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
        pregnancy_outcome="birth",
    )
    ctx = _make_ctx(pool, user)

    args = CorrectPregnancyEddInput(edd="2026-10-22", dating_basis="lmp")

    with pytest.raises(ToolCallRejected) as exc_info:
        await correct_pregnancy_edd(ctx, args)

    assert exc_info.value.result["error"] == "no_active_pregnancy"


# ---------------------------------------------------------------------------
# end_pregnancy — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_pregnancy_happy_path():
    """end_pregnancy records outcome and ended_at."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 10, 22),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    ctx = _make_ctx(pool, user)

    args = EndPregnancyInput(outcome="birth", ended_at="2026-10-20T14:00:00")
    result = await end_pregnancy(ctx, args)

    assert result.ok is True
    assert result.outcome == "birth"
    assert result.ended_at == "2026-10-20T14:00:00"
    assert ctx.user.pregnancy_ended_at == datetime(2026, 10, 20, 14, 0, 0)
    assert ctx.user.pregnancy_outcome == "birth"


@pytest.mark.asyncio
async def test_end_pregnancy_defaults_ended_at():
    """When ended_at is omitted, it defaults to now()."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 10, 22),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    ctx = _make_ctx(pool, user)

    args = EndPregnancyInput(outcome="loss")
    result = await end_pregnancy(ctx, args)

    assert result.ok is True
    assert ctx.user.pregnancy_ended_at is not None
    assert ctx.user.pregnancy_outcome == "loss"


@pytest.mark.asyncio
async def test_end_pregnancy_with_termination():
    """end_pregnancy supports outcome='termination'."""
    pool = _fresh_pool()
    user = _make_user(
        pregnancy_edd=date(2026, 10, 22),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
    )
    ctx = _make_ctx(pool, user)

    args = EndPregnancyInput(outcome="termination")
    result = await end_pregnancy(ctx, args)

    assert result.ok is True
    assert result.outcome == "termination"


# ---------------------------------------------------------------------------
# end_pregnancy — precondition errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_pregnancy_rejects_no_edd():
    """end_pregnancy when EDD has never been set raises an error."""
    pool = _fresh_pool()
    user = _make_user()
    ctx = _make_ctx(pool, user)

    args = EndPregnancyInput(outcome="birth")

    with pytest.raises(ToolCallRejected) as exc_info:
        await end_pregnancy(ctx, args)

    assert exc_info.value.result["error"] == "no_active_pregnancy"


@pytest.mark.asyncio
async def test_end_pregnancy_rejects_already_ended():
    """Calling end_pregnancy twice raises 'pregnancy already ended on <date>'."""
    pool = _fresh_pool()
    ended_at = datetime(2026, 3, 5, 14, 0, 0, tzinfo=timezone.utc)
    user = _make_user(
        pregnancy_edd=date(2026, 3, 1),
        pregnancy_dating_basis="lmp",
        pregnancy_started_at=datetime(2025, 7, 1, tzinfo=timezone.utc),
        pregnancy_ended_at=ended_at,
        pregnancy_outcome="birth",
    )
    ctx = _make_ctx(pool, user)

    args = EndPregnancyInput(outcome="birth")

    with pytest.raises(ToolCallRejected) as exc_info:
        await end_pregnancy(ctx, args)

    assert exc_info.value.result["error"] == "pregnancy_already_ended"
    assert "already ended on" in exc_info.value.result["reason"]
    assert ended_at.isoformat() in exc_info.value.result["reason"]


# ---------------------------------------------------------------------------
# Invalid enum values (tests that Pydantic validation blocks bad inputs)
# ---------------------------------------------------------------------------


def test_set_pregnancy_edd_rejects_invalid_dating_basis():
    """Pydantic should reject invalid dating_basis at the schema level."""
    with pytest.raises(Exception):
        SetPregnancyEddInput(edd="2026-10-22", dating_basis="ultrasound")  # type: ignore[arg-type]


def test_end_pregnancy_rejects_invalid_outcome():
    """Pydantic should reject invalid outcome at the schema level."""
    with pytest.raises(Exception):
        EndPregnancyInput(outcome="miscarriage")  # type: ignore[arg-type]
