"""Allowlist tests for the Hector fitness bot.

Mirrors test_pregnancy_allowlist.py.  Verifies:
- Hector's tool_allowlist contains all 7 commitment/event tools + 3 health read tools.
- Coach, Tante Rosi, and Mediator allowlists do NOT contain any
  commitment/event or health read tools.
- BOT_EXCLUSIVE_TOOLS filter removes Hector-only tools for non-Hector bots.
- to_anthropic_tools() never receives Hector-exclusive tool names for
  non-Hector bots.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Hector's allowlist
# ---------------------------------------------------------------------------


class TestHectorAllowlist:
    """Hector's tool_allowlist — all 10 Hector tools present."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg

        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def test_contains_all_hector_tools(self):
        """All 10 Hector tools (7 commitment/event + 3 health read) must be in the allowlist."""
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("hector")
        assert spec.tool_allowlist is not None

        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        for tool_name in hector_tools:
            assert tool_name in spec.tool_allowlist, (
                f"{tool_name} must be in Hector's allowlist"
            )

    def test_workout_summary_in_hector_allowlist(self):
        """get_workout_summary specifically must be in Hector's allowlist."""
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("hector")
        assert spec.tool_allowlist is not None
        assert "get_workout_summary" in spec.tool_allowlist, (
            "get_workout_summary must be in Hector's allowlist"
        )

    def test_excludes_pregnancy_tools(self):
        """Hector must not have pregnancy tools."""
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("hector")
        assert spec.tool_allowlist is not None

        for tool_name in (
            "set_pregnancy_edd",
            "correct_pregnancy_edd",
            "end_pregnancy",
        ):
            assert tool_name not in spec.tool_allowlist, (
                f"{tool_name} must be excluded from Hector allowlist"
            )

    def test_excludes_bridge_escalate_tools(self):
        """Hector must not have bridge/escalate dyad tools."""
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("hector")
        assert spec.tool_allowlist is not None

        for tool_name in (
            "create_bridge_candidate",
            "update_bridge_candidate",
            "send_bridge_candidate",
            "list_bridge_candidates",
            "escalate_to_partner",
        ):
            assert tool_name not in spec.tool_allowlist, (
                f"{tool_name} must be excluded from Hector allowlist"
            )


# ---------------------------------------------------------------------------
# Coach's allowlist — no commitment/event tools
# ---------------------------------------------------------------------------


class TestCoachAllowlist:
    """Coach's tool_allowlist must NOT contain commitment/event tools."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg

        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def test_coach_lacks_commitment_event_tools(self):
        """Coach must not have any commitment/event or health read tools."""
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("coach")
        assert spec.tool_allowlist is not None

        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        found = spec.tool_allowlist & hector_tools
        assert not found, (
            f"Coach allowlist contains Hector-exclusive tools: {found}"
        )


# ---------------------------------------------------------------------------
# Tante Rosi's allowlist — no commitment/event tools
# ---------------------------------------------------------------------------


class TestTanteRosiAllowlist:
    """Tante Rosi's tool_allowlist must NOT contain commitment/event tools."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg

        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def test_tante_rosi_lacks_commitment_event_tools(self):
        """Tante Rosi must not have any commitment/event or health read tools."""
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("tante_rosi")
        assert spec.tool_allowlist is not None

        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        found = spec.tool_allowlist & hector_tools
        assert not found, (
            f"Tante Rosi allowlist contains Hector-exclusive tools: {found}"
        )


# ---------------------------------------------------------------------------
# Mediator's allowlist — no commitment/event tools
# ---------------------------------------------------------------------------


class TestMediatorAllowlist:
    """Mediator's tool_allowlist must NOT contain commitment/event tools."""

    def test_mediator_lacks_commitment_event_tools(self):
        """Mediator must not have any commitment/event or health read tools (allowlist is None
        meaning all non-exclusive tools, but BOT_EXCLUSIVE_TOOLS removes them)."""
        from app.bots.registry import get_bot_spec

        spec = get_bot_spec("mediator")
        # Mediator has allowlist=None (all tools permitted)
        # but BOT_EXCLUSIVE_TOOLS filter removes Hector tools at runtime.
        # The allowlist itself should not enumerate them.
        if spec.tool_allowlist is not None:
            hector_tools = {
                "create_commitment",
                "update_commitment",
                "close_commitment",
                "log_event",
                "list_commitments",
                "list_events",
                "get_adherence",
                "get_weight_trend",
                "get_sleep_summary",
                "get_workout_summary",
            }
            found = spec.tool_allowlist & hector_tools
            assert not found, (
                f"Mediator allowlist contains Hector-exclusive tools: {found}"
            )
        # If allowlist is None, BOT_EXCLUSIVE_TOOLS handles the filtering.
        # That's the expected production behavior.


# ---------------------------------------------------------------------------
# BOT_EXCLUSIVE_TOOLS filter in _step_allowed()
# ---------------------------------------------------------------------------


