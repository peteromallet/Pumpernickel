"""Tests for the live-session synthesis (Sprint 3b)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from app.services.live.synthesis import synthesize_review


class _SynthFakePool:
    """asyncpg-shaped stand-in just for synthesize_review."""

    def __init__(self) -> None:
        self.conversation: dict[str, Any] | None = None
        self.items: list[dict[str, Any]] = []
        self.turns: list[dict[str, Any]] = []
        self.notes: list[dict[str, Any]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM mediator.conversations" in sql:
            return self.conversation
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM mediator.conversation_items" in sql:
            return self.items
        if "FROM mediator.transcript_turns" in sql:
            return self.turns
        if "FROM mediator.conversation_notes" in sql:
            return self.notes
        return []


@pytest.mark.anyio
async def test_synthesize_review_empty_when_no_conversation() -> None:
    pool = _SynthFakePool()
    result = await synthesize_review(pool, uuid4())
    assert result["is_empty"] is True
    assert result["what_heard"] == []
    assert result["what_decided"] == []


@pytest.mark.anyio
async def test_synthesize_review_buckets_artifacts() -> None:
    sid = uuid4()
    item_a = uuid4()
    item_b = uuid4()
    note_a = uuid4()
    note_b = uuid4()

    pool = _SynthFakePool()
    pool.conversation = {
        "id": sid,
        "bot_id": "tante_rosi",
        "mode": "steered",
        "status": "review_pending",
        "prep_summary": "Stub prep",
        "started_at": datetime.now(timezone.utc),
        "ended_at": datetime.now(timezone.utc),
        "session_fields": {},
    }
    pool.items = [
        {
            "id": item_a,
            "title": "Open with what's on your mind",
            "status": "covered",
            "priority": "must",
            "kind": "planned",
            "coverage_summary": "User shared the timing concern.",
            "coverage_evidence_quote": "I'm worried about the timing.",
            "intent": "Set focus.",
        },
        {
            "id": item_b,
            "title": "Still pending",
            "status": "pending",
            "priority": "should",
            "kind": "planned",
            "coverage_summary": None,
            "coverage_evidence_quote": None,
            "intent": "Talk through the obstacles.",
        },
    ]
    pool.turns = [
        {
            "speaker_role": "primary",
            "text": "I'm worried about the timing for the conversation tonight.",
            "ts": datetime.now(timezone.utc),
        },
        {
            "speaker_role": "bot",
            "text": "Thanks for sharing that.",
            "ts": datetime.now(timezone.utc),
        },
    ]
    pool.notes = [
        {
            "id": note_a,
            "text": "[fact] User scheduled a follow-up.",
            "attributed_to_speaker": "primary",
            "created_at": datetime.now(timezone.utc),
        },
        {
            "id": note_b,
            "text": "Free-form note without a kind prefix",
            "attributed_to_speaker": None,
            "created_at": datetime.now(timezone.utc),
        },
    ]

    result = await synthesize_review(pool, sid)
    assert result["is_empty"] is False
    assert result["bot_id"] == "tante_rosi"
    # "what_heard" only includes primary-role turns (the bot turn is filtered).
    assert any("timing" in line for line in result["what_heard"])
    assert all("Thanks for sharing" not in line for line in result["what_heard"])
    # what_decided contains the covered item with evidence + summary.
    decided_titles = [it["title"] for it in result["what_decided"]]
    assert "Open with what's on your mind" in decided_titles
    # still_open contains the pending item.
    open_titles = [it["title"] for it in result["still_open"]]
    assert "Still pending" in open_titles
    # what_to_remember surfaces kinds correctly.
    note_kinds = [n["kind"] for n in result["what_to_remember"]]
    assert "fact" in note_kinds
    # Note without a [kind] prefix gets the default kind "fact".
    assert note_kinds.count("fact") == 2
