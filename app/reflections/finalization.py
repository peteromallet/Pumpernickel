"""Deterministic finalization rules for reflection sessions.

Encodes the business logic for when and how a collecting reflection session
should transition to finalizing (or abandoned).  This module is pure logic —
it does NOT access the database directly.  It produces *finalization decisions*
that the service layer (``app/services/reflections.py``) executes against the
``ReflectionStore``.

Finalization scenarios
----------------------
* **Explicit completion**: User sends a message that clearly ends the
  reflection (e.g. "end reflection", "wrap it up", "that's all").
* **Clear topic transition**: User starts a new topic while a collecting
  session exists — the current session should be finalized so the new
  topic gets its own session.
* **Race-safe inactivity**: A collecting session whose ``idle_finalize_at``
  has passed should be auto-finalized.  Only one caller wins the race
  (the store-level ``status = 'collecting'`` filter guarantees it).
* **Late messages**: Messages that arrive for a session that is already
  finalized, abandoned, or processed should NOT re-attach.  They should
  open a new session (if reflective) or be skipped.
* **Abandoned sessions**: Sessions that have been collecting for an
  extended period without crossing the idle threshold can be explicitly
  abandoned by user action or by a sweeper.
* **Retry idempotency**: Calling finalize on an already-finalized session
  returns the current state instead of raising — it is safe to call
  finalize multiple times.

Design contract
---------------
* All functions are pure — no side effects, no I/O.
* Datetime comparisons use timezone-aware datetimes exclusively.
* The module does not mutate session rows; it computes ``FinalizationDecision``
  values that the caller applies atomically.
* Idempotency: the ``finalize_session`` decision is safe to compute
  even when the session is already in a terminal state.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID


# ── Public surface ──────────────────────────────────────────────────────────

# Finalization actions the rule engine can decide
FinalizationAction = Literal[
    "finalize",               # Transition collecting → finalizing
    "abandon",                # Transition collecting → abandoned
    "noop",                   # Do nothing (not ready / already terminal)
    "skip_late",              # Message arrived after session was closed
    "open_new_for_late",      # Late message — open a new session instead
]


# ── Decision types ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FinalizationDecision:
    """Result of evaluating whether a session should be finalized.

    Attributes:
        action: What should happen to the session.
        reason: Human-readable explanation.
        session_id: The session this decision applies to (if any).
        finalize_at: When finalization should occur (for auto-finalization).
        new_session_trigger: If ``action == "open_new_for_late"``, metadata
            about the message that triggered the new session.
    """

    action: FinalizationAction
    reason: str
    session_id: UUID | None = None
    finalize_at: datetime | None = None
    new_session_trigger: dict | None = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionState:
    """Lightweight snapshot of a reflection session for finalization decisions.

    Attributes:
        session_id: Session UUID.
        user_id: Owning user.
        bot_id: Bot identifier.
        status: Current session status (collecting, finalizing, etc.).
        source_message_ids: Ordered list of attached message IDs.
        opened_at: When the session was created.
        idle_finalize_at: When idle-finalization triggers (if set).
        finalized_at: When the session was finalized (if applicable).
        abandoned_at: When the session was abandoned (if applicable).
        topic_id: Optional topic.
        phase: Classification phase.
    """

    session_id: UUID
    user_id: UUID
    bot_id: str
    status: str
    source_message_ids: list[UUID] = field(default_factory=list)
    opened_at: datetime | None = None
    idle_finalize_at: datetime | None = None
    finalized_at: datetime | None = None
    abandoned_at: datetime | None = None
    topic_id: UUID | None = None
    phase: str = "freeform"


# ── Explicit completion detection ───────────────────────────────────────────

# Patterns that signal the user wants to end the current reflection.
_EXPLICIT_END_PATTERNS: list[re.Pattern[str]] = [
    # Action word + reflection-specific target: "end reflection", "finish session" etc.
    re.compile(
        r"\b(end|finish|wrap\s*(it\s*)?up|close|stop|done|complete)\s*"
        r"(reflection|reflecting|session|check[- ]?in)"
        r"\b", re.IGNORECASE
    ),
    # "end/finish/close/wrap up this [reflection/session]" — any position:
    # "wrap up this reflection and move on", "close this session now"
    re.compile(
        r"\b(end|finish|close|stop|done|complete|wrap\s*(it\s*)?up)\s+this"
        r"\s+(session|reflection|reflecting)\b",
        re.IGNORECASE,
    ),
    # "end/finish/close/wrap up this" at end of message (bare "this"):
    # "end this", "close this." — but NOT "complete this task"
    re.compile(
        r"\b(end|finish|close|stop|done|complete|wrap\s*(it\s*)?up)\s+this"
        r"\s*[.!?]*\s*$",
        re.IGNORECASE,
    ),
    # "finish/close/stop here" or "finish/close/stop now"
    re.compile(
        r"\b(finish|close|stop|done|complete)\s+(here|now)\b",
        re.IGNORECASE,
    ),
    # Standalone closing phrases
    re.compile(
        r"\b(that'?s\s+(all|it|everything)|I'?m\s+done|all\s+done|"
        r"nothing\s+else|no\s+more)"
        r"\b", re.IGNORECASE
    ),
    # Bare completion word at end of message
    re.compile(
        r"\b(end|finish|wrap|close|stop|done)\s*$",
        re.IGNORECASE,
    ),
    # Explicit finalization words
    re.compile(
        r"\b(finalize|close out|sign off|signing off)\b",
        re.IGNORECASE,
    ),
]


def is_explicit_completion(text: str) -> bool:
    """Check if a message text explicitly signals session completion.

    Args:
        text: The user message content (plain text).

    Returns:
        ``True`` if the text matches an explicit completion pattern.
    """
    if not text or not text.strip():
        return False
    text_stripped = text.strip()
    for pattern in _EXPLICIT_END_PATTERNS:
        if pattern.search(text_stripped):
            return True
    return False


# ── Topic transition detection ──────────────────────────────────────────────

# Patterns that suggest the user is starting a distinctly new topic
# that warrants closing the current reflection and opening a fresh one.
_TOPIC_TRANSITION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"\b(now\s+let'?s|moving\s+on|new\s+topic|different\s+topic|"
        r"switch(?:ing)?\s+(?:gears|topics?)|change\s+(?:of\s+)?subject)"
        r"\b", re.IGNORECASE
    ),
    re.compile(
        r"\b(ok(?:ay)?\s+so|alright\s+so|anyway|so\s+about)\b",
        re.IGNORECASE,
    ),
    # "let's talk/discuss/focus (about/on) X" or "let's talk/discuss/focus X"
    re.compile(
        r"\b(let'?s\s+(?:talk|discuss|focus)\s+(?:about\s+|on\s+)?)",
        re.IGNORECASE,
    ),
]


def is_topic_transition(text: str) -> bool:
    """Check if a message suggests a clear topic transition.

    This does NOT use semantic similarity — it only detects explicit
    topic-shift language.  The caller is responsible for determining
    whether the new topic actually differs from the current session's
    phase/context.

    Args:
        text: The user message content.

    Returns:
        ``True`` if the text contains topic-transition signals.
    """
    if not text or not text.strip():
        return False
    text_stripped = text.strip()
    for pattern in _TOPIC_TRANSITION_PATTERNS:
        if pattern.search(text_stripped):
            return True
    return False


# ── Finalization rule engine ────────────────────────────────────────────────


class FinalizationEngine:
    """Deterministic finalization rules for reflection sessions.

    This engine evaluates a session's current state and an optional incoming
    message and decides whether the session should be finalized, abandoned,
    or left as-is.  All decisions are pure and deterministic.

    Usage::

        engine = FinalizationEngine()
        decision = engine.evaluate_explicit_completion(
            session=session_state,
            message_text="end reflection",
        )
        if decision.action == "finalize":
            await store.finalize_session(
                user_id=..., session_id=decision.session_id,
            )

    The engine is stateless — create one instance and reuse it.
    """

    # ── Configuration (overridable) ─────────────────────────────────────

    # Default idle timeout: if a session has no idle_finalize_at set,
    # auto-finalize after this many seconds of inactivity.
    DEFAULT_IDLE_TIMEOUT_SECONDS: int = 900  # 15 minutes

    # Extended inactivity threshold for abandonment (vs finalization).
    # Sessions idle beyond this are abandoned rather than finalized.
    DEFAULT_ABANDON_TIMEOUT_SECONDS: int = 86400  # 24 hours

    # Maximum number of source messages before a session is
    # force-finalized regardless of activity (safety valve).
    MAX_SOURCE_MESSAGES: int = 500

    # ── Public API ──────────────────────────────────────────────────────

    def evaluate_explicit_completion(
        self,
        *,
        session: SessionState,
        message_text: str,
    ) -> FinalizationDecision:
        """Check if a message explicitly ends the reflection.

        Args:
            session: The currently collecting session state.
            message_text: The incoming message content.

        Returns:
            ``FinalizationDecision`` — ``finalize`` if explicit completion
            is detected AND the session is still collecting, ``noop`` otherwise.
        """
        if session.status != "collecting":
            return FinalizationDecision(
                action="noop",
                reason=f"session is not collecting (status={session.status})",
                session_id=session.session_id,
            )

        if is_explicit_completion(message_text):
            return FinalizationDecision(
                action="finalize",
                reason="explicit completion signal detected in message",
                session_id=session.session_id,
                finalize_at=datetime.now(timezone.utc),
            )

        return FinalizationDecision(
            action="noop",
            reason="no explicit completion signal",
            session_id=session.session_id,
        )

    def evaluate_topic_transition(
        self,
        *,
        session: SessionState,
        message_text: str,
        new_topic_id: UUID | None = None,
    ) -> FinalizationDecision:
        """Check if a message transitions away from the current topic.

        If the user signals a topic shift AND the current session is
        collecting, finalize the current session so the new topic gets
        a fresh one.

        Args:
            session: The currently collecting session state.
            message_text: The incoming message content.
            new_topic_id: Optional explicit new topic (e.g. from channel).

        Returns:
            ``FinalizationDecision``.
        """
        if session.status != "collecting":
            return FinalizationDecision(
                action="noop",
                reason=f"session is not collecting (status={session.status})",
                session_id=session.session_id,
            )

        # Explicit topic change via channel/topic_id
        if new_topic_id is not None and session.topic_id is not None:
            if new_topic_id != session.topic_id:
                return FinalizationDecision(
                    action="finalize",
                    reason=(
                        f"topic transition: session topic {session.topic_id} "
                        f"→ new topic {new_topic_id}"
                    ),
                    session_id=session.session_id,
                    finalize_at=datetime.now(timezone.utc),
                )

        # Text-based topic transition signal
        if is_topic_transition(message_text):
            return FinalizationDecision(
                action="finalize",
                reason="topic transition signal detected in message",
                session_id=session.session_id,
                finalize_at=datetime.now(timezone.utc),
            )

        return FinalizationDecision(
            action="noop",
            reason="no topic transition detected",
            session_id=session.session_id,
        )

    def evaluate_inactivity(
        self,
        *,
        session: SessionState,
        now: datetime | None = None,
    ) -> FinalizationDecision:
        """Evaluate whether a session should be auto-finalized due to inactivity.

        This is the race-safe inactivity check.  Only sessions that are
        still in ``collecting`` status AND have an ``idle_finalize_at``
        that has passed are eligible.  The store-level ``WHERE status =
        'collecting'`` filter provides the race safety.

        Args:
            session: The session to evaluate.
            now: Reference datetime (default: now UTC).

        Returns:
            ``finalize`` if idle timeout has passed, ``abandon`` if the
            session has been idle beyond the abandon threshold, ``noop``
            otherwise.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        if session.status != "collecting":
            return FinalizationDecision(
                action="noop",
                reason=f"session is not collecting (status={session.status})",
                session_id=session.session_id,
            )

        # Determine the effective idle deadline.
        deadline = session.idle_finalize_at
        if deadline is None:
            # No explicit idle_finalize_at set.  Use opened_at + default.
            if session.opened_at is not None:
                deadline = session.opened_at + timedelta(
                    seconds=self.DEFAULT_IDLE_TIMEOUT_SECONDS
                )
            else:
                # Can't determine — skip.
                return FinalizationDecision(
                    action="noop",
                    reason="no idle_finalize_at or opened_at — cannot compute deadline",
                    session_id=session.session_id,
                )

        if now < deadline:
            return FinalizationDecision(
                action="noop",
                reason=f"idle deadline not yet reached (deadline={deadline.isoformat()})",
                session_id=session.session_id,
            )

        # Deadline has passed.  Check if it's abandonment territory.
        abandon_deadline = deadline + timedelta(
            seconds=self.DEFAULT_ABANDON_TIMEOUT_SECONDS
        )
        if now >= abandon_deadline:
            return FinalizationDecision(
                action="abandon",
                reason=(
                    f"session idle beyond abandon threshold "
                    f"(deadline={deadline.isoformat()}, now={now.isoformat()})"
                ),
                session_id=session.session_id,
                finalize_at=now,
            )

        return FinalizationDecision(
            action="finalize",
            reason=f"idle deadline passed (deadline={deadline.isoformat()})",
            session_id=session.session_id,
            finalize_at=now,
        )

    def evaluate_late_message(
        self,
        *,
        session: SessionState,
        message_text: str,
        is_reflection_candidate: bool = False,
    ) -> FinalizationDecision:
        """Decide what to do with a message arriving for a non-collecting session.

        Late messages are messages that arrive after a session has already
        been finalized, abandoned, or processed.  They should NOT be attached
        to the closed session.

        Args:
            session: The session state (non-collecting).
            message_text: The incoming message content.
            is_reflection_candidate: Whether the message itself is a
                reflection candidate (per the classifier).

        Returns:
            ``skip_late`` if the message should be ignored for the closed
            session, ``open_new_for_late`` if a new collecting session
            should be opened for this message, ``noop`` if session is
            still collecting.
        """
        if session.status == "collecting":
            return FinalizationDecision(
                action="noop",
                reason="session is still collecting — not a late message",
                session_id=session.session_id,
            )

        if is_reflection_candidate:
            return FinalizationDecision(
                action="open_new_for_late",
                reason=(
                    f"message is a reflection candidate but session "
                    f"{session.session_id} is {session.status} — "
                    f"should open new session"
                ),
                session_id=session.session_id,
                new_session_trigger={"message_text": message_text[:200]},
            )

        return FinalizationDecision(
            action="skip_late",
            reason=(
                f"message is not a reflection candidate and session "
                f"{session.session_id} is {session.status}"
            ),
            session_id=session.session_id,
        )

    def evaluate_abandon(
        self,
        *,
        session: SessionState,
        now: datetime | None = None,
        force: bool = False,
    ) -> FinalizationDecision:
        """Decide whether to abandon a session.

        Abandonment is appropriate when:
        - The user explicitly abandons (force=True).
        - The session has been idle for an extremely long time
          (beyond the abandon threshold).
        - The session has exceeded MAX_SOURCE_MESSAGES (safety valve).

        Args:
            session: The session to evaluate.
            now: Reference datetime.
            force: If True, always recommend abandonment (user-requested).

        Returns:
            ``abandon`` or ``noop``.
        """
        if force:
            if session.status != "collecting":
                return FinalizationDecision(
                    action="noop",
                    reason=f"cannot abandon: session status is {session.status}",
                    session_id=session.session_id,
                )
            return FinalizationDecision(
                action="abandon",
                reason="forced abandonment requested",
                session_id=session.session_id,
            )

        if session.status != "collecting":
            return FinalizationDecision(
                action="noop",
                reason=f"session is not collecting (status={session.status})",
                session_id=session.session_id,
            )

        if now is None:
            now = datetime.now(timezone.utc)

        # Safety valve: too many messages
        if len(session.source_message_ids) >= self.MAX_SOURCE_MESSAGES:
            return FinalizationDecision(
                action="abandon",
                reason=(
                    f"session has {len(session.source_message_ids)} messages "
                    f"(max={self.MAX_SOURCE_MESSAGES})"
                ),
                session_id=session.session_id,
                finalize_at=now,
            )

        # Extended inactivity check
        if session.idle_finalize_at is not None:
            abandon_deadline = session.idle_finalize_at + timedelta(
                seconds=self.DEFAULT_ABANDON_TIMEOUT_SECONDS
            )
            if now >= abandon_deadline:
                return FinalizationDecision(
                    action="abandon",
                    reason=(
                        f"session idle beyond abandon threshold "
                        f"(idle_finalize_at={session.idle_finalize_at.isoformat()}, "
                        f"now={now.isoformat()})"
                    ),
                    session_id=session.session_id,
                    finalize_at=now,
                )

        return FinalizationDecision(
            action="noop",
            reason="session is active — not eligible for abandonment",
            session_id=session.session_id,
        )

    # ── Composite evaluation ────────────────────────────────────────────

    def evaluate_full(
        self,
        *,
        session: SessionState,
        message_text: str | None = None,
        is_reflection_candidate: bool = False,
        new_topic_id: UUID | None = None,
        now: datetime | None = None,
    ) -> FinalizationDecision:
        """Run all finalization checks in priority order.

        Priority (first match wins):
        1. If session is not collecting → late-message evaluation.
        2. Explicit completion detection.
        3. Topic transition detection.
        4. Force-check (max messages safety valve).
        5. Inactivity timeout.
        6. Default: noop.

        Args:
            session: The session to evaluate.
            message_text: Optional incoming message content.
            is_reflection_candidate: Whether the message is a reflection
                candidate (used in late-message handling).
            new_topic_id: Optional explicit new topic.
            now: Reference datetime.

        Returns:
            ``FinalizationDecision``.
        """
        if now is None:
            now = datetime.now(timezone.utc)

        # ── Tier 1: late message check (session already closed) ─────────
        if session.status != "collecting":
            if message_text is not None:
                return self.evaluate_late_message(
                    session=session,
                    message_text=message_text,
                    is_reflection_candidate=is_reflection_candidate,
                )
            return FinalizationDecision(
                action="noop",
                reason=f"session status is {session.status} — no action needed",
                session_id=session.session_id,
            )

        # ── Tier 2: explicit completion ─────────────────────────────────
        if message_text is not None:
            decision = self.evaluate_explicit_completion(
                session=session, message_text=message_text
            )
            if decision.action == "finalize":
                return decision

        # ── Tier 3: topic transition ────────────────────────────────────
        if message_text is not None:
            decision = self.evaluate_topic_transition(
                session=session,
                message_text=message_text,
                new_topic_id=new_topic_id,
            )
            if decision.action == "finalize":
                return decision

        # ── Tier 4: safety valve (max messages) ─────────────────────────
        if len(session.source_message_ids) >= self.MAX_SOURCE_MESSAGES:
            return FinalizationDecision(
                action="abandon",
                reason=(
                    f"session exceeded max source messages "
                    f"({len(session.source_message_ids)} >= {self.MAX_SOURCE_MESSAGES})"
                ),
                session_id=session.session_id,
                finalize_at=now,
            )

        # ── Tier 5: inactivity ──────────────────────────────────────────
        decision = self.evaluate_inactivity(session=session, now=now)
        if decision.action != "noop":
            return decision

        # ── Default: nothing to do ──────────────────────────────────────
        return FinalizationDecision(
            action="noop",
            reason="session is active — no finalization trigger",
            session_id=session.session_id,
        )


