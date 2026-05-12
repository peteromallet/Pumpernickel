"""Tests for the Tante Rosi persona prompt module.

Phase 1: verifies the placeholder persona imports cleanly and renders
a system prompt without error.  Phase 2 will add assertion tests for
specific persona content strings (medical-defer phrasing, onboarding
script, loss-handling guidance, red-flag triggers).
"""

from __future__ import annotations

import pytest


class TestTanteRosiPersonaImport:
    """Tante Rosi persona prompt module — import and render tests."""

    def test_imports_without_error(self):
        """The persona module should be importable."""
        from app.bots.prompts.tante_rosi import render_system_prompt

        assert callable(render_system_prompt)

    def test_renders_prompt_without_error(self):
        """Calling render_system_prompt with minimal args should succeed."""
        from app.bots.prompts.tante_rosi import render_system_prompt

        result = render_system_prompt(
            assistant_name="Tante Rosi",
            user_name="Anna",
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 0

    def test_renders_contains_topic_display_name(self):
        """The rendered prompt should reference pregnancy."""
        from app.bots.prompts.tante_rosi import render_system_prompt

        result = render_system_prompt(
            assistant_name="Tante Rosi",
            user_name="Anna",
            prompt_version="v1",
        )
        assert "pregnancy" in result.lower()

    def test_delegates_to_solo_prompt_template(self):
        """Key sections from the shared solo system prompt should appear."""
        from app.bots.prompts.tante_rosi import render_system_prompt

        result = render_system_prompt(
            assistant_name="Tante Rosi",
            user_name="Anna",
            prompt_version="v1",
        )
        assert "Role And Identity" in result
        assert "Operating Principles" in result
        assert "Tante Rosi" in result
        assert "Anna" in result

    def test_handles_extra_kwargs_gracefully(self):
        """Extra kwargs passed from BotSpec.render_system_prompt should
        not cause errors (e.g., partner=... is forwarded and ignored)."""
        from app.bots.prompts.tante_rosi import render_system_prompt

        result = render_system_prompt(
            assistant_name="Tante Rosi",
            user_name="Anna",
            prompt_version="v1",
            partner=None,
            partner_sharing_default=None,
            sharing_default=None,
        )
        assert isinstance(result, str)
        assert len(result) > 0


class TestBuildTanteRosiSpec:
    """BotSpec factory function tests."""

    def test_builds_spec_without_error(self):
        """build_tante_rosi_spec() should return a valid BotSpec."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        assert spec is not None
        assert spec.bot_id == "tante_rosi"
        assert spec.display_name == "Tante Rosi"
        assert spec.primary_topic_slug == "pregnancy"
        assert spec.participants_shape == "solo"
        assert callable(spec.prompt_renderer)

    def test_has_correct_read_scopes(self):
        """ReadScopes must match the sprint brief §2.1."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        assert spec.read_scopes.topics == frozenset({"own"})
        assert spec.read_scopes.allow_cross_topic_peek is True
        assert spec.read_scopes.allow_cross_topic_status_injection is False

    def test_has_correct_write_scopes(self):
        """WriteScopes must match the sprint brief §2.1."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        assert spec.write_scopes.topics == frozenset({"own"})

    def test_has_cross_topic_policy_peek(self):
        """cross_topic_policy must be 'peek'."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        assert spec.cross_topic_policy == "peek"

    def test_includes_pregnancy_tools_in_allowlist(self):
        """The three pregnancy write tools must be in the allowlist."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        assert spec.tool_allowlist is not None
        assert "set_pregnancy_edd" in spec.tool_allowlist
        assert "correct_pregnancy_edd" in spec.tool_allowlist
        assert "end_pregnancy" in spec.tool_allowlist

    def test_excludes_coach_exclusions(self):
        """The 8 coach-excluded tools must be absent from Rosi's allowlist."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        assert spec.tool_allowlist is not None
        for excluded in (
            "set_topic_status",
            "create_bridge_candidate",
            "update_bridge_candidate",
            "send_bridge_candidate",
            "list_bridge_candidates",
            "escalate_to_partner",
            "search_messages",
            "recent_activity",
        ):
            assert excluded not in spec.tool_allowlist, (
                f"{excluded} should be excluded from Rosi allowlist"
            )

    def test_renders_system_prompt_through_spec(self):
        """BotSpec.render_system_prompt should work for the Tante Rosi spec."""
        from app.bots.tante_rosi import build_tante_rosi_spec
        from app.models.user import User

        spec = build_tante_rosi_spec()
        user = User(id="u1", name="Anna", phone="+15555550100", timezone="UTC")
        result = spec.render_system_prompt(
            assistant_name="Tante Rosi",
            user=user,
            partner=None,
            prompt_version="v1",
        )
        assert isinstance(result, str)
        assert len(result) > 0
        assert "Tante Rosi" in result
        assert "Anna" in result