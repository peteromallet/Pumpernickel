"""S4 T27 — coach transport staging pre-flight test.

Skipped unless STAGING env is truthy. With STAGING=1, registers the coach
bot via the lazy registration path and exercises check_oob_with_policy with
a known-bad outbound + a stubbed OOB row, asserting the guardrail fires
(verdict=block).
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("STAGING", "").lower() not in {"1", "true", "yes"},
    reason="coach transport pre-flight only runs under STAGING env",
)


def test_coach_bot_registered_under_staging() -> None:
    from app.bots.registry import BOT_SPECS, _maybe_register_staging_bots

    _maybe_register_staging_bots()
    assert "coach" in BOT_SPECS, "coach bot must register under STAGING=1"
    coach = BOT_SPECS["coach"]
    assert coach.primary_topic_slug == "career"
    assert coach.read_scopes.topics == frozenset({"career"})
    assert coach.write_scopes.topics == frozenset({"career"})
    assert "set_topic_status" not in coach.tool_allowlist


class _StubPool:
    """Returns one hard-OOB entry; matches the join_artifact_topics shape."""

    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self._entries = entries

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return list(self._entries)


class _StubClient:
    """Stub Anthropic client that returns a 'block' verdict."""

    def __init__(self) -> None:
        self.messages = self
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        verdict = {
            "verdict": "block",
            "reason": "draft outbound discloses protected content",
            "triggering_oob_ids": [],
            "suggested_rewrite": None,
            "checker_failed": False,
        }
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(verdict))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=20),
        )


@pytest.mark.asyncio
async def test_oob_guardrail_fires_on_known_bad_outbound(monkeypatch) -> None:
    from app.services import oob_check

    # Avoid LLM spend cap DB lookup
    async def _under_cap_true(*_a: Any, **_kw: Any) -> bool:
        return True

    async def _no_cost(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(oob_check, "is_under_cap", _under_cap_true)
    monkeypatch.setattr(oob_check, "_record_response_cost", _no_cost)

    oob_id = uuid4()
    pool = _StubPool([
        {"id": oob_id, "sensitive_core": "the secret", "shareable_context": "a hard boundary", "severity": "hard"},
    ])
    client = _StubClient()
    recipient_id = uuid4()
    topic_id = uuid4()

    out = await oob_check.check_oob_with_policy(
        pool,
        content="The secret is X.",
        recipient_id=recipient_id,
        protected_owner_ids=[recipient_id],
        sender_intent="staging coach OOB pre-flight",
        client=client,
        topic_id=topic_id,
    )
    assert out.verdict == "block", f"expected block, got {out.verdict}"
    assert client.calls, "expected at least one Anthropic create call"
