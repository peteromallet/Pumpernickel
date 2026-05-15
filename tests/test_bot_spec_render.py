"""Tests for BotSpec.render_system_prompt partner=None safety.

T1 / FLAG-H08 / DEBT-041/046/047: Verify that BotSpec.render_system_prompt
does not AttributeError when partner=None (solo turns).  The fix is a one-line
guard in app/bots/base.py:75.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.models.user import User


def _make_test_user(name: str = "TestUser") -> User:
    return User(
        id=uuid4(),
        name=name,
        phone="+15555550100",
        timezone="America/New_York",
        onboarding_state="completed",
    )


class TestRenderSystemPromptPartnerNone:
    """BotSpec.render_system_prompt must handle partner=None without crashing."""

    def test_tante_rosi_solo_render_does_not_crash(self):
        """Tante Rosi BotSpec.render_system_prompt(partner=None) should succeed."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        user = _make_test_user("Anna")

        result = spec.render_system_prompt(
            assistant_name="Tante Rosi",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_mediator_solo_render_does_not_crash(self):
        """Mediator BotSpec.render_system_prompt(partner=None) should succeed."""
        from app.services.tools.registry import TOOL_DISPATCH

        mediator_spec = BotSpec(
            bot_id="mediator",
            prompt_renderer=lambda *a, **kw: "test prompt",
            step_instructions={
                "read": "read",
                "consult": "consult",
                "respond": "respond",
                "record": "record",
                "schedule": "schedule",
                "done": "done",
            },
            display_name="Mediator",
            primary_topic_slug="relationship",
            participants_shape="dyad",
            read_scopes=ReadScopes(
                topics=frozenset({"all"}),
                allow_cross_topic_peek=True,
                allow_cross_topic_status_injection=True,
            ),
            write_scopes=WriteScopes(topics=frozenset({"all"})),
        )
        user = _make_test_user("TestUser")

        result = mediator_spec.render_system_prompt(
            assistant_name="Mediator",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_base_method_partner_none_safety(self):
        """Direct BotSpec.render_system_prompt with partner=None must not AttributeError.

        This verifies the one-line guard at base.py:75 works regardless of
        which prompt_renderer is wired.  Any bot with participants_shape='solo'
        passes partner=None, and the base method must not crash.
        """

        def dummy_renderer(*args, **kwargs):
            return "ok"

        spec = BotSpec(
            bot_id="test_bot",
            prompt_renderer=dummy_renderer,
            step_instructions={
                "read": "r",
                "consult": "c",
                "respond": "res",
                "record": "rec",
                "schedule": "sch",
                "done": "d",
            },
            participants_shape="solo",
        )
        user = _make_test_user()

        # This is the call that would AttributeError without the guard
        result = spec.render_system_prompt(
            assistant_name="TestBot",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert result == "ok"
