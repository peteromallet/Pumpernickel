"""Test pregnancy helper module (app/services/pregnancy.py).

Covers every branch:
- gestational_age at boundaries (0w, 12/13w, 27/28w, 39/40/41/42w, overdue)
- EDD >1y future raises ValueError
- trimester classification
- is_pregnancy_active
- days_since_loss
- format_pregnancy_state: all 5 render branches + data-corruption fallbacks
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.pregnancy import (
    GESTATION_WEEKS,
    LATE_OVERDUE_WEEKS,
    days_since_loss,
    format_pregnancy_state,
    gestational_age,
    is_pregnancy_active,
    trimester,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_user(
    *,
    pregnancy_edd: date | None = None,
    pregnancy_dating_basis: str | None = None,
    pregnancy_lmp_date: date | None = None,
    pregnancy_scan_date: date | None = None,
    pregnancy_scan_corrected_at: datetime | None = None,
    pregnancy_started_at: datetime | None = None,
    pregnancy_ended_at: datetime | None = None,
    pregnancy_outcome: str | None = None,
    **kwargs,
) -> User:
    defaults: dict = {
        "id": uuid4(),
        "name": "test_user",
        "phone": "+155****0000",
        "timezone": "Europe/Berlin",
    }
    defaults.update(kwargs)
    return User(
        pregnancy_edd=pregnancy_edd,
        pregnancy_dating_basis=pregnancy_dating_basis,
        pregnancy_lmp_date=pregnancy_lmp_date,
        pregnancy_scan_date=pregnancy_scan_date,
        pregnancy_scan_corrected_at=pregnancy_scan_corrected_at,
        pregnancy_started_at=pregnancy_started_at,
        pregnancy_ended_at=pregnancy_ended_at,
        pregnancy_outcome=pregnancy_outcome,
        **defaults,
    )


TD = __import__("datetime").timedelta


# ===================================================================
# gestational_age
# ===================================================================


class TestGestationalAge:
    """Tests for gestational_age(edd, today=None)."""

    def test_mid_pregnancy(self):
        """Standard mid-pregnancy calculation.

        EDD 2026-10-22, LMP = 2026-01-15 (EDD - 280 days).
        From LMP to 2026-05-12 = 117 days = 16w5d.
        """
        weeks, days = gestational_age(date(2026, 10, 22), today=date(2026, 5, 12))
        assert (weeks, days) == (16, 5)

    def test_zero_weeks(self):
        """EDD exactly 40 weeks from today → (0, 0)."""
        today = date(2026, 5, 12)
        edd = today + TD(weeks=40)
        weeks, days = gestational_age(edd, today=today)
        assert (weeks, days) == (0, 0)

    def test_12w(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=12, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (12, 0)

    def test_13w(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=13, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (13, 0)

    def test_28w(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=28, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (28, 0)

    def test_39w(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=39, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (39, 0)

    def test_40w(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=40, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (40, 0)

    def test_41w(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=41, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (41, 0)

    def test_42w(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=42, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (42, 0)

    def test_42w6d(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=42, days=6)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (42, 6)

    def test_clamped_beyond_42w6d(self):
        today = date(2026, 5, 12)
        lmp = today - TD(weeks=44, days=0)
        edd = lmp + TD(weeks=40)
        assert gestational_age(edd, today=today) == (42, 6)

    def test_edp_past_by_more_than_2_weeks(self):
        today = date(2026, 5, 12)
        edd = today - TD(weeks=3)
        weeks, days = gestational_age(edd, today=today)
        assert weeks >= 42

    def test_negative_age_clamped(self, caplog):
        today = date(2026, 5, 12)
        edd = today + TD(days=300)  # LMP = today+20 → delta=-20
        with caplog.at_level(logging.WARNING):
            weeks, days = gestational_age(edd, today=today)
        assert (weeks, days) == (0, 0)
        assert "clamping to (0, 0)" in caplog.text

    def test_edd_more_than_1_year_future_raises(self):
        today = date(2026, 5, 12)
        edd = today + TD(days=700)
        with pytest.raises(ValueError, match="more than 1 year in the future"):
            gestational_age(edd, today=today)

    def test_edd_way_future_raises(self):
        today = date(2026, 5, 12)
        edd = today + TD(days=800)
        with pytest.raises(ValueError, match="more than 1 year in the future"):
            gestational_age(edd, today=today)

    def test_explicit_today_uses_default_utc(self):
        edd = date(2026, 10, 22)
        weeks, days = gestational_age(edd)
        assert weeks >= 0
        assert 0 <= days <= 6


# ===================================================================
# trimester
# ===================================================================


class TestTrimester:
    def test_first_0(self):
        assert trimester(0) == "first"

    def test_first_12(self):
        assert trimester(12) == "first"

    def test_second_13(self):
        assert trimester(13) == "second"

    def test_second_27(self):
        assert trimester(27) == "second"

    def test_third_28(self):
        assert trimester(28) == "third"

    def test_third_40(self):
        assert trimester(40) == "third"

    def test_third_42(self):
        assert trimester(42) == "third"


# ===================================================================
# is_pregnancy_active
# ===================================================================


class TestIsPregnancyActive:
    def test_active(self):
        user = _make_user(pregnancy_edd=date(2026, 10, 22))
        assert is_pregnancy_active(user) is True

    def test_inactive_no_edd(self):
        user = _make_user(pregnancy_edd=None)
        assert is_pregnancy_active(user) is False

    def test_inactive_ended(self):
        user = _make_user(
            pregnancy_edd=date(2026, 5, 1),
            pregnancy_ended_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            pregnancy_outcome="birth",
        )
        assert is_pregnancy_active(user) is False


# ===================================================================
# days_since_loss
# ===================================================================


class TestDaysSinceLoss:
    def test_loss(self):
        ended = datetime(2026, 5, 1, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2026, 4, 15),
            pregnancy_ended_at=ended,
            pregnancy_outcome="loss",
        )
        assert days_since_loss(user, today=date(2026, 5, 12)) == 11

    def test_not_loss_birth(self):
        user = _make_user(
            pregnancy_edd=date(2026, 5, 1),
            pregnancy_ended_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            pregnancy_outcome="birth",
        )
        assert days_since_loss(user, today=date(2026, 5, 12)) is None

    def test_not_loss_termination(self):
        user = _make_user(
            pregnancy_edd=date(2026, 5, 1),
            pregnancy_ended_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            pregnancy_outcome="termination",
        )
        assert days_since_loss(user, today=date(2026, 5, 12)) is None

    def test_no_ended_at(self):
        user = _make_user(pregnancy_edd=date(2026, 10, 22), pregnancy_outcome="loss")
        assert days_since_loss(user) is None


# ===================================================================
# format_pregnancy_state
# ===================================================================

FROZEN = date(2026, 5, 12)


class TestFormatPregnancyState:
    """Tests for format_pregnancy_state(user, today=...)."""

    # --- Branch 1: no pregnancy data ---------------------------------------

    def test_no_edd_returns_none(self):
        assert format_pregnancy_state(_make_user(pregnancy_edd=None)) is None

    # --- Branch 2: active pregnancy ----------------------------------------

    def test_active_early(self):
        """Active pregnancy at 0w0d (EDD = today + 40w)."""
        edd = FROZEN + TD(weeks=40)
        user = _make_user(
            pregnancy_edd=edd,
            pregnancy_dating_basis="lmp",
            pregnancy_started_at=datetime(2026, 5, 12, tzinfo=timezone.utc),
        )
        result = format_pregnancy_state(user, today=FROZEN)
        assert result == "0w0d (first trimester, EDD 2027-02-16, basis: lmp)"

    def test_active_mid_second_trimester(self):
        """Active pregnancy at 16w5d, second trimester."""
        edd = date(2026, 10, 22)
        user = _make_user(
            pregnancy_edd=edd,
            pregnancy_dating_basis="lmp",
            pregnancy_started_at=datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),
        )
        result = format_pregnancy_state(user, today=FROZEN)
        assert result is not None
        assert "16w5d" in result
        assert "second trimester" in result
        assert "EDD 2026-10-22" in result
        assert "basis: lmp" in result

    def test_active_third_trimester(self):
        """Active pregnancy in third trimester at 32w5d."""
        edd = date(2026, 10, 22)
        user = _make_user(pregnancy_edd=edd, pregnancy_dating_basis="scan")
        result = format_pregnancy_state(user, today=date(2026, 9, 1))
        assert result is not None
        assert "third trimester" in result
        assert "basis: scan" in result

    def test_active_overdue(self):
        """Active pregnancy past 42 weeks → overdue rendering."""
        edd = date(2026, 10, 22)
        user = _make_user(pregnancy_edd=edd, pregnancy_dating_basis="lmp")
        result = format_pregnancy_state(user, today=date(2026, 12, 1))
        assert result == "42w (overdue, EDD was 2026-10-22)"

    # --- Branch 3: recent loss (< 90 days) ---------------------------------

    def test_recent_loss(self):
        ended = datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2026, 9, 1),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=ended,
            pregnancy_outcome="loss",
        )
        result = format_pregnancy_state(user, today=FROZEN)
        assert result == "Recent loss (11 days ago). Handle with care."

    def test_recent_termination(self):
        ended = datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2026, 9, 1),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=ended,
            pregnancy_outcome="termination",
        )
        result = format_pregnancy_state(user, today=FROZEN)
        assert result == "Recent loss (7 days ago). Handle with care."

    # --- Branch 4: recent birth (< 90 days) ---------------------------------

    def test_recent_birth(self):
        ended = datetime(2026, 4, 30, 8, 0, 0, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2026, 5, 1),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=ended,
            pregnancy_outcome="birth",
        )
        result = format_pregnancy_state(user, today=FROZEN)
        assert result == "Birth 12 days ago (EDD was 2026-05-01)."

    # --- Branch 5: ended > 90 days ago → None ------------------------------

    def test_ended_over_90_days_returns_none(self):
        ended = datetime(2025, 12, 1, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2025, 11, 15),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=ended,
            pregnancy_outcome="birth",
        )
        assert format_pregnancy_state(user, today=FROZEN) is None

    # --- Data corruption cases ---------------------------------------------

    def test_edd_without_dating_basis_returns_none(self, caplog):
        user = _make_user(pregnancy_edd=date(2026, 10, 22), pregnancy_dating_basis=None)
        with caplog.at_level(logging.WARNING):
            result = format_pregnancy_state(user, today=FROZEN)
        assert result is None
        assert "no pregnancy_dating_basis" in caplog.text

    def test_ended_at_without_outcome_returns_none(self, caplog):
        user = _make_user(
            pregnancy_edd=date(2026, 5, 1),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=datetime(2026, 5, 3, tzinfo=timezone.utc),
            pregnancy_outcome=None,
        )
        with caplog.at_level(logging.WARNING):
            result = format_pregnancy_state(user, today=FROZEN)
        assert result is None
        assert "no pregnancy_outcome" in caplog.text

    def test_edd_too_far_future_returns_none(self, caplog):
        user = _make_user(
            pregnancy_edd=date(2030, 1, 1),  # >1y future → ValueError
            pregnancy_dating_basis="lmp",
        )
        with caplog.at_level(logging.WARNING):
            result = format_pregnancy_state(user, today=FROZEN)
        assert result is None
        assert "Cannot compute gestational age" in caplog.text

    def test_negative_age_active_render(self):
        """EDD > 40w future → LMP is after today → clamps to (0,0)."""
        edd = FROZEN + TD(days=300)  # LMP = FROZEN+20 → delta=-20
        user = _make_user(pregnancy_edd=edd, pregnancy_dating_basis="scan")
        result = format_pregnancy_state(user, today=FROZEN)
        assert result is not None
        assert result.startswith("0w")  # clamped
        assert "first trimester" in result