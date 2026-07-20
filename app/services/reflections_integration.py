"""Reflection capture integration — wires session manager into the inbound path.

This module is the single seam where reflection classification, session
attachment, and storage converge.  It is called from the burst-completion
handler in ``main.py`` (alongside ``run_agentic_turn``) so that:

* User text and voice transcripts flow through the **same** ingress path
  (both arrive as message IDs in a burst after the coalescer fires).
* Existing burst behaviour is NOT duplicated — this is a side-effect
  attached to the existing ``on_burst_complete`` callback.
* Non-reflection pacing is unchanged — the agentic turn still fires
  regardless of whether a reflection session was opened/attached.

Architecture
------------
::

    process_inbound()           (inbound.py)
        │
        ▼
    BurstCoalescer.add()        (debouncer.py)
        │
        ▼
    on_burst_complete()         (main.py)
        ├── capture_burst_for_reflection()   ← THIS MODULE
        └── run_agentic_turn()               ← existing, unchanged
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.reflections.classifier import ClassificationResult, classify_message
from app.reflections.session_manager import (
    ActiveSessionSnapshot,
    SessionManager,
)
from app.services.reflections import ReflectionStore

logger = logging.getLogger(__name__)

# Singleton — instantiated once per process; stateless.
_session_manager = SessionManager()


# ── Public API ──────────────────────────────────────────────────────────────


async def capture_burst_for_reflection(
    pool: Any,
    message_ids: list[UUID],
    user: Any,
    *,
    bot_id: str,
    topic_id: UUID | None = None,
) -> None:
    """Evaluate a burst and open/attach a reflection session when appropriate.

    Called immediately before (or alongside) ``run_agentic_turn`` in the
    ``on_burst_complete`` handler.  This function:

    1. Fetches message content from the database for every message in the
       burst.
    2. Classifies each message using the locked precedence policy in
       ``classify_message()``.
    3. Checks for an active collecting session via ``ReflectionStore``.
    4. Uses ``SessionManager.evaluate_burst_attachment()`` to decide
       whether to open a new session, attach to an existing one, or skip.
    5. Persists the decision through ``ReflectionStore.open_or_attach_session()``.

    Failures in this function are logged but **never** propagated — they
    must not block the agentic turn or affect ordinary messaging.

    Args:
        pool: Database pool (asyncpg or FakePool).
        message_ids: Ordered list of message UUIDs in the burst.
        user: User model instance (must have ``.id``, ``.timezone`` attrs).
        bot_id: The bot that received the messages.
        topic_id: Optional topic context for scoped sessions.
    """
    if not message_ids:
        return

    user_id: UUID = user.id
    user_tz: str = getattr(user, "timezone", None) or "UTC"

    # ── Step 1: fetch message content ──────────────────────────────────
    contents: list[tuple[UUID, str | None]] = []
    try:
        contents = await _fetch_message_contents(pool, message_ids)
    except Exception:
        logger.warning(
            "capture_burst_for_reflection: failed to fetch message contents; "
            "skipping reflection capture for burst",
            exc_info=True,
            extra={"user_id": str(user_id), "bot_id": bot_id},
        )
        return

    if not contents:
        return

    # ── Step 2: classify each message ───────────────────────────────────
    classifications: list[tuple[UUID, ClassificationResult]] = []
    for mid, content in contents:
        text = content or ""
        cr = classify_message(
            text,
            active_session_exists=False,  # checked next step
        )
        classifications.append((mid, cr))

    # ── Step 3: check for active collecting session ────────────────────
    active_session: ActiveSessionSnapshot | None = None
    try:
        store = ReflectionStore(pool)
        sessions = await store.list_sessions(
            user_id=user_id,
            statuses=["collecting"],
            limit=5,
        )
        for s in sessions:
            if s.bot_id == bot_id:
                active_session = ActiveSessionSnapshot(
                    session_id=s.id,
                    user_id=s.user_id,
                    bot_id=s.bot_id,
                    topic_id=s.topic_id,
                    source_message_ids=list(s.source_message_ids),
                    temporal_scope=s.temporal_scope,
                    phase=s.phase,
                )
                break
    except Exception:
        logger.warning(
            "capture_burst_for_reflection: failed to list sessions; "
            "proceeding without active-session context",
            exc_info=True,
            extra={"user_id": str(user_id), "bot_id": bot_id},
        )

    # ── Step 4: evaluate burst attachment ──────────────────────────────
    attachment = _session_manager.evaluate_burst_attachment(
        classifications=classifications,
        active_session=active_session,
        topic_id=topic_id,
    )

    if attachment.action == "skip":
        logger.debug(
            "capture_burst_for_reflection: burst skipped — %s",
            attachment.reason,
        )
        return

    # ── Step 5: open or attach ─────────────────────────────────────────
    first_candidate = None
    for mid, cr in classifications:
        if attachment.is_reflection_message and cr.confidence >= 0.3:
            first_candidate = (mid, cr)
            break

    if first_candidate is None and attachment.action == "open":
        # Degenerate: evaluate_burst_attachment said "open" but no candidate
        # found.  Use the first message as a fallback trigger.
        first_candidate = classifications[0]

    try:
        opened_by_message_id = first_candidate[0] if first_candidate else None
        cr_for_session = first_candidate[1] if first_candidate else None

        await store.open_or_attach_session(
            user_id=user_id,
            bot_id=bot_id,
            template_key="freeform_reflection",
            temporal_scope=(
                cr_for_session.temporal_scope if cr_for_session else "instant"
            ),
            phase=cr_for_session.phase if cr_for_session else "freeform",
            topic_id=topic_id,
            opened_by_message_id=opened_by_message_id,
            source_message_ids=attachment.merged_source_ids,
            classification_source=(
                cr_for_session.source if cr_for_session else "freeform_fallback"
            ),
            classification_confidence=(
                cr_for_session.confidence if cr_for_session else 0.3
            ),
            classification_metadata=(
                cr_for_session.metadata if cr_for_session else None
            ),
        )
        logger.info(
            "capture_burst_for_reflection: %s session for user=%s bot=%s "
            "messages=%d",
            "attached to" if attachment.action == "attach" else "opened",
            user_id,
            bot_id,
            len(attachment.merged_source_ids),
        )
    except Exception:
        logger.warning(
            "capture_burst_for_reflection: open_or_attach_session failed; "
            "reflection capture skipped for this burst",
            exc_info=True,
            extra={"user_id": str(user_id), "bot_id": bot_id},
        )


# ── Internal helpers ────────────────────────────────────────────────────────


async def _fetch_message_contents(
    pool: Any,
    message_ids: list[UUID],
) -> list[tuple[UUID, str | None]]:
    """Fetch content for a list of message IDs, preserving order.

    Returns a list of ``(message_id, content)`` tuples.  Messages that
    cannot be found are omitted (no error is raised).
    """
    if not message_ids:
        return []

    # Build a parameterised query with ANY for performance.
    rows = await pool.fetch(
        """
        SELECT id, content
        FROM messages
        WHERE id = ANY($1::uuid[])
        """,
        message_ids,
    )

    # Build a lookup map, then preserve input order.
    content_map: dict[UUID, str | None] = {}
    for row in rows:
        rid = row["id"]
        if isinstance(rid, UUID):
            content_map[rid] = row.get("content")

    result: list[tuple[UUID, str | None]] = []
    for mid in message_ids:
        if mid in content_map:
            result.append((mid, content_map[mid]))

    return result
