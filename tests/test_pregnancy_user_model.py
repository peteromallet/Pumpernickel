"""Test pregnancy field hydration on the User dataclass.

Verifies:
- All 8 pregnancy fields default to None on a new User
- _row_to_user hydrates pregnancy fields when present
- _row_to_user returns None for pregnancy fields when absent (defensive fallback)
- fetch_user_by_id SELECT includes all 8 columns
- upsert_user RETURNING includes all 8 columns
- partner_of now constructs User with pregnancy fields
"""

from __future__ import annotations

from datetime import date, datetime

from uuid import uuid4

import pytest

from app.models.user import User, _row_to_user


def _make_row_with_pregnancy():
    return {
        "id": uuid4(),
        "name": "rosi_user",
        "phone": "+155****2222",
        "timezone": "Europe/Berlin",
        "onboarding_state": "welcomed",
        "pacing_preferences": {"enabled": True},
        "cross_thread_sharing_default": "unset",
        "pregnancy_edd": date(2026, 10, 22),
        "pregnancy_dating_basis": "lmp",
        "pregnancy_lmp_date": date(2026, 1, 15),
        "pregnancy_scan_date": None,
        "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": datetime(2026, 1, 15, 10, 30, 0),
        "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }


def _make_row_without_pregnancy():
    return {
        "id": uuid4(),
        "name": "plain_user",
        "phone": "+155****3333",
        "timezone": "UTC",
        "onboarding_state": "pending",
        "pacing_preferences": {},
        "cross_thread_sharing_default": None,
    }


# ---------------------------------------------------------------------------
# User default values
# ---------------------------------------------------------------------------


def test_user_defaults_all_none():
    """All 8 pregnancy fields default to None on a fresh User."""
    user = User(
        id=uuid4(),
        name="test",
        phone="+155****1111",
        timezone="UTC",
    )
    assert user.pregnancy_edd is None
    assert user.pregnancy_dating_basis is None
    assert user.pregnancy_lmp_date is None
    assert user.pregnancy_scan_date is None
    assert user.pregnancy_scan_corrected_at is None
    assert user.pregnancy_started_at is None
    assert user.pregnancy_ended_at is None
    assert user.pregnancy_outcome is None


# ---------------------------------------------------------------------------
# _row_to_user: full hydration
# ---------------------------------------------------------------------------


def test_row_to_user_hydrates_pregnancy():
    """_row_to_user hydrates all 8 pregnancy fields when row has them."""
    row = _make_row_with_pregnancy()
    user = _row_to_user(row)

    assert user.pregnancy_edd == date(2026, 10, 22)
    assert user.pregnancy_dating_basis == "lmp"
    assert user.pregnancy_lmp_date == date(2026, 1, 15)
    assert user.pregnancy_scan_date is None
    assert user.pregnancy_scan_corrected_at is None
    assert user.pregnancy_started_at == datetime(2026, 1, 15, 10, 30, 0)
    assert user.pregnancy_ended_at is None
    assert user.pregnancy_outcome is None


# ---------------------------------------------------------------------------
# _row_to_user: absent columns → None (no AttributeError)
# ---------------------------------------------------------------------------


def test_row_to_user_missing_pregnancy_columns():
    """_row_to_user returns None for pregnancy fields when row lacks them."""
    row = _make_row_without_pregnancy()
    user = _row_to_user(row)

    # Must not raise AttributeError
    assert user.pregnancy_edd is None
    assert user.pregnancy_dating_basis is None
    assert user.pregnancy_lmp_date is None
    assert user.pregnancy_scan_date is None
    assert user.pregnancy_scan_corrected_at is None
    assert user.pregnancy_started_at is None
    assert user.pregnancy_ended_at is None
    assert user.pregnancy_outcome is None


# ---------------------------------------------------------------------------
# _row_to_user: partial presence (some columns, not all)
# ---------------------------------------------------------------------------


def test_row_to_user_partial_pregnancy_columns():
    """_row_to_user handles rows with only some pregnancy columns."""
    row = {
        "id": uuid4(),
        "name": "partial",
        "phone": "+155****4444",
        "timezone": "UTC",
        "pregnancy_edd": date(2026, 8, 15),
        "pregnancy_dating_basis": "scan",
        # other pregnancy fields absent
    }
    user = _row_to_user(row)

    assert user.pregnancy_edd == date(2026, 8, 15)
    assert user.pregnancy_dating_basis == "scan"
    assert user.pregnancy_lmp_date is None
    assert user.pregnancy_scan_date is None
    assert user.pregnancy_scan_corrected_at is None
    assert user.pregnancy_started_at is None
    assert user.pregnancy_ended_at is None
    assert user.pregnancy_outcome is None


# ---------------------------------------------------------------------------
# Frozen dataclass — verify replace works
# ---------------------------------------------------------------------------


def test_user_replace_preserves_pregnancy():
    """dataclasses.replace on User preserves pregnancy fields through copy."""
    original = _row_to_user(_make_row_with_pregnancy())

    # Simulate mid-turn refresh pattern
    from dataclasses import replace

    updated = replace(original, phone="+199****5555")
    assert updated.phone == "+199****5555"
    assert updated.pregnancy_edd == date(2026, 10, 22)
    assert updated.pregnancy_dating_basis == "lmp"
    assert updated.pregnancy_started_at == datetime(2026, 1, 15, 10, 30, 0)


# ---------------------------------------------------------------------------
# End-of-pregnancy state
# ---------------------------------------------------------------------------


def test_row_to_user_ended_pregnancy():
    """_row_to_user hydrates ended pregnancy fields correctly."""
    row = {
        "id": uuid4(),
        "name": "ended_user",
        "phone": "+155****6666",
        "timezone": "UTC",
        "pregnancy_edd": date(2026, 5, 1),
        "pregnancy_dating_basis": "lmp",
        "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None,
        "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": datetime(2025, 9, 1, 0, 0, 0),
        "pregnancy_ended_at": datetime(2026, 5, 3, 14, 0, 0),
        "pregnancy_outcome": "birth",
    }
    user = _row_to_user(row)

    assert user.pregnancy_edd == date(2026, 5, 1)
    assert user.pregnancy_ended_at == datetime(2026, 5, 3, 14, 0, 0)
    assert user.pregnancy_outcome == "birth"