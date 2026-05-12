"""
Pregnancy helper module — pure functions with no DB calls.

Provides gestational-age math, trimester classification, and prompt-ready
state rendering for the Tante Rosi pregnancy coach bot.

All functions accept an explicit ``today`` kwarg for test reproducibility.
When ``today`` is omitted, the real current date (UTC) is used.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from app.models.user import User

logger = logging.getLogger(__name__)

GESTATION_WEEKS = 40
LATE_OVERDUE_WEEKS = 42
_DAYS_PER_WEEK = 7
_RECENT_THRESHOLD_DAYS = 90


# ---------------------------------------------------------------------------
# gestational_age
# ---------------------------------------------------------------------------

def gestational_age(
    edd: date,
    today: date | None = None,
) -> tuple[int, int]:
    """Return (weeks, days) since LMP-equivalent (GESTATION_WEEKS weeks before EDD).

    Clamped to [(0, 0), (LATE_OVERDUE_WEEKS, 6)].

    Raises:
        ValueError: if EDD is more than 1 year in the future from *today*
                    (data-error guard — a pregnancy can't be >1 year away).
    """
    _today = today or _utc_today()
    # The conceptual LMP date is EDD minus 40 weeks.
    lmp = edd - timedelta(weeks=GESTATION_WEEKS)
    delta = (_today - lmp).days

    # Reject implausibly distant EDDs (>1 year out).
    if delta < -365:
        raise ValueError(
            f"EDD {edd.isoformat()} is more than 1 year in the future "
            f"from {_today.isoformat()}. Refusing to compute gestational age."
        )

    # Clamp negative ages to (0, 0).
    if delta < 0:
        logger.warning(
            "Computed negative gestational age from EDD %s (today %s) — "
            "clamping to (0, 0).",
            edd.isoformat(),
            _today.isoformat(),
        )
        return (0, 0)

    # Clamp to late-overdue ceiling.
    max_days = LATE_OVERDUE_WEEKS * _DAYS_PER_WEEK + 6
    if delta > max_days:
        delta = max_days

    weeks = delta // _DAYS_PER_WEEK
    days = delta % _DAYS_PER_WEEK
    return (weeks, days)


# ---------------------------------------------------------------------------
# trimester
# ---------------------------------------------------------------------------

def trimester(weeks: int) -> Literal["first", "second", "third"]:
    """Standard clinical trimester cutoffs.

    - First:  0 – 12  (weeks 0–12 inclusive)
    - Second: 13 – 27
    - Third:  28+
    """
    if weeks <= 12:
        return "first"
    if weeks <= 27:
        return "second"
    return "third"


# ---------------------------------------------------------------------------
# is_pregnancy_active
# ---------------------------------------------------------------------------

def is_pregnancy_active(user: User) -> bool:
    """True iff ``pregnancy_edd`` is set AND ``pregnancy_ended_at`` is null."""
    return user.pregnancy_edd is not None and user.pregnancy_ended_at is None


# ---------------------------------------------------------------------------
# days_since_loss
# ---------------------------------------------------------------------------

def days_since_loss(user: User, today: date | None = None) -> int | None:
    """Return the number of days since ``pregnancy_ended_at`` when outcome is
    'loss', else None.

    Only meaningful for sensitivity rendering — helps the bot choose the right
    emotional frame.
    """
    if user.pregnancy_outcome != "loss" or user.pregnancy_ended_at is None:
        return None
    _today = today or _utc_today()
    ended_date = _as_date(user.pregnancy_ended_at)
    return (_today - ended_date).days


# ---------------------------------------------------------------------------
# format_pregnancy_state
# ---------------------------------------------------------------------------

def format_pregnancy_state(user: User, today: date | None = None) -> str | None:
    """Return the prompt-ready pregnancy state block, or None if there is
    nothing to render.

    Render branches
    ---------------
    1. ``pregnancy_edd IS NULL`` → None
    2. Active pregnancy → ``"17w2d (second trimester, EDD 2026-10-22, basis: lmp)"``
    3. Recent loss (< 90 days since ``pregnancy_ended_at``) →
       ``"Recent loss (12 days ago). Handle with care."``
    4. Recent birth (< 90 days since ``pregnancy_ended_at``) →
       ``"Birth 12 days ago (EDD was 2026-10-22)."``
    5. Ended > 90 days ago → None
    """
    # --- No pregnancy data at all -------------------------------------------
    if user.pregnancy_edd is None:
        return None

    _today = today or _utc_today()

    # --- Ended pregnancy ----------------------------------------------------
    if user.pregnancy_ended_at is not None:
        if user.pregnancy_outcome is None:
            logger.warning(
                "User %s has pregnancy_ended_at but no pregnancy_outcome — "
                "skipping pregnancy render.",
                user.id,
            )
            return None

        ended_date = _as_date(user.pregnancy_ended_at)
        days_ago = (_today - ended_date).days

        if days_ago > _RECENT_THRESHOLD_DAYS:
            return None  # stale — no render

        if user.pregnancy_outcome in ("loss", "termination"):
            return f"Recent loss ({days_ago} days ago). Handle with care."

        if user.pregnancy_outcome == "birth":
            edd_str = user.pregnancy_edd.isoformat()
            return f"Birth {days_ago} days ago (EDD was {edd_str})."

        # Unknown outcome — treat as loss for safety
        logger.warning(
            "User %s has unrecognized pregnancy_outcome %r — treating as loss.",
            user.id,
            user.pregnancy_outcome,
        )
        return f"Recent loss ({days_ago} days ago). Handle with care."

    # --- Active pregnancy ---------------------------------------------------
    if user.pregnancy_dating_basis is None:
        logger.warning(
            "User %s has pregnancy_edd set but no pregnancy_dating_basis — "
            "data corruption; skipping pregnancy render.",
            user.id,
        )
        return None

    try:
        weeks, days = gestational_age(user.pregnancy_edd, today=_today)
    except ValueError as exc:
        logger.warning("Cannot compute gestational age for user %s: %s", user.id, exc)
        return None

    tri = trimester(weeks)
    edd_str = user.pregnancy_edd.isoformat()
    basis = user.pregnancy_dating_basis

    # Overdue rendering
    if weeks >= LATE_OVERDUE_WEEKS:
        return f"42w (overdue, EDD was {edd_str})"

    return f"{weeks}w{days}d ({tri} trimester, EDD {edd_str}, basis: {basis})"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _utc_today() -> date:
    """Return today's date in UTC."""
    return datetime.now(timezone.utc).date()


def _as_date(dt: datetime) -> date:
    """Extract the date portion of a datetime, using its tzinfo if available,
    otherwise treating it as UTC."""
    if dt.tzinfo is not None:
        return dt.date()
    # Naive datetime — treat as UTC
    return dt.replace(tzinfo=timezone.utc).date()


# ---------------------------------------------------------------------------
# stdlib imports (placed at bottom to keep top clean)
# ---------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402