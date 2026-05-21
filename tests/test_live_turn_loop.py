"""Tests for live voice turn context."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from app.bots.base import BotSpec
from app.bots.registry import BOT_SPECS
from app.services.live.turn_loop import load_turn_context


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
