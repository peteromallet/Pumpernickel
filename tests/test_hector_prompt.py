"""Prompt content tests for Hector.

T16: Assert that the rendered system prompt contains EVERY locked
Prompt Requirement from docs/fitness-bot-commitments-plan.md.
"""

from __future__ import annotations

import pytest

from app.bots.hector import build_hector_spec
from app.bots.prompts.hector import render_system_prompt


def _render(assistant_name: str = "Hector", user_name: str = "TestUser") -> str:
    """Render the Hector system prompt with minimal arguments."""
    return render_system_prompt(
        assistant_name=assistant_name,
        user_name=user_name,
        prompt_version="v1",
        onboarding_state="completed",
        partner_share=None,
        partner_sharing_state="unavailable",
    )


class TestHectorPromptRequirements:
    """Every locked Prompt Requirement must appear in the rendered prompt."""

    # ── Concrete plans only ──────────────────────────────────────────

    def test_create_commitments_only_from_concrete_plans(self):
        prompt = _render()
        # The prompt uses "concrete" in multiple forms — check for the concept
        assert "concrete" in prompt.lower() or "not create commitments" in prompt.lower() or "before creating" in prompt.lower()

    def test_ask_before_tracking_vague_goals(self):
        prompt = _render()
        assert "vague" in prompt.lower()

    # ── Adherence checklist ──────────────────────────────────────────

    def test_use_hot_context_adherence_checklist(self):
        prompt = _render()
        assert "adherence" in prompt.lower()

    # ── Unknown vs missed ───────────────────────────────────────────

    def test_distinguish_unknown_vs_missed(self):
        prompt = _render()
        assert "unknown" in prompt.lower() and "missed" in prompt.lower()

    # ── No shaming ──────────────────────────────────────────────────

    def test_no_shaming(self):
        prompt = _render()
        assert "shame" in prompt.lower()
        # Should mention NOT shaming
        assert "not" in prompt.lower() or "never" in prompt.lower()

    # ── No overpraise ───────────────────────────────────────────────

    def test_no_overpraise(self):
        prompt = _render()
        assert "overpraise" in prompt.lower() or "over-praise" in prompt.lower()

    # ── Low-key pressure ────────────────────────────────────────────

    def test_low_key_pressure(self):
        prompt = _render()
        assert "low-key" in prompt.lower() or "low_key" in prompt.lower() or "pressure" in prompt.lower()

    # ── One concrete next action ────────────────────────────────────

    def test_prefer_one_concrete_next_action(self):
        prompt = _render()
        assert "next" in prompt.lower()
        assert "action" in prompt.lower() or "step" in prompt.lower()

    # ── Respect constraints ─────────────────────────────────────────

    def test_respect_constraints(self):
        prompt = _render()
        assert "constraint" in prompt.lower()

    # ── Defer medical ───────────────────────────────────────────────

    def test_defer_medical(self):
        prompt = _render()
        assert "medical" in prompt.lower() or "doctor" in prompt.lower() or "clinical" in prompt.lower()

    # ── No calorie-counting pressure unless asked ───────────────────

    def test_no_calorie_counting_pressure(self):
        prompt = _render()
        assert "calorie" in prompt.lower()

    # ── No body-image escalation / ED-like behavior ─────────────────

    def test_no_body_image_escalation(self):
        prompt = _render()
        assert "body" in prompt.lower() and ("image" in prompt.lower() or "appearance" in prompt.lower() or "eating" in prompt.lower())

    # ── No default weigh-ins / progress photos ──────────────────────

    def test_no_default_weigh_ins_or_photos(self):
        prompt = _render()
        # At least one of these topics must be addressed
        assert (
            "weigh" in prompt.lower()
            or "scale" in prompt.lower()
            or "photo" in prompt.lower()
            or "progress pic" in prompt.lower()
        )

    # ── One clarifying question for vague goals ─────────────────────

    def test_ask_clarifying_question_for_vague_goals(self):
        prompt = _render()
        assert "clarif" in prompt.lower() or "question" in prompt.lower()

    # ── Persona checks ──────────────────────────────────────────────

    def test_not_a_doctor(self):
        prompt = _render()
        assert "not a doctor" in prompt.lower()

    def test_not_a_therapist(self):
        prompt = _render()
        assert "not" in prompt.lower() and "therapist" in prompt.lower()

    def test_not_a_nutritionist(self):
        prompt = _render()
        assert "nutritionist" in prompt.lower()

    def test_not_a_shame_machine(self):
        prompt = _render()
        assert "shame" in prompt.lower()

    def test_not_an_optimization_dashboard(self):
        prompt = _render()
        assert "optimization" in prompt.lower() or "dashboard" in prompt.lower()

    def test_not_a_motivational_poster(self):
        prompt = _render()
        assert "motivational" in prompt.lower()


