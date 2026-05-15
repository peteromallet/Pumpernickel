"""Sprint 3b — end-of-session synthesizer.

Synthesizes a 4-section review from the artifacts the live turn loop
produced:

* ``what_heard`` — short bullets summarizing the user's transcript_turns.
* ``what_decided`` — items advanced to ``status='covered'`` with a
  coverage_summary or evidence_quote.
* ``still_open`` — items still ``pending`` / ``active`` (incl. dynamic
  items the bot introduced).
* ``what_to_remember`` — conversation_notes entries flagged ``[fact]``,
  ``[open_loop]``, ``[decision]``.

The v1 synthesizer is deterministic / no-LLM so a session ending with
zero artifacts still produces a meaningful card and the e2e flow is
testable without a real key.  ``OpusSynthesizer`` is the v1.1 hook.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)


_NOTE_KIND_RE = re.compile(r"^\[(?P<kind>fact|open_loop|concern|decision)\]\s*(?P<body>.*)$")


async def synthesize_review(pool: Any, session_id: UUID) -> dict[str, Any]:
    """Pure-Python synthesis: read artifacts, bucket them, return a dict."""
    conv = await pool.fetchrow(
        """
        SELECT id, bot_id, mode, status, prep_summary, started_at, ended_at,
               session_fields
        FROM mediator.conversations
        WHERE id = $1
        """,
        session_id,
    )
    if conv is None:
        return {
            "session_id": str(session_id),
            "what_heard": [],
            "what_decided": [],
            "still_open": [],
            "what_to_remember": [],
            "is_empty": True,
        }

    items = await pool.fetch(
        """
        SELECT id, title, status, priority, kind,
               coverage_summary, coverage_evidence_quote, intent
        FROM mediator.conversation_items
        WHERE conversation_id = $1
        ORDER BY order_hint, created_at
        """,
        session_id,
    )
    turns = await pool.fetch(
        """
        SELECT speaker_role, text, ts
        FROM mediator.transcript_turns
        WHERE conversation_id = $1
        ORDER BY ts
        """,
        session_id,
    )
    notes = await pool.fetch(
        """
        SELECT id, text, attributed_to_speaker, created_at
        FROM mediator.conversation_notes
        WHERE conversation_id = $1
        ORDER BY created_at
        """,
        session_id,
    )

    # what_heard: bucket the primary user's transcript by simple sentence
    # splitting, then keep the most "signal-dense" lines (>= 6 words).
    what_heard: list[str] = []
    for t in turns:
        if t["speaker_role"] != "primary":
            continue
        for sentence in re.split(r"(?<=[.?!])\s+", (t["text"] or "").strip()):
            words = sentence.strip()
            if len(words.split()) >= 4:
                what_heard.append(words)
    # Cap to last 6 to keep the card readable.
    what_heard = what_heard[-6:]

    what_decided: list[dict[str, str]] = []
    still_open: list[dict[str, str]] = []
    for item in items:
        if item["status"] == "covered":
            what_decided.append({
                "item_id": str(item["id"]),
                "title": item["title"],
                "summary": item["coverage_summary"] or "(covered)",
                "evidence_quote": item["coverage_evidence_quote"] or "",
            })
        elif item["status"] in ("pending", "active"):
            still_open.append({
                "item_id": str(item["id"]),
                "title": item["title"],
                "priority": item["priority"],
                "intent": item["intent"] or "",
            })

    what_to_remember: list[dict[str, str]] = []
    for n in notes:
        kind = "fact"
        body = (n["text"] or "").strip()
        match = _NOTE_KIND_RE.match(body)
        if match:
            kind = match.group("kind")
            body = match.group("body").strip()
        what_to_remember.append({
            "note_id": str(n["id"]),
            "kind": kind,
            "text": body,
        })

    return {
        "session_id": str(session_id),
        "bot_id": conv["bot_id"],
        "status": conv["status"],
        "started_at": (conv["started_at"].isoformat() if conv["started_at"] else None),
        "ended_at": (conv["ended_at"].isoformat() if conv["ended_at"] else None),
        "prep_summary": conv["prep_summary"],
        "what_heard": what_heard,
        "what_decided": what_decided,
        "still_open": still_open,
        "what_to_remember": what_to_remember,
        "is_empty": not (what_heard or what_decided or still_open or what_to_remember),
    }


async def finalize_session(pool: Any, session_id: UUID) -> None:
    """Mark ``conversations.ended_at`` + flip status to ``review_pending``."""
    await pool.execute(
        """
        UPDATE mediator.conversations
        SET status = 'review_pending',
            ended_at = COALESCE(ended_at, now())
        WHERE id = $1
        """,
        session_id,
    )


async def save_review(
    pool: Any,
    session_id: UUID,
    *,
    keep_items: list[dict[str, Any]],
    keep_notes: list[dict[str, Any]],
) -> None:
    """Mark the session synthesized.

    Sprint 3b intentionally writes ONLY through the existing
    `conversation_*` tables — it does NOT write to `observations`,
    `distillations`, or `themes` yet.  That write-through lands in
    Sprint 3c once we have a real Opus synthesizer.  The frontend's
    Save still has user value (it locks the review) and any user-edited
    text gets persisted back to the source rows.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            for item in keep_items:
                if not item.get("item_id"):
                    continue
                try:
                    item_id = UUID(item["item_id"])
                except Exception:
                    continue
                summary = (item.get("summary") or "").strip() or None
                await conn.execute(
                    """
                    UPDATE mediator.conversation_items
                    SET coverage_summary = COALESCE($2, coverage_summary)
                    WHERE id = $1
                    """,
                    item_id,
                    summary,
                )
            for note in keep_notes:
                if not note.get("note_id"):
                    continue
                try:
                    note_id = UUID(note["note_id"])
                except Exception:
                    continue
                text = (note.get("text") or "").strip()
                if not text:
                    await conn.execute(
                        "DELETE FROM mediator.conversation_notes WHERE id = $1",
                        note_id,
                    )
                else:
                    await conn.execute(
                        "UPDATE mediator.conversation_notes SET text = $2 WHERE id = $1",
                        note_id,
                        text,
                    )
            await conn.execute(
                """
                UPDATE mediator.conversations
                SET status = 'synthesized'
                WHERE id = $1
                """,
                session_id,
            )
