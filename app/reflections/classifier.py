"""Reflection message classifier — locked precedence policy.

Encodes the settled classification precedence:
  1. Explicit wording (user says "reflection", "checkpoint", "retrospective", etc.)
  2. Active-session context (an open collecting session exists)
  3. Message semantics (logistics, jokes, questions, tasks, reminders, follow-ups
     are classified as non-reflection; introspective content is reflection)
  4. Conversational context (how the message relates to preceding exchange)
  5. Local time (weakest signal — only tips the balance when everything else
     is ambiguous)

Returns ``(phase, temporal_scope, confidence, source)`` where source is one
of the classification_source enum values (wired to the reflection session).

Time signal is intentionally weakest — a message at day boundary that reads
as a joke will NOT be classified as a reflection just because it's 11:59 PM.
Ambiguous messages that don't match any positive or negative pattern fall
back to ``freeform`` phase with lower confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ── Public surface ──────────────────────────────────────────────────────────

# Phase values mirror the migration CHECK constraint (app/services/reflections.py)
VALID_PHASES: frozenset[str] = frozenset({
    "opening", "closing", "checkpoint", "prospective",
    "retrospective", "freeform",
})

# Temporal scope values
VALID_TEMPORAL_SCOPES: frozenset[str] = frozenset({
    "instant", "day", "week", "month", "custom", "none",
})

# Classification source tagging
_SOURCE_EXPLICIT = "explicit_wording"
_SOURCE_SESSION = "active_session"
_SOURCE_SEMANTICS = "message_semantics"
_SOURCE_CONVERSATION = "conversational_context"
_SOURCE_TIME = "local_time"
_SOURCE_FREEFORM = "freeform_fallback"


# ── Result type ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    """Immutable result from the reflection classifier.

    Attributes:
        phase: One of VALID_PHASES — the temporal phase the message fits into.
        temporal_scope: One of VALID_TEMPORAL_SCOPES — the bounded time window.
        confidence: Float in [0, 1] indicating classification strength.
        source: Tag describing which precedence tier won (e.g. "explicit_wording").
        metadata: Arbitrary structured metadata for the classification_metadata
                  column (serialisable to JSON).
    """

    phase: str
    temporal_scope: str
    confidence: float
    source: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.phase not in VALID_PHASES:
            raise ValueError(
                f"invalid phase {self.phase!r}; expected one of {sorted(VALID_PHASES)}"
            )
        if self.temporal_scope not in VALID_TEMPORAL_SCOPES:
            raise ValueError(
                f"invalid temporal_scope {self.temporal_scope!r}; "
                f"expected one of {sorted(VALID_TEMPORAL_SCOPES)}"
            )
        if not (0 <= self.confidence <= 1):
            raise ValueError(
                f"confidence must be in [0, 1], got {self.confidence}"
            )


# ── Negative pattern sets ───────────────────────────────────────────────────

# Messages that are explicitly NOT reflections, even if they mention time
# or feel introspective.  These are matched before the positive patterns.

_NEGATIVE_EXPLICIT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(joke|kidding|just kidding|lol|haha|funny|pun)\b", re.IGNORECASE), "joke"),
    (re.compile(r"^\s*(hi|hello|hey|yo|sup|good morning|good evening|good afternoon)\s*[!.]*\s*$", re.IGNORECASE), "greeting_only"),
    (re.compile(r"^\s*(bye|goodbye|see you|talk later|gn|good night)\s*[!.]*\s*$", re.IGNORECASE), "farewell_only"),
    (re.compile(r"^\s*(thanks|thank you|thx|ty)\s*[!.]*\s*$", re.IGNORECASE), "thanks_only"),
    (re.compile(r"^\s*(ok|okay|k|kk|got it|acknowledged|cool)\s*[!.]*\s*$", re.IGNORECASE), "ack_only"),
]

# Question patterns — pure information-seeking, not introspective
_NEGATIVE_QUESTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(what time is|what day is|what date is|when is)\b", re.IGNORECASE), "clock_check"),
    (re.compile(r"\b(how do I|how to|what does .+ mean|define|explain)\b", re.IGNORECASE), "how_to"),
    (re.compile(r"\b(weather|temperature|forecast)\b", re.IGNORECASE), "weather"),
    (re.compile(r"\b(directions|navigate|where is|how far)\b", re.IGNORECASE), "navigation"),
]

# Task/reminder patterns — action-oriented, not reflective
_NEGATIVE_TASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(remind me|set a reminder|set reminder|create reminder)\b", re.IGNORECASE), "reminder_create"),
    (re.compile(r"\b(add\s+(a\s+)?task|create\s+(a\s+)?task|to[- ]do|todo|action item)\b", re.IGNORECASE), "task_create"),
    (re.compile(r"\b(schedule|appointment|calendar|book a|reserve)\b", re.IGNORECASE), "scheduling"),
    (re.compile(r"\b(follow[- ]up with|check in with|reach out to|ping)\b", re.IGNORECASE), "follow_up"),
    (re.compile(r"\b(send|email|text|message|call|dm) .+(to|for)\b", re.IGNORECASE), "send_action"),
]

# Logistics — coordination, not reflection
_NEGATIVE_LOGISTICS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(rsvp|invite|party|dinner|meeting|sync|standup)\b", re.IGNORECASE), "event_logistics"),
    (re.compile(r"\b(flight|hotel|airbnb|booking|reservation|ticket)\b", re.IGNORECASE), "travel_logistics"),
    (re.compile(r"\b(grocery|shopping list|buy|purchase|order)\b", re.IGNORECASE), "shopping"),
    (re.compile(r"\b(link|url|http|attachment|file|upload)\b", re.IGNORECASE), "link_share"),
]

# Combined negative set with label
_ALL_NEGATIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = (
    _NEGATIVE_EXPLICIT_PATTERNS
    + _NEGATIVE_QUESTION_PATTERNS
    + _NEGATIVE_TASK_PATTERNS
    + _NEGATIVE_LOGISTICS_PATTERNS
)


# ── Positive explicit patterns ──────────────────────────────────────────────

# User explicitly invokes reflection language

_EXPLICIT_PHASE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # More specific phases first; "reflection" (freeform) is the catch-all last
    (re.compile(r"\b(retrospective|retro\b|looking back|in hindsight)\b", re.IGNORECASE), "retrospective"),
    (re.compile(r"\b(prospective|looking ahead|planning forward)\b", re.IGNORECASE), "prospective"),
    (re.compile(r"\b(checkpoint|checking in|progress report|status update)\b", re.IGNORECASE), "checkpoint"),
    (re.compile(r"\b(opening|starting|kicking off|beginning|launch)\b", re.IGNORECASE), "opening"),
    (re.compile(r"\b(closing|wrapping up|finishing|ending|done with)\b", re.IGNORECASE), "closing"),
    # Catch-all: "reflection"/"reflect" → freeform (also matches "starting" but opening above wins)
    (re.compile(r"\b(reflection|reflect|reflecting)\b", re.IGNORECASE), "freeform"),
]

_EXPLICIT_SCOPE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(right now|in this moment|currently|at this instant)\b", re.IGNORECASE), "instant"),
    (re.compile(r"\b(today|this day|daily|today'?s)\b", re.IGNORECASE), "day"),
    (re.compile(r"\b(this week|weekly|week'?s review|week review|my week|the week)\b", re.IGNORECASE), "week"),
    (re.compile(r"\b(this month|monthly|month'?s review|month review|my month|the month)\b", re.IGNORECASE), "month"),
    (re.compile(r"\b(custom|between|from .+ to|since .+ until)\b", re.IGNORECASE), "custom"),
]


# ── Semantic patterns (introspective content hint) ──────────────────────────

_INTROSPECTIVE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(I feel|I felt|I'?ve been feeling|I notice|I noticed)\b", re.IGNORECASE),
    re.compile(r"\b(I think|I thought|I believe|I'?ve realized|it dawned on me)\b", re.IGNORECASE),
    re.compile(r"\b(I learned|I'?ve learned|lesson|takeaway|insight)\b", re.IGNORECASE),
    re.compile(r"\b(pattern|trend|habit|recurring|keeps happening)\b", re.IGNORECASE),
    re.compile(r"\b(progress|improvement|growth|development|change)\b", re.IGNORECASE),
    re.compile(r"\b(struggling|stuck|blocked|obstacle|challenge)\b", re.IGNORECASE),
    re.compile(r"\b(goal|priority|intention|commitment|promise)\b", re.IGNORECASE),
    re.compile(r"\b(gratitude|grateful|thankful|appreciate|blessing)\b", re.IGNORECASE),
    re.compile(r"\b(mood|energy|focus|motivation|drive)\b", re.IGNORECASE),
    re.compile(r"\b(success|failure|win|lose|setback|breakthrough)\b", re.IGNORECASE),
    re.compile(r"\b(wondering|curious|pondering|reflecting|contemplating)\b", re.IGNORECASE),
]


# ── Conversational context hints ────────────────────────────────────────────

# These are applied when conversation context (preceding messages) is provided.
# Contextual continuation signals increase confidence for reflection classification.

_CONVERSATION_CONTINUATION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b(also|and|plus|besides|additionally|moreover)\b", re.IGNORECASE),
    re.compile(r"\b(another thing|one more|also worth noting)\b", re.IGNORECASE),
    re.compile(r"\b(like I said|as I mentioned|to add to that)\b", re.IGNORECASE),
    re.compile(r"\b(elaborating|expanding on|building on)\b", re.IGNORECASE),
]


# ── Time signal (weakest) ───────────────────────────────────────────────────

# Default time-of-day hints.  These are intentionally the weakest signal:
# they only matter when nothing else fires.
# Morning / start-of-day: "opening" phase
# Evening / end-of-day: "closing" phase
# Mid-day: "checkpoint" phase
# Late night: "retrospective" phase

_HOUR_PHASE_MAP: dict[int, str] = {
    **{h: "opening" for h in range(5, 10)},       # 05:00–09:59
    **{h: "checkpoint" for h in range(10, 16)},    # 10:00–15:59
    **{h: "closing" for h in range(16, 21)},       # 16:00–20:59
    **{h: "retrospective" for h in range(21, 24)}, # 21:00–23:59
    **{h: "retrospective" for h in range(0, 5)},    # 00:00–04:59
}

# Time-of-day scope hints
_HOUR_SCOPE_MAP: dict[int, str] = {
    **{h: "day" for h in range(5, 10)},
    **{h: "day" for h in range(10, 16)},
    **{h: "day" for h in range(16, 21)},
    **{h: "day" for h in range(21, 24)},
    **{h: "day" for h in range(0, 5)},
}


# ── Public API ──────────────────────────────────────────────────────────────


def classify_message(
    text: str,
    *,
    active_session_exists: bool = False,
    conversation_context: str | None = None,
    local_datetime: datetime | None = None,
) -> ClassificationResult:
    """Classify a user message for reflection capture.

    Locked precedence policy (highest to lowest):
      1. **Explicit wording**: User names reflection phase/scope directly.
      2. **Active-session context**: An open collecting session exists.
      3. **Message semantics**: Content patterns suggest introspection vs logistics.
      4. **Conversational context**: Message continues a reflection exchange.
      5. **Local time**: Time-of-day hints (weakest — only when all else is ambiguous).

    Negative cases (logistics, jokes, questions, tasks, reminders, follow-ups)
    are explicitly classified as non-reflection.

    Args:
        text: The user message text (plain text, or voice transcript).
        active_session_exists: Whether there's an active collecting session.
        conversation_context: Preceding conversation text for context analysis.
        local_datetime: User's local datetime for time-of-day hints.

    Returns:
        ClassificationResult with phase, temporal_scope, confidence, and source.
    """
    if not text or not text.strip():
        return ClassificationResult(
            phase="freeform",
            temporal_scope="none",
            confidence=0.0,
            source=_SOURCE_FREEFORM,
            metadata={"reason": "empty_text"},
        )

    text_stripped = text.strip()

    # ── Negative check (runs before explicit — a joke about reflection
    #     is not a reflection) ──────────────────────────────────────────
    if _is_negative(text_stripped):
        return _negative_result(text_stripped)

    # ── Tier 1: Explicit wording ─────────────────────────────────────────
    explicit = _check_explicit_wording(text_stripped)
    if explicit is not None:
        return explicit

    # ── Tier 2: Active-session context ────────────────────────────────────
    if active_session_exists:
        # When an active session is open, we bias toward reflection but
        # still check negative patterns — a task command during a session
        # should not be captured.
        if _is_negative(text_stripped):
            return _negative_result(text_stripped)
        return ClassificationResult(
            phase="freeform",
            temporal_scope="instant",
            confidence=0.75,
            source=_SOURCE_SESSION,
            metadata={"reason": "active_session_continuation"},
        )

    # ── Tier 3: Message semantics ─────────────────────────────────────────
    if _is_negative(text_stripped):
        return _negative_result(text_stripped)

    semantic = _check_semantics(text_stripped)
    if semantic is not None:
        return semantic

    # ── Tier 4: Conversational context ────────────────────────────────────
    if conversation_context:
        context_result = _check_conversation_context(
            text_stripped, conversation_context
        )
        if context_result is not None:
            return context_result

    # ── Tier 5: Local time (weakest) ─────────────────────────────────────
    if local_datetime is not None:
        return _classify_by_time(text_stripped, local_datetime)

    # ── Fallback: freeform ────────────────────────────────────────────────
    return ClassificationResult(
        phase="freeform",
        temporal_scope="none",
        confidence=0.1,
        source=_SOURCE_FREEFORM,
        metadata={"reason": "ambiguous_no_signals"},
    )


def is_reflection_candidate(
    text: str,
    *,
    active_session_exists: bool = False,
    conversation_context: str | None = None,
    local_datetime: datetime | None = None,
) -> bool:
    """Quick check: is this message a candidate for reflection capture?

    This is a convenience wrapper around ``classify_message`` that returns
    a boolean instead of the full result.  Use it in routing / gate decisions
    where only the yes/no answer matters.
    """
    if not text or not text.strip():
        return False

    result = classify_message(
        text,
        active_session_exists=active_session_exists,
        conversation_context=conversation_context,
        local_datetime=local_datetime,
    )
    # Negative results have confidence 0 and phase "freeform" with "none" scope
    if result.confidence == 0.0 and "negative" in result.metadata.get("reason", ""):
        return False
    # Freeform with very low confidence is not a candidate
    if result.source == _SOURCE_FREEFORM and result.confidence < 0.3:
        return False
    return True


# ── Internal helpers ────────────────────────────────────────────────────────


def _is_negative(text: str) -> bool:
    """Check if the text matches any explicit negative pattern."""
    for pattern, _label in _ALL_NEGATIVE_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _negative_result(text: str) -> ClassificationResult:
    """Build a result for a negative match."""
    matched_labels: list[str] = []
    for pattern, label in _ALL_NEGATIVE_PATTERNS:
        if pattern.search(text):
            matched_labels.append(label)
    return ClassificationResult(
        phase="freeform",
        temporal_scope="none",
        confidence=0.0,
        source="negative_pattern",
        metadata={
            "reason": "negative",
            "matched_patterns": matched_labels[:5],
        },
    )


def _check_explicit_wording(text: str) -> ClassificationResult | None:
    """Tier 1: check for explicit reflection language."""
    phase: str | None = None
    scope: str | None = None

    for pattern, p in _EXPLICIT_PHASE_PATTERNS:
        if pattern.search(text):
            phase = p
            break

    for pattern, s in _EXPLICIT_SCOPE_PATTERNS:
        if pattern.search(text):
            scope = s
            break

    if phase is None and scope is None:
        return None

    # If we found phase but not scope, default to "instant"
    if phase is not None and scope is None:
        scope = "instant"
    # If we found scope but not phase, default to "freeform"
    if scope is not None and phase is None:
        phase = "freeform"

    return ClassificationResult(
        phase=phase,  # type: ignore[arg-type]
        temporal_scope=scope,  # type: ignore[arg-type]
        confidence=0.95,
        source=_SOURCE_EXPLICIT,
        metadata={"reason": "explicit_wording", "matched_phase": phase, "matched_scope": scope},
    )


def _check_semantics(text: str) -> ClassificationResult | None:
    """Tier 3: check for introspective semantic patterns."""
    for pattern in _INTROSPECTIVE_PATTERNS:
        if pattern.search(text):
            # Determine phase from content hints
            phase = "freeform"
            for ppattern, p in _EXPLICIT_PHASE_PATTERNS:
                if ppattern.search(text):
                    phase = p
                    break
            return ClassificationResult(
                phase=phase,
                temporal_scope="day",
                confidence=0.55,
                source=_SOURCE_SEMANTICS,
                metadata={"reason": "introspective_content"},
            )
    return None


def _check_conversation_context(
    text: str, context: str
) -> ClassificationResult | None:
    """Tier 4: check if message continues a reflective conversation."""
    # If the message uses continuation language in a reflective context
    for pattern in _CONVERSATION_CONTINUATION_PATTERNS:
        if pattern.search(text):
            # Also check that the context shows reflective content
            if _has_introspective_content(context):
                return ClassificationResult(
                    phase="freeform",
                    temporal_scope="instant",
                    confidence=0.45,
                    source=_SOURCE_CONVERSATION,
                    metadata={"reason": "continuation_in_reflective_context"},
                )
    # If the context itself is reflective, even without continuation cues
    if _has_introspective_content(context):
        for pattern in _INTROSPECTIVE_PATTERNS:
            if pattern.search(text):
                return ClassificationResult(
                    phase="freeform",
                    temporal_scope="instant",
                    confidence=0.50,
                    source=_SOURCE_CONVERSATION,
                    metadata={"reason": "reflective_context_match"},
                )
    return None


def _has_introspective_content(text: str) -> bool:
    """Check if text contains introspective patterns."""
    for pattern in _INTROSPECTIVE_PATTERNS:
        if pattern.search(text):
            return True
    for pattern, _ in _EXPLICIT_PHASE_PATTERNS:
        if pattern.search(text):
            return True
    return False


def _classify_by_time(
    text: str, local_dt: datetime
) -> ClassificationResult:
    """Tier 5: time-of-day hints (weakest signal).

    Only fires when nothing else has matched.  Time alone never overrides
    a negative classification.
    """
    hour = local_dt.hour
    phase = _HOUR_PHASE_MAP.get(hour, "freeform")
    scope = _HOUR_SCOPE_MAP.get(hour, "day")

    return ClassificationResult(
        phase=phase,
        temporal_scope=scope,
        confidence=0.2,
        source=_SOURCE_TIME,
        metadata={
            "reason": "time_of_day",
            "hour": hour,
            "tz_offset": (
                local_dt.strftime("%z") if local_dt.tzinfo else None
            ),
        },
    )
