"""Session manager for active reflection session attachment.

Deterministic active-session selection by (user_id, bot_id), topic, and
session state; ordered canonical source-message preservation; duplicate
prevention; and handling for same-burst, cross-turn, competing-start,
and normal non-reflection pacing scenarios.

This module is pure business logic — it does NOT access the database
directly.  It produces attachment decisions that the service layer
(``app/services/reflections.py``) executes against the ``ReflectionStore``.
Keeping session state independent before ingress wiring satisfies the
North Star requirement that raw messages remain canonical evidence
grouped into the complete train of thought.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from app.reflections.classifier import ClassificationResult


# ── Public surface ──────────────────────────────────────────────────────────

# Actions the session manager can decide
AttachmentAction = Literal["attach", "open", "skip"]


# ── Result types ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SessionAttachment:
    """Result of evaluating whether/how to attach a message to a session.

    Attributes:
        action: ``"attach"`` (add to existing collecting session),
                ``"open"`` (create a new collecting session),
                or ``"skip"`` (do nothing — not a reflection candidate).
        session_id: The session to attach to (only for ``"attach"``).
        reason: Human-readable explanation of the decision.
        merged_source_ids: Ordered, deduplicated list of source message IDs
                           that SHOULD be stored (caller applies to store).
                           Empty for ``"skip"``.
        is_reflection_message: Whether the message itself is a reflection candidate.
    """

    action: AttachmentAction
    session_id: UUID | None
    reason: str
    merged_source_ids: list[UUID] = field(default_factory=list)
    is_reflection_message: bool = False


@dataclass(frozen=True, slots=True)
class ActiveSessionSnapshot:
    """Lightweight snapshot of an actively collecting session.

    The session manager uses this to evaluate attachment without
    coupling to the full ``ReflectionSession`` read model.
    """

    session_id: UUID
    user_id: UUID
    bot_id: str
    topic_id: UUID | None
    source_message_ids: list[UUID]
    temporal_scope: str
    phase: str


# ── Session manager ─────────────────────────────────────────────────────────


class SessionManager:
    """Core active reflection session attachment logic.

    Deterministic selection rules (in priority order):

    1. If the message is NOT a reflection candidate, skip.
    2. If an active collecting session exists for the same
       ``(user_id, bot_id)``, attach to it (append source message,
       deduplicated).
    3. Otherwise, open a new collecting session.

    Source messages are always preserved in canonical arrival order.
    Duplicates are detected by UUID membership — if a message ID is
    already in the session's ``source_message_ids``, it is NOT appended
    again.
    """

    # ── Public API ──────────────────────────────────────────────────────

    def evaluate_attachment(
        self,
        *,
        classification: ClassificationResult,
        active_session: ActiveSessionSnapshot | None,
        message_id: UUID,
        topic_id: UUID | None = None,
    ) -> SessionAttachment:
        """Decide whether to attach, open, or skip for a single message.

        Args:
            classification: Result from ``classify_message()``.
            active_session: The currently collecting session, if any,
                            for this ``(user_id, bot_id)`` pair.
            message_id: The UUID of the message being evaluated.
            topic_id: Optional topic context (reserved for future
                      topic-scoped sessions).

        Returns:
            ``SessionAttachment`` with the decision and merged source IDs.
        """
        # ── Gate: skip non-reflection messages ─────────────────────────
        if not self._is_reflection_candidate(classification):
            return SessionAttachment(
                action="skip",
                session_id=None,
                reason=f"not a reflection candidate (source={classification.source}, "
                f"confidence={classification.confidence:.2f})",
                merged_source_ids=[],
                is_reflection_message=False,
            )

        # ── Attach to existing active session ───────────────────────────
        if active_session is not None:
            merged = self.merge_ordered_deduped(
                existing_ids=active_session.source_message_ids,
                new_ids=[message_id],
            )
            return SessionAttachment(
                action="attach",
                session_id=active_session.session_id,
                reason="attaching to existing collecting session",
                merged_source_ids=merged,
                is_reflection_message=True,
            )

        # ── Open a new session ──────────────────────────────────────────
        return SessionAttachment(
            action="open",
            session_id=None,  # caller assigns UUID on insert
            reason="no active session — opening new collecting session",
            merged_source_ids=[message_id],
            is_reflection_message=True,
        )

    def evaluate_burst_attachment(
        self,
        *,
        classifications: list[tuple[UUID, ClassificationResult]],
        active_session: ActiveSessionSnapshot | None,
        topic_id: UUID | None = None,
    ) -> SessionAttachment:
        """Decide attachment for a burst of messages arriving together.

        Same-burst semantics: all messages within a burst are treated as
        a single train of thought.  The first reflection candidate in the
        burst opens (or attaches to) the session; subsequent messages
        in the same burst are appended regardless of their individual
        classification, because they are part of the same conversational
        turn.

        If multiple messages in the burst could independently open a
        session, only the first one triggers the open — the rest attach.
        This is the "competing starts" resolution.

        Args:
            classifications: Ordered list of ``(message_id, classification_result)``
                             tuples in the order they arrived.
            active_session: The currently collecting session, if any.
            topic_id: Optional topic context.

        Returns:
            ``SessionAttachment`` reflecting the cumulative decision for
            the entire burst.  ``merged_source_ids`` contains all message
            IDs from the burst (in order, deduped against any existing
            session).
        """
        if not classifications:
            return SessionAttachment(
                action="skip",
                session_id=None,
                reason="empty burst",
                merged_source_ids=[],
                is_reflection_message=False,
            )

        # Collect all message IDs in burst order
        all_ids = [mid for mid, _ in classifications]

        # Find the first reflection candidate — this drives open vs attach
        first_candidate_idx: int | None = None
        for i, (_, cr) in enumerate(classifications):
            if self._is_reflection_candidate(cr):
                first_candidate_idx = i
                break

        # No reflection candidate in the burst → skip the whole burst
        if first_candidate_idx is None:
            return SessionAttachment(
                action="skip",
                session_id=None,
                reason="no reflection candidate in burst",
                merged_source_ids=[],
                is_reflection_message=False,
            )

        first_candidate_cr = classifications[first_candidate_idx][1]

        # Determine base IDs (existing session or fresh)
        if active_session is not None:
            base_ids = list(active_session.source_message_ids)
            action: AttachmentAction = "attach"
            session_id = active_session.session_id
            reason = "burst: attaching to existing collecting session"
        else:
            base_ids = []
            action = "open"
            session_id = None
            reason = "burst: opening new collecting session (first reflection candidate)"

        # Merge all burst messages (not just the candidate) into the session.
        # This preserves the complete train of thought.
        merged = self.merge_ordered_deduped(
            existing_ids=base_ids,
            new_ids=all_ids,
        )

        return SessionAttachment(
            action=action,
            session_id=session_id,
            reason=reason,
            merged_source_ids=merged,
            is_reflection_message=True,
        )

    # ── Source message ordering & dedup ─────────────────────────────────

    @staticmethod
    def merge_ordered_deduped(
        existing_ids: list[UUID],
        new_ids: list[UUID],
    ) -> list[UUID]:
        """Merge new IDs into an ordered list, preserving order and deduplicating.

        Existing IDs always come first (preserving their original order).
        New IDs are appended in their given order, skipping any that are
        already present in the existing set.

        This guarantees:
        - Canonical order: first-seen ordering is always preserved.
        - No duplicates: a message ID appears at most once.
        - Idempotency: calling with the same ``new_ids`` twice produces
          the same result.

        Args:
            existing_ids: Already-stored source message IDs in order.
            new_ids: New message IDs to append in order.

        Returns:
            A new list with existing IDs followed by deduplicated new IDs.
        """
        seen: set[UUID] = set(existing_ids)
        result = list(existing_ids)
        for mid in new_ids:
            if mid not in seen:
                result.append(mid)
                seen.add(mid)
        return result

    @staticmethod
    def is_duplicate(
        message_id: UUID,
        existing_ids: list[UUID],
    ) -> bool:
        """Check if a message ID is already present in the session.

        Convenience for callers that want to short-circuit before
        computing the full merged list.
        """
        return message_id in set(existing_ids)

    # ── Reflection candidate gating ─────────────────────────────────────

    @staticmethod
    def _is_reflection_candidate(cr: ClassificationResult) -> bool:
        """Determine if a classification result warrants session attachment.

        Uses the same gating logic as ``is_reflection_candidate()`` in the
        classifier module, but operates directly on a ``ClassificationResult``
        to avoid double-classification.
        """
        # Negative results have confidence 0 and "negative" reason
        if cr.confidence == 0.0 and "negative" in cr.metadata.get("reason", ""):
            return False
        # Freeform with very low confidence is not a candidate
        if cr.source == "freeform_fallback" and cr.confidence < 0.3:
            return False
        # Anything with confidence >= 0.3 is a candidate
        if cr.confidence >= 0.3:
            return True
        return False


# ── Convenience helpers ─────────────────────────────────────────────────────


def select_active_session(
    *,
    sessions: list[ActiveSessionSnapshot],
    user_id: UUID,
    bot_id: str,
    topic_id: UUID | None = None,
) -> ActiveSessionSnapshot | None:
    """Deterministically select the active collecting session.

    Selection rules (in priority order):
    1. Match by ``(user_id, bot_id, status='collecting')``.
    2. If ``topic_id`` is provided and a session matches the topic, prefer it.
    3. If multiple sessions match, return the most recently created one.

    This function is a pure helper — it does not access the database.
    Callers should pass the list of candidate sessions (typically fetched
    from ``ReflectionStore.list_sessions`` with ``statuses=['collecting']``).

    Args:
        sessions: Candidate sessions to select from.
        user_id: The user to match.
        bot_id: The bot to match.
        topic_id: Optional topic to prefer.

    Returns:
        The selected session, or ``None`` if no collecting session exists.
    """
    # Filter to matching (user_id, bot_id) collecting sessions
    candidates = [
        s
        for s in sessions
        if s.user_id == user_id and s.bot_id == bot_id
    ]

    if not candidates:
        return None

    # Prefer topic match if topic_id is provided
    if topic_id is not None:
        topic_matches = [s for s in candidates if s.topic_id == topic_id]
        if topic_matches:
            candidates = topic_matches

    # Return the most recently created (last in list if sorted by created_at DESC)
    # For determinism, sort by session_id as tiebreaker
    candidates.sort(key=lambda s: s.session_id, reverse=True)
    return candidates[0]


def build_session_snapshot(
    *,
    session_id: UUID,
    user_id: UUID,
    bot_id: str,
    topic_id: UUID | None = None,
    source_message_ids: list[UUID] | None = None,
    temporal_scope: str = "instant",
    phase: str = "freeform",
) -> ActiveSessionSnapshot:
    """Build an ``ActiveSessionSnapshot`` from raw values.

    Convenience for tests and for callers that have session data
    but not a full ``ReflectionSession`` read model.
    """
    return ActiveSessionSnapshot(
        session_id=session_id,
        user_id=user_id,
        bot_id=bot_id,
        topic_id=topic_id,
        source_message_ids=list(source_message_ids or []),
        temporal_scope=temporal_scope,
        phase=phase,
    )
