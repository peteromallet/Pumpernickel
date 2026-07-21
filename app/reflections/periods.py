"""Reflection period resolution in user timezone.

Resolves day/week/month/custom periods for reflection sessions, keeping
temporal classification separate from capture eligibility.  Period resolution
is deterministic: given a datetime and timezone, the module computes
period_start and period_end boundaries.

Key design:
  - Period resolution is a pure function — no database or side effects.
  - Content hints (e.g., user says "this week") override clock-derived periods.
  - Local rollover (midnight crossing) boundaries are correct per zone.
  - Content-vs-clock conflicts are resolved in favour of explicit content hints.
  - Period resolution does NOT gate capture eligibility — that is the
    classifier's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


# ── Public surface ──────────────────────────────────────────────────────────

VALID_TEMPORAL_SCOPES: frozenset[str] = frozenset({
    "instant", "day", "week", "month", "custom", "none",
})


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PeriodResult:
    """Resolved period for a reflection session.

    Attributes:
        temporal_scope: One of VALID_TEMPORAL_SCOPES.
        period_start: Start of the period (timezone-aware datetime).
        period_end: End of the period (timezone-aware datetime), exclusive.
        timezone: IANA timezone string used for resolution (e.g. "America/New_York").
        source: How the period was determined ("explicit_content", "clock_time", "default").
    """

    temporal_scope: str
    period_start: datetime
    period_end: datetime
    timezone: str
    source: str

    def __post_init__(self) -> None:
        if self.temporal_scope not in VALID_TEMPORAL_SCOPES:
            raise ValueError(
                f"invalid temporal_scope {self.temporal_scope!r}; "
                f"expected one of {sorted(VALID_TEMPORAL_SCOPES)}"
            )
        if self.period_start.tzinfo is None:
            raise ValueError("period_start must be timezone-aware")
        if self.period_end.tzinfo is None:
            raise ValueError("period_end must be timezone-aware")
        if self.period_start >= self.period_end:
            raise ValueError(
                f"period_start ({self.period_start}) must be before "
                f"period_end ({self.period_end})"
            )


# ── Content-override patterns ───────────────────────────────────────────────

# When the user explicitly names a temporal scope, we override the
# clock-derived period to reflect what they said.  These patterns are
# deliberately broader than the classifier's explicit patterns because
# period resolution is about interpreting the scope, not deciding
# whether something is a reflection.

_CONTENT_SCOPE_DAY = re.compile(
    r"\b(today|this day|daily|today'?s|all day)\b", re.IGNORECASE
)
_CONTENT_SCOPE_WEEK = re.compile(
    r"\b(this week|weekly|week'?s review|week review|past week|last week|"
    r"whole week)\b",
    re.IGNORECASE,
)
_CONTENT_SCOPE_MONTH = re.compile(
    r"\b(this month|monthly|month'?s review|month review|past month|"
    r"last month|whole month)\b",
    re.IGNORECASE,
)
_CONTENT_SCOPE_CUSTOM_START = re.compile(
    r"\b(since\s+|from\s+|starting\s+)"
    r"(\d{4}-\d{2}-\d{2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)",
    re.IGNORECASE,
)
_CONTENT_SCOPE_CUSTOM_END = re.compile(
    r"\b(until\s+|to\s+|through\s+|ending\s+)"
    r"(\d{4}-\d{2}-\d{2}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?)",
    re.IGNORECASE,
)


# ── Public API ──────────────────────────────────────────────────────────────


def resolve_period(
    temporal_scope: str,
    *,
    user_tz: str = "UTC",
    reference_dt: datetime | None = None,
    user_text: str | None = None,
) -> PeriodResult:
    """Resolve period_start and period_end for a given temporal scope.

    Args:
        temporal_scope: One of "instant", "day", "week", "month", "custom", "none".
        user_tz: IANA timezone string (e.g. "America/New_York").
        reference_dt: The datetime to resolve relative to (default: now UTC).
        user_text: Optional user message text for content-override parsing.

    Returns:
        PeriodResult with bounded start/end.

    Raises:
        ValueError: If temporal_scope is invalid or tz is not recognized.
    """
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if temporal_scope not in VALID_TEMPORAL_SCOPES:
        raise ValueError(
            f"invalid temporal_scope {temporal_scope!r}; "
            f"expected one of {sorted(VALID_TEMPORAL_SCOPES)}"
        )

    try:
        tz = ZoneInfo(user_tz)
    except (ZoneInfoNotFoundError, KeyError):
        raise ValueError(f"Unrecognized timezone: {user_tz!r}") from None

    # Resolve reference datetime
    if reference_dt is None:
        reference_dt = datetime.now(timezone.utc)
    if reference_dt.tzinfo is None:
        raise ValueError("reference_dt must be timezone-aware")

    # Convert to user-local time for boundary calculations
    local_dt = reference_dt.astimezone(tz)
    local_date = local_dt.date()

    # ── Content override: check if user text hints at a different scope ──
    scope_override: str | None = None
    if user_text:
        scope_override = _detect_content_scope_override(user_text)

    effective_scope = scope_override or temporal_scope

    # ── Resolve based on effective scope ────────────────────────────────
    if effective_scope == "instant":
        start, end = _resolve_instant(local_dt, tz)
        source = "explicit_content" if scope_override else "clock_time"
    elif effective_scope == "day":
        start, end = _resolve_day(local_date, tz)
        source = "explicit_content" if scope_override else "clock_time"
    elif effective_scope == "week":
        start, end = _resolve_week(local_date, tz)
        source = "explicit_content" if scope_override else "clock_time"
    elif effective_scope == "month":
        start, end = _resolve_month(local_date, tz)
        source = "explicit_content" if scope_override else "clock_time"
    elif effective_scope == "custom":
        start, end = _resolve_custom(local_dt, tz, user_text)
        source = "explicit_content" if scope_override else "default"
    else:
        # "none" — no period
        start = local_dt
        end = local_dt + timedelta(hours=1)
        source = "default"

    return PeriodResult(
        temporal_scope=effective_scope,
        period_start=start,
        period_end=end,
        timezone=user_tz,
        source=source,
    )


def resolve_from_classification(
    *,
    temporal_scope: str,
    user_tz: str,
    reference_dt: datetime | None = None,
    user_text: str | None = None,
) -> PeriodResult:
    """Convenience: resolve period from classification output.

    This is the bridge between the classifier and period resolver.
    Call after ``classify_message`` to get the period boundaries.

    Args:
        temporal_scope: From ClassificationResult.temporal_scope.
        user_tz: User's IANA timezone string.
        reference_dt: The datetime to resolve relative to.
        user_text: Original user message for content-override.

    Returns:
        PeriodResult.
    """
    return resolve_period(
        temporal_scope=temporal_scope,
        user_tz=user_tz,
        reference_dt=reference_dt,
        user_text=user_text,
    )


# ── Internal resolvers ──────────────────────────────────────────────────────


def _localize(dt: datetime, tz) -> datetime:
    """Ensure dt has the given tzinfo."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tz)
    return dt.astimezone(tz)


