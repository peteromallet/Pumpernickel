"""Per-bot scenario corpus checks for SuperPOM.

T1: Loads all SuperPOM per-bot eval scenarios from evals/per_bot/superpom/
and verifies every scenario carries tool/outbound assertions — not just
registration or filename checks.  Also ensures the corpus tags cover the
required SuperPOM behavior matrix.

Failures here point to missing scenario content (absent coverage), not to
hidden runtime behaviour.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.scenario import Scenario, load_scenarios

SUPERPOM_SCENARIOS_DIR = (
    Path(__file__).resolve().parents[1] / "evals" / "per_bot" / "superpom"
)

# ── Required behaviours (from the M3 success criteria) ──────────────────────
#
# Every tag listed here must appear in at least one SuperPOM scenario.
REQUIRED_TAGS: set[str] = {
    "superpom",
    "compass-first",
}

# Each of these behaviour categories must be covered by at least one scenario.
# The key is a human label; the value is a set of tags that *jointly* count
# as covering the category (the scenario must match at least one of them).
REQUIRED_BEHAVIOURS: dict[str, set[str]] = {
    "clarify           (vague statement → clarifying question)": {
        "clarify",
    },
    "gentle-challenge  (name misalignment without shame)": {
        "gentle-challenge",
        "challenge",
    },
    "concrete-next-move (one clear action, no prescription)": {
        "next-move",
        "concrete-next-move",
    },
    "reflection         (Compass-grounded reflection with bot_proposed)": {
        "reflection",
        "bot-proposed",
    },
    "review-correction  (user reviews/proposes corrections)": {
        "review",
        "review-gate",
        "correction",
    },
    "completed-goals    (rendering / responding to completed goals)": {
        "completed-goals",
        "completed-goal",
    },
    "privacy-suppression (no partner-private detail leaked)": {
        "privacy",
        "suppression",
        "no-leak",
    },
    "shame-guardrail    (no moral scoring, ideal-self, perfectionism)": {
        "no-shame",
        "shame-guardrail",
        "anti-perfectionism",
    },
    "generic-advice-avoid (Coach-style sprawling advice avoided)": {
        "no-advice",
        "no-generic-advice",
        "no-prescription",
    },
    "anti-pattern        (recognising / naming anti-patterns)": {
        "anti-pattern",
    },
}

# ── Helpers ─────────────────────────────────────────────────────────────────

def _load_superpom_scenarios() -> list[Scenario]:
    """Load every SuperPOM per-bot scenario markdown file."""
    if not SUPERPOM_SCENARIOS_DIR.exists():
        pytest.skip(f"SuperPOM scenario directory missing: {SUPERPOM_SCENARIOS_DIR}")
    return load_scenarios(SUPERPOM_SCENARIOS_DIR)


# ── Structural assertions (every single scenario) ───────────────────────────

class TestEverySuperPOMScenario:
    """Structural assertions that apply to *every* SuperPOM scenario."""

    @pytest.fixture(scope="class")
    def scenarios(self) -> list[Scenario]:
        return _load_superpom_scenarios()

    def test_at_least_some_scenarios_exist(self, scenarios: list[Scenario]) -> None:
        """The directory must contain at least one SuperPOM eval scenario."""
        assert len(scenarios) > 0, (
            "No SuperPOM per-bot scenarios found. "
            "Add markdown files under evals/per_bot/superpom/."
        )

    @pytest.mark.parametrize("scenario_index", list(range(20)))  # upper bound
    def test_each_scenario_has_tool_assertions(
        self, scenarios: list[Scenario], scenario_index: int
    ) -> None:
        """Every scenario must declare must_call_tools OR must_not_call_tools."""
        if scenario_index >= len(scenarios):
            return  # parametrize bound wider than actual count — no-op
        scenario = scenarios[scenario_index]
        has_tool_assertion = (
            bool(scenario.expectations.must_call_tools)
            or bool(scenario.expectations.must_not_call_tools)
        )
        assert has_tool_assertion, (
            f"Scenario {scenario.name!r} has no tool assertions "
            f"(must_call_tools or must_not_call_tools). "
            "Every SuperPOM scenario must declare which tools are required and forbidden."
        )

    @pytest.mark.parametrize("scenario_index", list(range(20)))
    def test_each_scenario_has_outbound_assertions(
        self, scenarios: list[Scenario], scenario_index: int
    ) -> None:
        """Every scenario must have outbound_assertions (behaviour, not just tools)."""
        if scenario_index >= len(scenarios):
            return
        scenario = scenarios[scenario_index]
        assert scenario.expectations.outbound_assertions, (
            f"Scenario {scenario.name!r} has no outbound_assertions. "
            "Every SuperPOM scenario must assert on the bot's actual response text."
        )


# ── Behaviour coverage assertions ───────────────────────────────────────────

class TestSuperPOMBehaviourCoverage:
    """Behaviour-matrix coverage tests for the SuperPOM per-bot corpus."""

    @pytest.fixture(scope="class")
    def scenarios(self) -> list[Scenario]:
        return _load_superpom_scenarios()

    @pytest.fixture(scope="class")
    def all_tags(self, scenarios: list[Scenario]) -> set[str]:
        return {tag for s in scenarios for tag in s.tags}

    def test_required_tags_present(self, all_tags: set[str]) -> None:
        """Bare-minimum tags must appear in at least one scenario."""
        missing = REQUIRED_TAGS - all_tags
        assert not missing, (
            f"Required tags missing from SuperPOM corpus: {sorted(missing)}. "
            f"Tags found: {sorted(all_tags)}"
        )

    @pytest.mark.parametrize("label,required", sorted(REQUIRED_BEHAVIOURS.items()))
    def test_behaviour_tag_coverage(
        self, scenarios: list[Scenario], label: str, required: set[str]
    ) -> None:
        """Each required behaviour must match at least one scenario tag."""
        covered = any(
            required & set(s.tags) for s in scenarios
        )
        assert covered, (
            f"Missing behaviour coverage: {label!r}. "
            f"None of the {len(scenarios)} SuperPOM scenarios carry any of "
            f"the required tags {sorted(required)}. "
            "Add a scenario that exercises this behaviour with the appropriate tag(s)."
        )

    def test_every_scenario_belongs_to_superpom(self, scenarios: list[Scenario]) -> None:
        """Every scenario in the directory must include the 'superpom' tag."""
        for s in scenarios:
            assert "superpom" in s.tags, (
                f"Scenario {s.name!r} is missing the 'superpom' tag."
            )

    def test_compass_first_tag_coverage(self, scenarios: list[Scenario]) -> None:
        """At least one scenario must demonstrate 'compass-first' explicitly."""
        compass_first = [s for s in scenarios if "compass-first" in s.tags]
        assert compass_first, (
            "No SuperPOM scenario carries the 'compass-first' tag. "
            "At least one scenario must demonstrate reading the Compass first."
        )

    def test_list_orientation_items_in_must_call(self, scenarios: list[Scenario]) -> None:
        """At least one scenario must require list_orientation_items in must_call_tools."""
        found = any(
            "list_orientation_items" in s.expectations.must_call_tools
            for s in scenarios
        )
        assert found, (
            "No scenario requires list_orientation_items in must_call_tools. "
            "At least one scenario must demonstrate the Compass-first pattern."
        )


# ── Scenario-specific content checks ────────────────────────────────────────

class TestSuperPOMScenarioContent:
    """Verify that specific named scenarios exist and are well-formed."""

    @pytest.fixture(scope="class")
    def scenarios_by_name(self) -> dict[str, Scenario]:
        return {s.name: s for s in _load_superpom_scenarios()}

    def test_clarify_scenario_exists(self, scenarios_by_name: dict[str, Scenario]) -> None:
        assert "superpom-clarify-vague-statement" in scenarios_by_name, (
            "Missing scenario: superpom-clarify-vague-statement"
        )

    def test_challenge_scenario_exists(self, scenarios_by_name: dict[str, Scenario]) -> None:
        assert "superpom-gentle-challenge-misalignment" in scenarios_by_name, (
            "Missing scenario: superpom-gentle-challenge-misalignment"
        )

    def test_next_move_scenario_exists(self, scenarios_by_name: dict[str, Scenario]) -> None:
        assert "superpom-concrete-next-move" in scenarios_by_name, (
            "Missing scenario: superpom-concrete-next-move"
        )

    def test_reflection_scenario_exists(self, scenarios_by_name: dict[str, Scenario]) -> None:
        assert "superpom-compass-grounded-reflection" in scenarios_by_name, (
            "Missing scenario: superpom-compass-grounded-reflection"
        )