# ── Idempotency helpers ─────────────────────────────────────────────────────


def is_idempotent_finalize(
    *,
    session: SessionState,
) -> bool:
    """Check if finalizing this session would be idempotent (already done).

    A session is already finalized/terminal if its status is one of:
    ``finalizing``, ``processed``, ``processing_failed``, or ``abandoned``.

    Args:
        session: The session to check.

    Returns:
        ``True`` if the session is already in a terminal state where
        re-finalization is a safe noop.
    """
    return session.status in {"finalizing", "processed", "processing_failed", "abandoned"}


def compute_idle_deadline(
    *,
    session: SessionState,
    default_timeout_seconds: int = 900,
) -> datetime | None:
    """Compute the effective idle deadline for a session.

    If ``idle_finalize_at`` is explicitly set, use it.  Otherwise,
    compute from ``opened_at`` + ``default_timeout_seconds``.

    Args:
        session: The session to compute for.
        default_timeout_seconds: Default idle timeout when no explicit
            ``idle_finalize_at`` is set.

    Returns:
        The deadline datetime, or ``None`` if neither ``idle_finalize_at``
        nor ``opened_at`` is available.
    """
    if session.idle_finalize_at is not None:
        return session.idle_finalize_at
    if session.opened_at is not None:
        return session.opened_at + timedelta(seconds=default_timeout_seconds)
    return None


# ── Session state builders ──────────────────────────────────────────────────


def build_session_state(
    *,
    session_id: UUID,
    user_id: UUID,
    bot_id: str,
    status: str = "collecting",
    source_message_ids: list[UUID] | None = None,
    opened_at: datetime | None = None,
    idle_finalize_at: datetime | None = None,
    finalized_at: datetime | None = None,
    abandoned_at: datetime | None = None,
    topic_id: UUID | None = None,
    phase: str = "freeform",
) -> SessionState:
    """Build a ``SessionState`` from raw values.

    Convenience for tests and for callers that have session data
    but not a full ``ReflectionSession`` read model.
    """
    return SessionState(
        session_id=session_id,
        user_id=user_id,
        bot_id=bot_id,
        status=status,
        source_message_ids=list(source_message_ids or []),
        opened_at=opened_at,
        idle_finalize_at=idle_finalize_at,
        finalized_at=finalized_at,
        abandoned_at=abandoned_at,
        topic_id=topic_id,
        phase=phase,
    )