def _start_of_day(d: date, tz) -> datetime:
    """00:00:00 on the given date in the given timezone."""
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=tz)


def _end_of_day(d: date, tz) -> datetime:
    """00:00:00 on the *next* day (exclusive end)."""
    next_day = d + timedelta(days=1)
    return datetime(next_day.year, next_day.month, next_day.day, 0, 0, 0, tzinfo=tz)


def _resolve_instant(local_dt: datetime, tz) -> tuple[datetime, datetime]:
    """Instant: a 5-minute window centered on the reference time."""
    start = local_dt - timedelta(minutes=2, seconds=30)
    end = local_dt + timedelta(minutes=2, seconds=30)
    return start, end


def _resolve_day(local_date: date, tz) -> tuple[datetime, datetime]:
    """Day: midnight-to-midnight in user timezone."""
    return _start_of_day(local_date, tz), _end_of_day(local_date, tz)


def _resolve_week(local_date: date, tz) -> tuple[datetime, datetime]:
    """Week: Monday 00:00 to next Monday 00:00 in user timezone.

    The week starts on Monday and ends at the following Monday midnight.
    """
    # Monday = 0, Sunday = 6
    weekday = local_date.weekday()  # 0 = Monday
    monday = local_date - timedelta(days=weekday)
    next_monday = monday + timedelta(days=7)
    return _start_of_day(monday, tz), _start_of_day(next_monday, tz)


