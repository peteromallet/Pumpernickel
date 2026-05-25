"""Tests for live voice turn context."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.bots.base import BotSpec
from app.bots.registry import BOT_SPECS
from app.services.live.schemas import TurnEmission, TurnRequest
from app.services.live.turn_loop import FallbackTurnCaller, load_turn_context, select_turn_caller


class _TurnFakePool:
    def __init__(self, conversation: dict[str, Any], user: dict[str, Any]) -> None:
        self.conversation = conversation
        self.user = user

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        if "FROM mediator.conversations" in sql:
            return self.conversation
        if "FROM users" in sql:
            return self.user
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return []


@pytest.mark.anyio
async def test_load_turn_context_includes_selected_bot_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: live turns must not default to mediator instructions."""

    def prompt_renderer(
        assistant_name: str,
        user_name: str,
        partner_name: str | None = None,
        **_: Any,
    ) -> str:
        del partner_name
        return (
            f"{assistant_name} pregnancy prompt for {user_name}; "
            "speak German when apt."
        )

    monkeypatch.setitem(
        BOT_SPECS,
        "rosi_live_test",
        BotSpec(
            bot_id="rosi_live_test",
            prompt_renderer=prompt_renderer,
            step_instructions={
                "read": "read",
                "consult": "consult",
                "respond": "respond",
                "record": "record",
                "schedule": "schedule",
                "done": "done",
            },
            display_name="Tante Rosi",
            primary_topic_slug="pregnancy",
            participants_shape="solo",
        ),
    )

    session_id = uuid4()
    user_id = uuid4()
    pool = _TurnFakePool(
        conversation={
            "id": session_id,
            "user_id": user_id,
            "bot_id": "rosi_live_test",
            "prep_summary": "prep",
            "current_item_id": None,
            "session_fields": {},
            "status": "active",
        },
        user={
            "id": user_id,
            "name": "Maya",
            "phone": "+15555550100",
            "timezone": "Europe/Berlin",
            "onboarding_state": "ready",
            "pacing_preferences": {},
        },
    )

    context = await load_turn_context(pool, session_id)

    profile = context["bot_profile"]
    assert profile["bot_id"] == "rosi_live_test"
    assert profile["display_name"] == "Tante Rosi"
    assert profile["primary_topic_slug"] == "pregnancy"
    assert "pregnancy prompt for Maya" in profile["system_prompt"]


def test_select_turn_caller_wraps_anthropic_with_deepseek_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prod regression: a billing-blocked Anthropic key must not silence live turns."""

    monkeypatch.delenv("LIVE_VOICE_TURN_PROVIDER", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real-looking")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek-real-looking")

    caller = select_turn_caller()

    assert isinstance(caller, FallbackTurnCaller)
    assert caller.primary_name == "anthropic"
    assert caller.fallback_name == "deepseek"


@pytest.mark.anyio
async def test_fallback_turn_caller_uses_secondary_after_primary_failure() -> None:
    class Primary:
        async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
            raise RuntimeError("credit balance too low")

    class Secondary:
        async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
            return TurnEmission(utterance=f"reply to {request.user_transcript_final}")

    caller = FallbackTurnCaller(
        Primary(),
        Secondary(),
        primary_name="anthropic",
        fallback_name="deepseek",
    )

    emission = await caller.call(
        TurnRequest(session_id=str(uuid4()), user_transcript_final="Hey, can you hear me?"),
        {},
    )

    assert emission.utterance == "reply to Hey, can you hear me?"
