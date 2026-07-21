"""Focused T19 checks for reflection eval fixtures and documented deferrals."""

from __future__ import annotations

from pathlib import Path

from evals.scenario import Scenario, load_scenarios


ROOT = Path(__file__).resolve().parents[1]
SUPERPOM_SCENARIOS_DIR = ROOT / "evals" / "per_bot" / "superpom"
RELEASE_EVIDENCE_PATH = ROOT / "docs" / "reflections_m4_release_evidence.md"


def _scenarios_by_name() -> dict[str, Scenario]:
    return {scenario.name: scenario for scenario in load_scenarios(SUPERPOM_SCENARIOS_DIR)}


class TestReflectionFixtureMatrix:
    def test_finalized_fixture_matrix_is_present(self) -> None:
        scenarios = _scenarios_by_name()

        expected = {
            "explicit-reflection": "superpom-compass-grounded-reflection",
            "implicit-reflection": "superpom-implicit-pattern-reflection",
            "voice-derived": "superpom-voice-checkpoint-reflection",
            "temporal-content-override": "superpom-temporal-content-override",
            "sensitive-content-negative": "superpom-privacy-suppression",
            "no-proactive-outreach": "superpom-no-proactive-outreach",
        }

        for tag, scenario_name in expected.items():
            scenario = scenarios.get(scenario_name)
            assert scenario is not None, f"Missing focused reflection fixture: {scenario_name}"
            assert tag in scenario.tags, (
                f"Scenario {scenario_name!r} must carry tag {tag!r}; "
                f"tags found: {scenario.tags}"
            )
            assert scenario.expectations.outbound_assertions, (
                f"Scenario {scenario_name!r} must declare outbound assertions"
            )

    def test_voice_fixture_declares_voice_inbound_metadata(self) -> None:
        scenario = _scenarios_by_name()["superpom-voice-checkpoint-reflection"]

        assert len(scenario.inbound) == 1
        inbound = scenario.inbound[0]
        assert inbound.media_type == "voice"
        assert inbound.media_url
        assert inbound.media_duration_seconds and inbound.media_duration_seconds > 0
        assert "checkpoint" in inbound.text.lower()

    def test_temporal_fixture_anchors_month_scope_from_content(self) -> None:
        scenario = _scenarios_by_name()["superpom-temporal-content-override"]

        assert "month-scope" in scenario.tags
        assert "this whole month" in scenario.inbound[0].text.lower()
        assert any(
            "month-long pattern" in assertion
            for assertion in scenario.expectations.outbound_assertions
        )

    def test_no_proactive_fixture_stays_negative_and_direct(self) -> None:
        scenario = _scenarios_by_name()["superpom-no-proactive-outreach"]

        assert "negative" in scenario.tags
        assert "direct-response" in scenario.tags
        assert not scenario.expectations.must_call_tools
        assert any(
            "does not invite the user to start a reflection" in assertion
            for assertion in scenario.expectations.outbound_assertions
        )


class TestReleaseEvidenceDeferrals:
    def test_release_evidence_records_deferred_fixture_reasons(self) -> None:
        text = RELEASE_EVIDENCE_PATH.read_text(encoding="utf-8")
        normalized = text.lower()

        required_phrases = (
            "deferred fixture targets",
            "retry deduplication",
            "deletion visibility",
            "operator redaction",
            "persisted retry/restart state transitions",
            "deleted-source reflection rows",
            "downstream derived",
            "captures `/admin/reflections` html",
        )
        for phrase in required_phrases:
            assert phrase.lower() in normalized, (
                f"Release evidence missing deferred-fixture note: {phrase}"
            )