class TestBotExclusiveToolsFilter:
    """BOT_EXCLUSIVE_TOOLS removes Hector-only tools for non-Hector bots."""

    @pytest.fixture(autouse=True)
    def _register_staging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("STAGING", "1")
        import app.bots.registry as _reg

        _reg._STAGING_BOTS_REGISTERED = False
        _reg._maybe_register_staging_bots()

    def test_hector_gets_hector_tools_from_step_allowed(self):
        """_step_allowed() for Hector includes commitment/event + health read tools."""
        from uuid import uuid4

        from app.models.user import User
        from app.services.turn_context import TurnContext
        from app.services.tools.registry import _step_allowed
        from app.bots.registry import get_bot_spec

        user = User(
            id=uuid4(), name="Test", phone="+155****0100", timezone="UTC"
        )
        spec = get_bot_spec("hector")
        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=user,
            partner=None,
            triggering_message_ids=[],
            bot_id="hector",
            primary_topic_id=uuid4(),
            primary_topic_slug="fitness",
            current_step="record",
            bot_spec=spec,
        )

        allowed = _step_allowed(ctx)
        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        present = allowed & hector_tools
        assert present == hector_tools, (
            f"Hector _step_allowed missing: {hector_tools - present}"
        )

    def test_coach_step_allowed_excludes_hector_tools(self):
        """_step_allowed() for Coach removes all Hector-exclusive tools."""
        from uuid import uuid4

        from app.models.user import User
        from app.services.turn_context import TurnContext
        from app.services.tools.registry import _step_allowed
        from app.bots.registry import get_bot_spec

        user = User(
            id=uuid4(), name="Test", phone="+155****0100", timezone="UTC"
        )
        spec = get_bot_spec("coach")
        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=user,
            partner=None,
            triggering_message_ids=[],
            bot_id="coach",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            current_step="record",
            bot_spec=spec,
        )

        allowed = _step_allowed(ctx)
        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        found = allowed & hector_tools
        assert not found, (
            f"Coach _step_allowed has Hector-exclusive tools: {found}"
        )

    def test_tante_rosi_step_allowed_excludes_hector_tools(self):
        """_step_allowed() for Tante Rosi removes all Hector-exclusive tools."""
        from uuid import uuid4

        from app.models.user import User
        from app.services.turn_context import TurnContext
        from app.services.tools.registry import _step_allowed
        from app.bots.registry import get_bot_spec

        user = User(
            id=uuid4(), name="Test", phone="+155****0100", timezone="UTC"
        )
        spec = get_bot_spec("tante_rosi")
        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=user,
            partner=None,
            triggering_message_ids=[],
            bot_id="tante_rosi",
            primary_topic_id=uuid4(),
            primary_topic_slug="pregnancy",
            current_step="record",
            bot_spec=spec,
        )

        allowed = _step_allowed(ctx)
        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        found = allowed & hector_tools
        assert not found, (
            f"Tante Rosi _step_allowed has Hector-exclusive tools: {found}"
        )

    def test_mediator_step_allowed_excludes_hector_tools(self):
        """_step_allowed() for Mediator removes all Hector-exclusive tools even
        though Mediator's tool_allowlist is None (meaning all tools)."""
        from uuid import uuid4

        from app.models.user import User
        from app.services.turn_context import TurnContext
        from app.services.tools.registry import _step_allowed

        user = User(
            id=uuid4(), name="Test", phone="+155****0100", timezone="UTC"
        )
        ctx = TurnContext(
            turn_id=uuid4(),
            pool=None,
            user=user,
            partner=None,
            triggering_message_ids=[],
            bot_id="mediator",
            primary_topic_id=uuid4(),
            primary_topic_slug="relationship",
            current_step="record",
            bot_spec=None,  # Mediator spec might not be registered in tests
        )

        allowed = _step_allowed(ctx)
        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        found = allowed & hector_tools
        assert not found, (
            f"Mediator _step_allowed has Hector-exclusive tools: {found}"
        )


# ---------------------------------------------------------------------------
# to_anthropic_tools() exclusion
# ---------------------------------------------------------------------------


class TestAnthropicToolsExclusion:
    """to_anthropic_tools() should never include Hector tools for non-Hector bots."""

    def test_hector_tools_not_in_to_anthropic_for_coach(self):
        """Coach's to_anthropic_tools output must exclude all Hector-only tools."""
        from app.services.tools.registry import TOOL_DISPATCH, to_anthropic_tools

        # Simulate what Coach would get: all tools minus Hector exclusives
        coach_allowed = set(TOOL_DISPATCH.keys()) - {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        tools = to_anthropic_tools(coach_allowed)
        tool_names = {t["name"] for t in tools}

        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        found = tool_names & hector_tools
        assert not found, (
            f"Coach to_anthropic_tools includes Hector-exclusive tools: {found}"
        )

    def test_hector_tools_in_to_anthropic_for_hector(self):
        """Hector's to_anthropic_tools output must INCLUDE all Hector-only tools."""
        from app.services.tools.registry import TOOL_DISPATCH, to_anthropic_tools

        hector_allowed = set(TOOL_DISPATCH.keys())  # Hector gets everything it wants
        tools = to_anthropic_tools(hector_allowed)
        tool_names = {t["name"] for t in tools}

        hector_tools = {
            "create_commitment",
            "update_commitment",
            "close_commitment",
            "log_event",
            "list_commitments",
            "list_events",
            "get_adherence",
            "get_weight_trend",
            "get_sleep_summary",
            "get_workout_summary",
        }
        present = tool_names & hector_tools
        assert present == hector_tools, (
            f"Hector to_anthropic_tools missing: {hector_tools - present}"
        )