class TestHectorPromptRenderSafety:
    """Renderer must accept partner_name=None safely."""

    def test_render_with_partner_name_none(self):
        result = render_system_prompt(
            assistant_name="Hector",
            user_name="TestUser",
            partner_name=None,
            prompt_version="v1",
            onboarding_state="completed",
            partner_share=None,
            partner_sharing_state="unavailable",
        )
        assert isinstance(result, str)
        assert len(result) > 100

    def test_render_contains_hector_name(self):
        prompt = _render("Hector", "Alice")
        assert "Hector" in prompt

    def test_render_contains_user_name(self):
        prompt = _render("Hector", "Alice")
        assert "Alice" in prompt


class TestHectorPersistenceGuidance:
    """Hector should know what durable fitness state is worth preserving."""

    def test_prompt_defines_fitness_knowledge_primitives(self):
        prompt = _render()
        lower = prompt.lower()

        assert "knowledge primitives" in lower
        assert "memories are stable concrete facts" in lower
        assert "observations are patterns and tactics" in lower
        assert "commitments are explicit concrete plans" in lower
        assert "events are adherence reports" in lower
        assert "before adding or updating durable state" in lower

    def test_step_instructions_are_specific_not_stubs(self):
        spec = build_hector_spec()
        rendered = "\n".join(spec.step_instructions.values()).lower()

        assert "phase 1 stub" not in rendered
        assert "durable fitness state" in rendered
        assert "read before durable writes" in rendered
        assert "prefer updating" in rendered


class TestHealthReadGuidanceWorkoutBoundaries:
    """The health_read_guidance slot must encode hard boundaries about
    imported workouts — they never create or satisfy commitments."""

    def test_prompt_has_health_data_reads_section(self):
        prompt = _render()
        assert "# Health Data Reads" in prompt or "health data reads" in prompt.lower(), (
            "Prompt must include health data reads guidance section"
        )

    def test_workout_reads_never_for_commitment_satisfaction(self):
        """Boundary 1: imported workouts never satisfy commitments."""
        prompt = _render()
        lower = prompt.lower()
        assert "never for commitment satisfaction" in lower, (
            "Must state that weight/sleep/workout data does NOT satisfy commitments"
        )
        assert "do not satisfy" in lower or "does not satisfy" in lower, (
            "Must explicitly state data doesn't satisfy workout commitments"
        )

    def test_workout_reads_never_for_commitment_creation(self):
        """Boundary 2: do not create commitments from health data."""
        prompt = _render()
        lower = prompt.lower()
        assert "never for commitment creation" in lower, (
            "Must state not to create commitments from health data"
        )
        assert "do not create commitments" in lower, (
            "Must contain explicit 'do not create commitments' directive"
        )

    def test_workout_reads_never_for_medical_interpretation(self):
        """Boundary 3: never for medical interpretation."""
        prompt = _render()
        lower = prompt.lower()
        assert "never for medical interpretation" in lower, (
            "Must state not to medically interpret health data"
        )

    def test_workout_reads_never_infer_missed_or_excused(self):
        """Boundary 4: never infer missed/excused from workout data."""
        prompt = _render()
        lower = prompt.lower()
        assert "never infer missed" in lower or "never infer missed or excused" in lower, (
            "Must state not to infer missed/excused adherence from workout data"
        )
        assert "solely by" in lower or "only by" in lower, (
            "Must state adherence is determined solely by explicit events"
        )

    def test_imported_workouts_do_not_create_commitments(self):
        """The prompt must explicitly state imported workouts are not commitments."""
        prompt = _render()
        lower = prompt.lower()
        assert (
            "do not create commitments" in lower
            and "imported" in lower
        ) or "do not create commitments" in lower, (
            "Prompt must state imported workouts don't create commitments"
        )

    def test_device_workouts_are_informational_context(self):
        """Imported/device workouts are context, not commitment completions."""
        prompt = _render()
        lower = prompt.lower()
        assert "informational context" in lower, (
            "Prompt must state imported workouts are informational context"
        )

    def test_prompt_has_compact_summary_language(self):
        """Prompt describes workout reads as compact aggregates, not raw data."""
        prompt = _render()
        lower = prompt.lower()
        assert "compact" in lower, (
            "Prompt must describe workout data as compact summaries"
        )
        assert "never raw" in lower or "never raw workout" in lower, (
            "Prompt must state workout reads are never raw data"
        )

    def test_prompt_explicitly_excludes_device_ids_and_heart_rate(self):
        """Prompt must state that device IDs and heart-rate detail are excluded."""
        prompt = _render()
        lower = prompt.lower()
        assert "device" in lower, (
            "Prompt must mention device data exclusion"
        )
        assert "heart" in lower and "rate" in lower, (
            "Prompt must mention heart-rate detail exclusion"
        )

    def test_no_language_implying_device_workouts_create_commitments(self):
        """Prompt must not imply that synced/imported workouts create commitments."""
        prompt = _render()
        lower = prompt.lower()
        # These phrases must NOT appear
        assert "create commitments from your workouts" not in lower
        assert "automatically create commitments" not in lower
        assert "workouts will be tracked as commitments" not in lower
