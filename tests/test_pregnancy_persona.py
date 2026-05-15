"""Tests for the Tante Rosi persona prompt module.

Phase 1 verified the placeholder persona imported and rendered. Phase 2
adds assertion tests for the real content: German-default language
stance, medical-defer phrasing, red-flag escalation triggers, loss
handling, onboarding script, and boundary refusals.
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

    def test_step_instructions_are_specific_not_stubs(self):
        """Tante Rosi should have real phase instructions, not placeholders."""
        from app.bots.tante_rosi import build_tante_rosi_spec

        spec = build_tante_rosi_spec()
        rendered = "\n".join(spec.step_instructions.values()).lower()

        assert "phase 1 stub" not in rendered
        assert "durable pregnancy state" in rendered
        assert "set_pregnancy_edd" in rendered
        assert "read before durable writes" in rendered


class TestTanteRosiPersonaContent:
    """Phase 2 content assertions — the persona must encode specific guardrails."""

    @staticmethod
    def _render(onboarding_state: str | None = None) -> str:
        from app.bots.prompts.tante_rosi import render_system_prompt

        return render_system_prompt(
            assistant_name="Tante Rosi",
            user_name="Anna",
            prompt_version="v1",
            onboarding_state=onboarding_state,
        )

    def test_default_language_is_german_with_match_fallback(self):
        """Persona instructs German by default, match user's language otherwise."""
        result = self._render()
        assert "German" in result
        # The instruction must include matching the user's language when they
        # clearly use another, not just German-only.
        assert "another language" in result.lower() or "match" in result.lower()

    def test_prompt_defines_pregnancy_knowledge_primitives(self):
        """Rosi should know what pregnancy context is worth preserving."""
        result = self._render()
        lower = result.lower()

        assert "pregnancy knowledge primitives" in lower
        assert "pregnancy state is the formal pregnancy timeline" in lower
        assert "memories are stable concrete facts" in lower
        assert "observations are patterns and support signals" in lower
        assert "before adding or updating durable state" in lower

    def test_uses_du_not_sie(self):
        """German register is informal 'du'."""
        result = self._render()
        assert '"du"' in result
        # Explicit guidance against Sie (formal).
        assert '"Sie"' in result  # mentioned as the form to avoid

    def test_avoids_saccharine_pet_names(self):
        """Persona explicitly tells the bot not to use pet names."""
        result = self._render()
        assert "mein Schatz" in result or "meine Liebe" in result
        # Either appears in the prohibition list (signals the rule is encoded).

    def test_medical_defer_phrasing_present(self):
        """The 'I am not a doctor' refrain must be in the prompt."""
        result = self._render()
        assert "keine Ärztin" in result
        assert "Hebamme" in result or "Ärztin" in result

    def test_red_flag_bleeding_escalation(self):
        """Heavy bleeding red flag must escalate to Notaufnahme."""
        result = self._render()
        assert "Blutung" in result
        assert "Notaufnahme" in result

    def test_red_flag_preeclampsia_escalation(self):
        """Severe headache + vision changes red flag references Präeklampsie."""
        result = self._render()
        assert "Präeklampsie" in result

    def test_red_flag_reduced_fetal_movement(self):
        """Reduced fetal movement red flag references Kindsbewegungen + Kreißsaal."""
        result = self._render()
        assert "Kindsbewegungen" in result
        assert "Kreißsaal" in result

    def test_red_flag_self_harm_routes_to_crisis(self):
        """Self-harm red flag must reference the crisis protocol."""
        result = self._render()
        assert "sich selbst zu verletzen" in result.lower() or "self-harm" in result.lower()
        # Must mention crisis/clinical routing.
        assert "crisis" in result.lower() or "Krise" in result

    def test_loss_handling_no_forward_momentum(self):
        """Loss handling must forbid 'try again' / fatalistic framing."""
        result = self._render()
        assert "Verlust" in result or "loss" in result.lower()
        # Explicit prohibition on common bad responses.
        assert "nochmal versuchen" in result or "try again" in result.lower()
        assert "aus einem Grund" in result or "for a reason" in result.lower()

    def test_loss_no_euphemism_unless_user_uses_first(self):
        """Persona avoids euphemisms like Sternenkind unless user uses them first."""
        result = self._render()
        # Sternenkind or Engelskind should appear as a 'don't use unless user does first'.
        assert "Sternenkind" in result or "Engelskind" in result

    def test_open_asks_operating_principle_present(self):
        """Open asks now carry EDD capture instead of an onboarding stanza."""
        result = self._render()
        assert "## Open asks" in result
        assert "things you" in result
        assert "need to find out from the user" in result
        assert "One per turn" in result

    def test_onboarding_first_contact_no_longer_mounts(self):
        """onboarding_state no longer mounts a separate first-contact block."""
        result_pending = self._render(onboarding_state="pending")
        result_complete = self._render(onboarding_state="complete")
        assert "First Contact" not in result_pending
        assert "First Contact" not in result_complete

    def test_boundary_redirect_relationship_to_veas(self):
        """Relationship-question boundary redirects to Véas."""
        result = self._render()
        assert "Véas" in result

    def test_boundary_no_medical_diagnosis(self):
        """Persona refuses specific medical diagnosis/treatment."""
        result = self._render()
        # The Boundaries section names diagnosis or treatment as off-limits.
        assert "diagnosis" in result.lower() or "Diagnose" in result.lower()

    def test_handles_pregnancy_state_via_hot_context_not_inferred(self):
        """Persona must tell the bot to use tools only on explicit user signal."""
        result = self._render()
        assert "Don't infer" in result or "don't infer" in result
        assert "explicitly" in result.lower()

    def test_one_question_per_reply_rule(self):
        """Persona enforces the 'one question per reply' rule."""
        result = self._render()
        assert "One question per reply" in result or "one question per reply" in result.lower()

    def test_lowercase_pregnancy_appears(self):
        """The topic word 'pregnancy' must appear (existing test compat)."""
        result = self._render()
        assert "pregnancy" in result.lower()
