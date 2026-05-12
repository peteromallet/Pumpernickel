"""S4 T22 — hot_context topic_status render + cross-topic helper tests."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from app.services.hot_context import (
    HotContext,
    _render_with_counts,
    fetch_cross_topic_status,
    peek_other_topics,
)


def _empty_hc(topic_status: dict[str, Any] | None) -> HotContext:
    return HotContext(
        current_user={"id": uuid4(), "name": "A", "timezone": "UTC", "cross_thread_sharing_default": "opt_in"},
        partner_user={"id": uuid4(), "name": "B", "timezone": "UTC", "cross_thread_sharing_default": "opt_in"},
        conversation_load={"period": "today", "timezone": "UTC", "period_start": None, "period_end": None, "inbound_count": 0, "outbound_count": 0, "total_count": 0},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[],
        time_since_last_message=None,
        trigger_metadata={"kind": "inbound", "triggering_message_ids": [], "messages": []},
        topic_status=topic_status,
    )


def test_topic_status_block_renders_when_row_present() -> None:
    ts = {"id": uuid4(), "headline": "Repair after argument", "body": "Both apologized.", "last_updated_at": datetime(2026, 5, 12, 9, 30, tzinfo=UTC)}
    hc = _empty_hc(topic_status=ts)
    out = _render_with_counts(hc, {})
    assert "## Topic status" in out
    assert "Repair after argument" in out
    assert "Both apologized." in out
    assert "2026-05-12" in out


def test_topic_status_block_suppressed_when_no_row() -> None:
    hc = _empty_hc(topic_status=None)
    out = _render_with_counts(hc, {})
    assert "## Topic status" not in out


def test_topic_status_body_omitted_when_empty_string() -> None:
    ts = {"id": uuid4(), "headline": "Quiet day", "body": "", "last_updated_at": datetime(2026, 5, 12, tzinfo=UTC)}
    hc = _empty_hc(topic_status=ts)
    out = _render_with_counts(hc, {})
    assert "## Topic status" in out
    assert "Quiet day" in out
    # body line is "- body:" — absent when body is empty
    assert "- body:" not in out


class _StubPool:
    """Records fetch calls, returns canned rows."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.calls.append((sql, args))
        return list(self._rows)


@pytest.mark.asyncio
async def test_fetch_cross_topic_status_returns_rows_dyad_branch() -> None:
    other_topic = uuid4()
    rows = [{"id": uuid4(), "topic_id": other_topic, "headline": "h", "body": "b", "last_updated_at": datetime.now(UTC)}]
    pool = _StubPool(rows)
    out = await fetch_cross_topic_status(
        pool, dyad_id=uuid4(), user_id=uuid4(), exclude_topic_id=uuid4(), cap=5,
    )
    assert len(out) == 1
    assert out[0]["topic_id"] == other_topic


@pytest.mark.asyncio
async def test_fetch_cross_topic_status_empty_one_topic() -> None:
    pool = _StubPool([])
    out = await fetch_cross_topic_status(
        pool, dyad_id=None, user_id=uuid4(), exclude_topic_id=uuid4(), cap=5,
    )
    assert out == []


@pytest.mark.asyncio
async def test_peek_other_topics_uses_since_window() -> None:
    pool = _StubPool([])
    since = datetime.now(UTC) - timedelta(days=14)
    out = await peek_other_topics(
        pool, dyad_id=uuid4(), user_id=uuid4(), exclude_topic_id=uuid4(), since=since, cap=5,
    )
    assert out == []
    # since was passed to the underlying query
    assert pool.calls, "expected one fetch call"
    assert since in pool.calls[0][1]
