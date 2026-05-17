"""Tests for ``scripts/why_no_reply.py``.

These exercise the pure decision logic (``evaluate_retry_eligibility`` /
``recommended_action``) and the async ``diagnose`` orchestrator against a
tiny in-memory stub pool.  They do not require a real Postgres connection,
matching the pattern used by other unit-level script tests in this repo
(see ``tests/test_ops_scripts.py``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from scripts.why_no_reply import (
    DEFAULT_MAX_RETRIES,
    diagnose,
    evaluate_retry_eligibility,
    recommended_action,
)


NOW = datetime(2020, 1, 1, 12, 0, 0, tzinfo=UTC)


def _msg(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": uuid4(),
        "direction": "inbound",
        "sender_id": uuid4(),
        "recipient_id": None,
        "content": "hi",
        "sent_at": NOW - timedelta(minutes=2),
        "in_reply_to": None,
        "processing_state": "raw",
        "charge": "routine",
        "whatsapp_message_id": "9999999999",
        "deleted_at": None,
        "bot_id": "hector",
        "topic_id": uuid4(),
        "handled_at": None,
        "handled_by_turn_id": None,
        "handling_result": None,
        "processing_started_at": None,
        "processing_error": None,
        "processing_attempts": 0,
        "next_retry_at": None,
        "failure_class": None,
    }
    base.update(overrides)
    return base


# ── pure-logic tests ─────────────────────────────────────────────────────────


class TestEvaluateRetryEligibility:
    def test_raw_with_no_gate_is_eligible(self) -> None:
        msg = _msg(processing_state="raw")
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is True
        assert "raw" in reason

    def test_raw_with_future_next_retry_at_is_not_eligible(self) -> None:
        msg = _msg(
            processing_state="raw",
            next_retry_at=NOW + timedelta(minutes=5),
        )
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is False
        assert "next_retry_at" in reason

    def test_failed_retryable_pre_send_within_attempts_is_eligible(self) -> None:
        msg = _msg(
            processing_state="failed",
            processing_attempts=1,
            failure_class="retryable_pre_send",
            next_retry_at=NOW - timedelta(seconds=5),
        )
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is True
        assert "retryable_pre_send" in reason

    def test_failed_retry_cap_reached_is_not_eligible(self) -> None:
        msg = _msg(
            processing_state="failed",
            processing_attempts=DEFAULT_MAX_RETRIES,
            failure_class="retryable_pre_send",
        )
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is False
        assert "retry cap" in reason

    def test_terminal_post_send_never_retryable(self) -> None:
        msg = _msg(
            processing_state="failed",
            processing_attempts=1,
            failure_class="terminal_post_send",
        )
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is False
        assert "terminal_post_send" in reason

    def test_infra_bug_never_retryable(self) -> None:
        msg = _msg(
            processing_state="failed",
            failure_class="infra_bug",
        )
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is False
        assert "infra_bug" in reason

    def test_stale_processing_is_eligible(self) -> None:
        msg = _msg(
            processing_state="processing",
            processing_started_at=NOW - timedelta(minutes=10),
        )
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is True
        assert "stale" in reason

    def test_fresh_processing_is_not_eligible(self) -> None:
        msg = _msg(
            processing_state="processing",
            processing_started_at=NOW - timedelta(seconds=10),
        )
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is False
        assert "processing" in reason

    def test_processed_terminal(self) -> None:
        msg = _msg(processing_state="processed", handling_result="replied")
        eligible, reason = evaluate_retry_eligibility(msg, now=NOW)
        assert eligible is False
        assert "terminal" in reason

    def test_missing_coalescer_blocks_recovery(self) -> None:
        msg = _msg(
            processing_state="failed",
            failure_class="retryable_pre_send",
            processing_attempts=1,
            bot_id="ghost_bot",
        )
        eligible, reason = evaluate_retry_eligibility(
            msg, now=NOW, known_bot_ids={"hector", "mediator"}
        )
        assert eligible is False
        assert "ghost_bot" in reason
        assert "coalescer" in reason


class TestRecommendedAction:
    def test_replied_says_no_action(self) -> None:
        msg = _msg(processing_state="processed", handling_result="replied")
        action = recommended_action(
            msg,
            eligible=False,
            eligible_reason="already terminal (processed/replied)",
            turns=[],
            outbound_id=uuid4(),
            now=NOW,
        )
        assert "already replied" in action

    def test_terminal_post_send_says_no_action(self) -> None:
        msg = _msg(processing_state="failed", failure_class="terminal_post_send")
        action = recommended_action(
            msg,
            eligible=False,
            eligible_reason="terminal_post_send (reply already sent; not retryable)",
            turns=[],
            outbound_id=None,
            now=NOW,
        )
        assert "do NOT manually requeue" in action

    def test_infra_bug_says_manual(self) -> None:
        msg = _msg(processing_state="failed", failure_class="infra_bug")
        action = recommended_action(
            msg,
            eligible=False,
            eligible_reason="infra_bug",
            turns=[],
            outbound_id=None,
            now=NOW,
        )
        assert "manual" in action.lower()

    def test_eligible_says_wait_for_recovery(self) -> None:
        msg = _msg(processing_state="raw")
        action = recommended_action(
            msg,
            eligible=True,
            eligible_reason="raw and eligible for claim",
            turns=[],
            outbound_id=None,
            now=NOW,
        )
        assert "recovery" in action or "wait" in action

    def test_stale_processing_advises_sweep(self) -> None:
        msg = _msg(
            processing_state="processing",
            processing_started_at=NOW - timedelta(minutes=10),
        )
        action = recommended_action(
            msg,
            eligible=True,
            eligible_reason="stale claim",
            turns=[],
            outbound_id=None,
            now=NOW,
        )
        assert "stale" in action.lower() or "reset" in action.lower()


# ── orchestrator tests with a stub pool ──────────────────────────────────────


class _StubPool:
    """Minimal asyncpg-pool-compatible stub.

    Routes queries by inspecting a short fragment of the SQL text and
    returning canned rows.  Tests construct the pool with the rows the
    diagnostic should see.
    """

    def __init__(
        self,
        *,
        message_row: dict[str, Any] | None,
        turns: list[dict[str, Any]] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        outbound_row: dict[str, Any] | None = None,
        known_bots: list[str] | None = None,
    ) -> None:
        self.message_row = message_row
        self.turns = list(turns or [])
        self.tool_calls = list(tool_calls or [])
        self.outbound_row = outbound_row
        self.known_bots = list(known_bots or [])

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        if "FROM messages" in sql and "WHERE id =" in sql:
            # Could be the inbound lookup OR the outbound lookup; the
            # outbound lookup carries the final_output_message_id arg.
            target = args[0]
            if self.outbound_row is not None and str(self.outbound_row.get("id")) == str(target):
                return self.outbound_row
            if self.message_row is not None and str(self.message_row.get("id")) == str(target):
                return self.message_row
            # First-shot inbound UUID lookup when identifier == messages.id:
            if self.message_row is not None and str(target) == str(args[0]):
                return self.message_row
            return None
        if "FROM messages" in sql and "whatsapp_message_id" in sql:
            if (
                self.message_row is not None
                and self.message_row.get("whatsapp_message_id") == args[0]
            ):
                return self.message_row
            return None
        return None

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        if "FROM bot_turns" in sql:
            return self.turns
        if "FROM tool_calls" in sql:
            return self.tool_calls
        if "DISTINCT bot_id FROM messages" in sql:
            return [{"bot_id": b} for b in self.known_bots]
        return []

    async def execute(self, sql: str, *args: Any) -> str:  # pragma: no cover - unused
        return "OK"

    async def close(self) -> None:  # pragma: no cover - unused
        return None


def test_diagnose_normal_replied_turn() -> None:
    msg_id = uuid4()
    turn_id = uuid4()
    outbound_id = uuid4()
    message_row = _msg(
        id=msg_id,
        processing_state="processed",
        handling_result="replied",
        handled_at=NOW - timedelta(seconds=30),
        handled_by_turn_id=turn_id,
        processing_attempts=1,
    )
    turns = [
        {
            "id": turn_id,
            "bot_id": "hector",
            "topic_id": message_row["topic_id"],
            "triggered_by_message_id": msg_id,
            "triggering_message_ids": [msg_id],
            "user_in_context": message_row["sender_id"],
            "started_at": NOW - timedelta(seconds=60),
            "completed_at": NOW - timedelta(seconds=30),
            "model_version": "claude-opus-4.7",
            "tool_call_count": 1,
            "duration_ms": 30000,
            "failure_reason": None,
            "final_output_message_id": outbound_id,
        }
    ]
    tool_calls = [
        {
            "id": uuid4(),
            "tool_name": "send_message",
            "arguments": {},
            "result": {"ok": True},
            "called_at": NOW - timedelta(seconds=45),
            "duration_ms": 1000,
        }
    ]
    outbound_row = {
        "id": outbound_id,
        "whatsapp_message_id": "8888888888",
        "sent_at": NOW - timedelta(seconds=30),
        "direction": "outbound",
        "content": "hello back",
    }
    pool = _StubPool(
        message_row=message_row,
        turns=turns,
        tool_calls=tool_calls,
        outbound_row=outbound_row,
        known_bots=["hector"],
    )

    out = asyncio.run(diagnose(pool, str(msg_id)))

    assert "Inbound row state" in out
    assert "processed" in out
    assert "replied" in out
    assert "Tool calls" in out
    assert "send_message" in out
    assert str(outbound_id) in out
    assert "already replied" in out


def test_diagnose_failed_pre_send_retryable() -> None:
    msg_id = uuid4()
    turn_id = uuid4()
    message_row = _msg(
        id=msg_id,
        processing_state="failed",
        processing_attempts=1,
        failure_class="retryable_pre_send",
        next_retry_at=NOW - timedelta(seconds=5),
        processing_error="provider_send_failed [failure_class=retryable_pre_send]",
        handling_result="failed",
        handled_by_turn_id=turn_id,
    )
    turns = [
        {
            "id": turn_id,
            "bot_id": "hector",
            "topic_id": message_row["topic_id"],
            "triggered_by_message_id": msg_id,
            "triggering_message_ids": [msg_id],
            "user_in_context": message_row["sender_id"],
            "started_at": NOW - timedelta(minutes=2),
            "completed_at": NOW - timedelta(minutes=1),
            "model_version": "claude-opus-4.7",
            "tool_call_count": 0,
            "duration_ms": 60000,
            "failure_reason": "provider_send_failed",
            "final_output_message_id": None,
        }
    ]
    pool = _StubPool(
        message_row=message_row,
        turns=turns,
        tool_calls=[],
        outbound_row=None,
        known_bots=["hector"],
    )

    out = asyncio.run(diagnose(pool, str(msg_id)))

    assert "retryable_pre_send" in out
    assert "failed" in out
    # Eligibility section: should report eligible=yes
    assert "eligible       = yes" in out
    assert "no final outbound" in out


def test_diagnose_missing_message_returns_clear_error() -> None:
    pool = _StubPool(message_row=None, known_bots=[])
    bogus_id = str(uuid4())
    out = asyncio.run(diagnose(pool, bogus_id))
    assert "no messages row" in out