def _resolve_month(local_date: date, tz) -> tuple[datetime, datetime]:
    """Month: 1st 00:00 of this month to 1st 00:00 of next month."""
    first_of_month = local_date.replace(day=1)
    # Next month
    if first_of_month.month == 12:
        first_of_next = first_of_month.replace(year=first_of_month.year + 1, month=1)
    else:
        first_of_next = first_of_month.replace(month=first_of_month.month + 1)
    return _start_of_day(first_of_month, tz), _start_of_day(first_of_next, tz)


def _resolve_custom(
    local_dt: datetime, tz, user_text: str | None = None
) -> tuple[datetime, datetime]:
    """Custom: parse from user_text if available, otherwise default to a day.

    If the user provided start/end dates in text, extract them.
    Otherwise, use the reference datetime ± 1 hour as a placeholder.
    """
    if user_text:
        start_match = _CONTENT_SCOPE_CUSTOM_START.search(user_text)
        end_match = _CONTENT_SCOPE_CUSTOM_END.search(user_text)

        if start_match and end_match:
            try:
                start_dt = _parse_date_string(start_match.group(2), tz)
                end_dt = _parse_date_string(end_match.group(2), tz)
                # end is exclusive, so add a day
                end_dt = end_dt + timedelta(days=1)
                if start_dt < end_dt:
                    return start_dt, end_dt
            except (ValueError, IndexError):
                pass
        elif start_match:
            try:
                start_dt = _parse_date_string(start_match.group(2), tz)
                end_dt = local_dt
                if start_dt < end_dt:
                    return start_dt, end_dt
            except (ValueError, IndexError):
                pass

    # Default: ± 1 hour window
    return local_dt - timedelta(hours=1), local_dt + timedelta(hours=1)


def _parse_date_string(date_str: str, tz) -> datetime:
    """Parse a date string into a midnight-local datetime.

    Supports:
      - ISO: 2026-07-19
      - Natural: July 19, 2026 / Jul 19th, 2026 / July 19 (current year)
      - Natural with ordinals: July 19th

    Returns a timezone-aware datetime at midnight in tz.
    """
    from datetime import date as dt_date

    # Try ISO format first
    try:
        d = dt_date.fromisoformat(date_str.strip())
        return _start_of_day(d, tz)
    except (ValueError, TypeError):
        pass

    # Try natural language formats
    cleaned = date_str.strip().rstrip(",.")
    # Remove internal commas and ordinal suffixes
    cleaned = cleaned.replace(",", "")
    cleaned = re.sub(r"(\d+)(st|nd|rd|th)", r"\1", cleaned)

    # Formats with year
    formats_with_year = [
        "%B %d %Y",   # July 19 2026
        "%b %d %Y",   # Jul 19 2026
        "%d %B %Y",   # 19 July 2026
        "%d %b %Y",   # 19 Jul 2026
    ]

    for fmt in formats_with_year:
        try:
            d = datetime.strptime(cleaned, fmt).date()
            return _start_of_day(d, tz)
        except ValueError:
            continue

    # Formats without year — use current year from reference context
    formats_no_year = [
        "%B %d",   # July 19
        "%b %d",   # Jul 19
        "%d %B",   # 19 July
        "%d %b",   # 19 Jul
    ]

    for fmt in formats_no_year:
        try:
            d = datetime.strptime(cleaned, fmt).date()
            # Default to current year
            current_year = datetime.now(tz).year
            d = d.replace(year=current_year)
            return _start_of_day(d, tz)
        except ValueError:
            continue

    raise ValueError(f"Could not parse date string: {date_str!r}")


def _detect_content_scope_override(user_text: str) -> str | None:
    """Detect if user text explicitly names a temporal scope.

    Returns the scope string, or None if no override detected.
    Content override takes precedence over the classification result's
    temporal_scope because the user's words are the strongest signal.
    """
    if not user_text:
        return None

    # Check in priority order: custom first (most specific), then month, week, day
    if _CONTENT_SCOPE_CUSTOM_START.search(user_text) and _CONTENT_SCOPE_CUSTOM_END.search(user_text):
        return "custom"
    if _CONTENT_SCOPE_MONTH.search(user_text):
        return "month"
    if _CONTENT_SCOPE_WEEK.search(user_text):
        return "week"
    if _CONTENT_SCOPE_DAY.search(user_text):
        return "day"
    return None
