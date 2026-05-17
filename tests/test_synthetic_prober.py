"""Unit tests for ``scripts/synthetic_prober.py``.

The tests exercise the prober against a tiny in-memory stub pool that
mimics the asyncpg ``Pool.fetchrow``/``fetch`` surface.  No real
Postgres connection is required.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from scripts.synthetic_prober import (
    SYNTHETIC_PROBE_CONTENT,
    ProbeResult,
    _is_terminal_success,
    emit_results,
    probe_bot,
    run_probes,
)


# ── stub pool ────────────────────────────────────────────────────────────────


class StubPool:
    """In-memory mimic of an asyncpg pool sufficient for the prober."""

    def __init__(
        self,
        *,
        scope_by_bot: dict[str, tuple[uuid.UUID, uuid.UUID]] | None = None,
        # Map of bot_id -> sequence of (processing_state, handling_result) tuples
        # that ``fetch_message_state`` will return on successive polls.
        states_by_bot: dict[
            str, list[tuple[str | None, str | None]]
        ] | None = None,
    ) -> None:
        self._scope_by_bot = scope_by_bot or {}
        self._states_by_bot = states_by_bot or {}
        self._poll_idx: dict[str, int] = {}
        self._inserted: dict[uuid.UUID, str] = {}
        self._inserted_rows: list[dict[str, Any]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        stripped = " ".join(sql.split())
        if stripped.startswith("INSERT INTO messages"):
            new_id = uuid.uuid4()
            bot_id = args[3]
            self._inserted[new_id] = bot_id
            self._inserted_rows.append(
                {
                    "id": new_id,
                    "sender_id": args[0],
                    "content": args[1],
                    "sent_at": args[2],
                    "bot_id": bot_id,
                    "topic_id": args[4],
                }
            )
            return {"id": new_id}
        if "SELECT processing_state, handling_result" in sql:
            message_id = args[0]
            bot_id = self._inserted.get(message_id)
            if bot_id is None:
                return None
            seq = self._states_by_bot.get(bot_id, [])
            idx = self._poll_idx.get(bot_id, 0)
            if not seq:
                state, hr = (None, None)
            else:
                state, hr = seq[min(idx, len(seq) - 1)]
            self._poll_idx[bot_id] = idx + 1
            return {"processing_state": state, "handling_result": hr}
        if "SELECT sender_id, topic_id" in sql:
            bot_id = args[0]
            scope = self._scope_by_bot.get(bot_id)
            if scope is None:
                return None
            sender_id, topic_id = scope
            return {"sender_id": sender_id, "topic_id": topic_id}
        raise AssertionError(f"unhandled SQL in StubPool: {sql!r}")

    @property
    def inserted_rows(self) -> list[dict[str, Any]]:
        return list(self._inserted_rows)


# ── helpers ──────────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2024, 1, 1, tzinfo=UTC)


async def _instant_sleep(_seconds: float) -> None:
    """Yield control without actually sleeping, so tests are fast."""
    await asyncio.sleep(0)


# ── pure logic tests ─────────────────────────────────────────────────────────


class TestIsTerminalSuccess:
    def test_processed_replied_is_success(self) -> None:
        assert _is_terminal_success("processed", "replied") is True

    def test_processed_silent_is_not_success(self) -> None:
        assert _is_terminal_success("processed", "silent") is False

    def test_failed_is_not_success(self) -> None:
        assert _is_terminal_success("failed", "failed") is False

    def test_none_is_not_success(self) -> None:
        assert _is_terminal_success(None, None) is False

    def test_expired_replied_counts_as_success(self) -> None:
        # Terminal state with handling_result='replied' counts even if the
        # row ended up expired (retention sweep ran after success).
        assert _is_terminal_success("expired", "replied") is True


# ── probe_bot tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_bot_passes_when_bot_replies_quickly() -> None:
    sender, topic = uuid.uuid4(), uuid.uuid4()
    pool = StubPool(
        scope_by_bot={"mediator": (sender, topic)},
        # First poll: still raw.  Second poll: processed/replied.
        states_by_bot={
            "mediator": [("raw", None), ("processed", "replied")],
        },
    )
    result = await probe_bot(
        pool,
        "mediator",
        slo_seconds=10.0,
        poll_interval=0.01,
        now_fn=_now,
        sleep_fn=_instant_sleep,
    )
    assert result.reached_terminal is True
    assert result.bot == "mediator"
    assert result.final_state == "processed"
    assert result.handling_result == "replied"
    # The synthetic row really got inserted with our marker content.
    assert pool.inserted_rows
    row = pool.inserted_rows[0]
    assert row["content"] == SYNTHETIC_PROBE_CONTENT
    assert row["bot_id"] == "mediator"
    assert row["sender_id"] == sender
    assert row["topic_id"] == topic


@pytest.mark.asyncio
async def test_probe_bot_returns_failure_when_no_scope_history_exists() -> None:
    pool = StubPool(scope_by_bot={}, states_by_bot={})
    result = await probe_bot(
        pool,
        "ghost_bot",
        slo_seconds=1.0,
        poll_interval=0.01,
        now_fn=_now,
        sleep_fn=_instant_sleep,
    )
    assert result.reached_terminal is False
    assert result.final_state is None
    assert result.bot == "ghost_bot"


@pytest.mark.asyncio
async def test_probe_bot_times_out_when_row_never_terminal() -> None:
    sender, topic = uuid.uuid4(), uuid.uuid4()
    pool = StubPool(
        scope_by_bot={"hector": (sender, topic)},
        # Row stays in 'raw' forever — should time out.
        states_by_bot={"hector": [("raw", None)]},
    )
    # Use slo_seconds=0 so the very first elapsed-check fires the timeout
    # path, but the row gets inserted and inspected.
    result = await probe_bot(
        pool,
        "hector",
        slo_seconds=0.0,
        poll_interval=0.01,
        now_fn=_now,
        sleep_fn=_instant_sleep,
    )
    assert result.reached_terminal is False
    assert result.final_state == "raw"
    assert result.handling_result is None


# ── run_probes (multi-bot) ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_probes_mixed_pass_and_fail() -> None:
    sender, topic = uuid.uuid4(), uuid.uuid4()
    pool = StubPool(
        scope_by_bot={
            "mediator": (sender, topic),
            "hector": (sender, topic),
        },
        states_by_bot={
            "mediator": [("processed", "replied")],
            "hector": [("raw", None)],
        },
    )
    results = await run_probes(
        pool,
        ("mediator", "hector"),
        slo_seconds=0.0,
        poll_interval=0.01,
    )
    by_bot = {r.bot: r for r in results}
    assert by_bot["mediator"].reached_terminal is True
    assert by_bot["hector"].reached_terminal is False


# ── log emission ─────────────────────────────────────────────────────────────


def test_emit_results_writes_one_log_per_bot(caplog: pytest.LogCaptureFixture) -> None:
    results = [
        ProbeResult(
            bot="mediator",
            message_id=uuid.uuid4(),
            reached_terminal=True,
            latency_seconds=1.23,
            final_state="processed",
            handling_result="replied",
        ),
        ProbeResult(
            bot="hector",
            message_id=uuid.uuid4(),
            reached_terminal=False,
            latency_seconds=60.0,
            final_state="raw",
            handling_result=None,
        ),
    ]
    with caplog.at_level("INFO", logger="synthetic_prober"):
        emit_results(results)
    recs = [r for r in caplog.records if r.name == "synthetic_prober"]
    assert len(recs) == 2
    assert any("bot=mediator" in r.getMessage() for r in recs)
    assert any("bot=hector" in r.getMessage() for r in recs)
