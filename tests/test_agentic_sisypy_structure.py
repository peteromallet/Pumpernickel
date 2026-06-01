"""Structural validation tests for M4 Sisypy agent-behavior harness.

Covers:
  - All scenario YAML files load.
  - Briefs exist for every scenario that expects one.
  - Fixture case IDs are unique.
  - Every enforced rubric names at least one concrete evidence file.
  - The compatibility shim cannot satisfy behavior pass criteria.
  - Missing evidence is classified as ``undetermined`` (never ``passed``).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths under test
# ---------------------------------------------------------------------------

SCENARIOS_DIR = Path("tests/agentic/scenarios")
BRIEFS_DIR = Path("tests/agentic/briefs")

# ---------------------------------------------------------------------------
# Scenario loading helpers
# ---------------------------------------------------------------------------


def _collect_scenario_yaml_paths() -> list[Path]:
    """Return every .yaml file under the scenarios directory."""
    return sorted(SCENARIOS_DIR.glob("*.yaml"))


def _load_scenario(yaml_path: Path):
    """Load a single scenario YAML via Sisypy (or compat shim)."""
    try:
        from sisypy.runner import _load_scenario  # type: ignore[import-untyped]
    except ImportError:
        from tests.agentic.sisypy_compat import _emit_diagnostic

        _emit_diagnostic()
        pytest.skip("Sisypy package not available — cannot load scenarios.")
        return None  # unreachable

    return _load_scenario(yaml_path)


def _load_scenarios_from_dir(scenarios_dir: Path, briefs_dir: Path | None = None):
    """Load all scenario YAML files from a directory via Sisypy."""
    try:
        from sisypy.runner import _load_scenarios_from_dir  # type: ignore[import-untyped]
    except ImportError:
        pytest.skip("Sisypy package not available — cannot load scenarios.")
        return []  # unreachable

    return _load_scenarios_from_dir(scenarios_dir, briefs_dir=briefs_dir)


# ---------------------------------------------------------------------------
# Concrete evidence file references expected in enforced rubrics
# ---------------------------------------------------------------------------

_EVIDENCE_FILE_PATTERNS: list[str] = [
    r"project_specific/tool_transcript\.json",
    r"project_specific/final_answer\.md",
    r"project_specific/hot_context\.md",
    r"project_specific/infrastructure\.json",
    r"project_specific/assertions\.json",
    r"project_specific/messages_seed\.json",
    r"project_specific/expected_behavior\.json",
]

_COMPILED_EVIDENCE_RE = re.compile(
    "|".join(_EVIDENCE_FILE_PATTERNS), re.IGNORECASE
)

# Implicit evidence file references — rubrics may mention these terms without
# spelling out the full ``project_specific/`` path.
_IMPLICIT_EVIDENCE_PATTERNS: list[tuple[str, str]] = [
    (r"\btranscript\b", "project_specific/tool_transcript.json"),
    (r"\btool_transcript\b", "project_specific/tool_transcript.json"),
    (r"\bfinal.answer\b", "project_specific/final_answer.md"),
    (r"\bfinal_answer\b", "project_specific/final_answer.md"),
    (r"\bhot.context\b", "project_specific/hot_context.md"),
    (r"\bhot_context\b", "project_specific/hot_context.md"),
    (r"\binfrastructure\b", "project_specific/infrastructure.json"),
]

_IMPLICIT_RES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat, re.IGNORECASE), mapped)
    for pat, mapped in _IMPLICIT_EVIDENCE_PATTERNS
]


def _extract_evidence_files_from_rubric(text: str) -> set[str]:
    """Return the set of evidence file paths mentioned in a rubric text."""
    found: set[str] = set()
    # Explicit paths
    for match in _COMPILED_EVIDENCE_RE.finditer(text):
        raw = match.group(0)
        # Normalise to preferred form
        if not raw.startswith("project_specific/"):
            raw = "project_specific/" + raw
        found.add(raw)
    # Implicit references
    for compiled, mapped in _IMPLICIT_RES:
        if compiled.search(text):
            found.add(mapped)
    return found


# ==========================================================================
# Tests
# ==========================================================================


class TestScenarioYAMLFilesLoad:
    """Every scenario YAML must load without error."""

    @pytest.mark.parametrize(
        "yaml_path",
        [p for p in _collect_scenario_yaml_paths()],
        ids=lambda p: p.stem,
    )
    def test_scenario_yaml_loads(self, yaml_path: Path) -> None:
        scenario = _load_scenario(yaml_path)
        assert scenario is not None, f"Failed to load {yaml_path}"
        assert scenario.name, f"Scenario in {yaml_path} has no name"
        assert scenario.tier >= 1, f"Scenario {scenario.name} has invalid tier"
        # Every scenario must have at least one agent
        assert len(scenario.agents) >= 1, (
            f"Scenario {scenario.name} has no agents"
        )


class TestBriefsExist:
    """Every scenario that is not a bare structural smoke test should have
    a corresponding brief markdown file."""

    # Scenarios that intentionally have no brief (minimal structural smokes)
    _SMOKE_WITHOUT_BRIEF: frozenset[str] = frozenset(
        {"structural-smoke", "positional-scripted-smoke"}
    )

    @pytest.mark.parametrize(
        "yaml_path",
        [p for p in _collect_scenario_yaml_paths()],
        ids=lambda p: p.stem,
    )
    def test_brief_exists(self, yaml_path: Path) -> None:
        scenario = _load_scenario(yaml_path)
        brief_path = BRIEFS_DIR / f"{scenario.name}.md"

        if scenario.name in self._SMOKE_WITHOUT_BRIEF:
            # Smoke scenarios are allowed to omit briefs.
            # Just verify the scenario loaded with brief=None fine.
            try:
                from sisypy.runner import _load_scenario as _sisypy_load
            except ImportError:
                pytest.skip("Sisypy unavailable")
            reloaded = _sisypy_load(yaml_path)  # no briefs_dir
            assert reloaded.name == scenario.name
            return

        assert brief_path.is_file(), (
            f"Brief file missing for scenario '{scenario.name}': "
            f"expected at {brief_path}"
        )
        # Additionally, the brief must be non-empty
        content = brief_path.read_text(encoding="utf-8").strip()
        assert content, f"Brief file for '{scenario.name}' is empty"


class TestFixtureIDsAreUnique:
    """All fixture case IDs in SEARCH_NAV_CASES must be unique."""

    def test_fixture_case_ids_unique(self) -> None:
        from tests.agentic.fixtures.search_nav_cases import SEARCH_NAV_CASES

        case_ids = [case["id"] for case in SEARCH_NAV_CASES.values()]
        duplicates = [cid for cid in case_ids if case_ids.count(cid) > 1]
        assert not duplicates, (
            f"Duplicate fixture case IDs found: {sorted(set(duplicates))}"
        )

        # Verify each case has a non-empty, well-formed id field
        for key, case in SEARCH_NAV_CASES.items():
            cid = case.get("id", "")
            assert cid, f"Case '{key}' has no 'id' field"
            # ID should start with a consistent prefix and use kebab-case
            assert cid.startswith("search-nav-"), (
                f"Case '{key}' id '{cid}' does not start with 'search-nav-'"
            )
            assert " " not in cid, (
                f"Case '{key}' id '{cid}' contains spaces"
            )
            # ID must be a simple kebab-case identifier
            assert re.fullmatch(r"[a-z0-9\-]+", cid), (
                f"Case '{key}' id '{cid}' is not valid kebab-case"
            )


class TestEnforcedRubricsNameConcreteEvidenceFiles:
    """Every enforced rubric in every scenario must name at least one
    concrete evidence file path (project_specific/...)."""

    @pytest.mark.parametrize(
        "yaml_path",
        [p for p in _collect_scenario_yaml_paths()],
        ids=lambda p: p.stem,
    )
    def test_enforced_rubrics_reference_evidence_files(
        self, yaml_path: Path
    ) -> None:
        scenario = _load_scenario(yaml_path)
        assessment = getattr(scenario, "assessment", None)
        if assessment is None:
            return  # no assessment section — fine for minimal smokes

        enforced = getattr(assessment, "enforced", []) or []
        if not enforced:
            # Scenarios without enforced rubrics are allowed (e.g. structural-smoke)
            return

        for i, rubric_text in enumerate(enforced):
            if not isinstance(rubric_text, str) or not rubric_text.strip():
                continue

            evidence_files = _extract_evidence_files_from_rubric(rubric_text)
            assert evidence_files, (
                f"Scenario '{scenario.name}' enforced rubric {i + 1} "
                f"does not name any concrete evidence file. "
                f"Rubric text: {rubric_text[:120]}..."
            )


class TestCompatibilityShimCannotSatisfyBehaviorCriteria:
    """The diagnostic compatibility shim must explicitly gate itself as
    incapable of satisfying behavior pass criteria."""

    def test_shim_declares_incapable(self) -> None:
        from tests.agentic.sisypy_compat import _SISYPY_UNAVAILABLE_MSG

        msg = _SISYPY_UNAVAILABLE_MSG.lower()
        # Key gating text
        assert "cannot satisfy" in msg, (
            "Shim message must say CANNOT satisfy behavior criteria"
        )
        assert "undetermined" in msg, (
            "Shim message must classify behavior scenarios as 'undetermined'"
        )

    def test_fake_project_adapter_never_passes_behavior(self) -> None:
        from tests.agentic.sisypy_compat import (
            EvidencePack,
            FakeProjectAdapter,
            Scenario,
            SuccessProofLevel,
        )

        adapter = FakeProjectAdapter()
        scenario = Scenario(name="test-behavior", tier=2)
        evidence_pack = EvidencePack(manifest={})

        result = adapter.classify_success(scenario, evidence_pack)
        # FakeProjectAdapter MUST return AUTHORED — the lowest possible level,
        # which is below VALIDATED and cannot satisfy behavior criteria.
        assert result == SuccessProofLevel.AUTHORED, (
            f"FakeProjectAdapter.classify_success returned {result}, "
            f"expected AUTHORED ({SuccessProofLevel.AUTHORED}). "
            f"The compat shim must never claim behavior success."
        )

        # AUTHORED must sort below VALIDATED in the proof-level ordering
        levels = list(SuccessProofLevel)
        assert levels.index(SuccessProofLevel.AUTHORED) < levels.index(
            SuccessProofLevel.VALIDATED
        ), (
            "SuccessProofLevel.AUTHORED must precede VALIDATED so that "
            "the compat shim's AUTHORED return cannot satisfy behavior criteria."
        )

    def test_shim_command_policy_allows_all(self) -> None:
        """The compat shim's command_policy should enforce=False (diagnostic only)."""
        from tests.agentic.sisypy_compat import (
            ActorRun,
            FakeProjectAdapter,
            Scenario,
        )

        adapter = FakeProjectAdapter()
        scenario = Scenario(name="test")
        run = ActorRun(dispatcher="fake", mode="structural")
        policy = adapter.command_policy(scenario, run)
        # The shim's command policy must not enforce restrictions
        assert policy["enforce"] is False, (
            f"Shim command_policy enforce={policy['enforce']}, expected False"
        )
        # allow_patterns should include a catch-all
        assert any(".*" in pat for pat in policy.get("allow_patterns", [])), (
            "Shim allow_patterns must include a catch-all '.*'"
        )


class TestMissingEvidenceClassifiedAsUndetermined:
    """Every evidence check helper must return ``undetermined`` (not
    ``passed``) when the required evidence file is missing."""

    def test_required_tool_used_missing_transcript(self, tmp_path: Path) -> None:
        from tests.agentic.checks import required_tool_used

        result = required_tool_used(evidence_dir=tmp_path, required_tools={"messages_before"})
        assert result["passed"] is False, f"Expected passed=False, got {result}"
        assert result.get("undetermined") is True, (
            f"Expected undetermined=True, got {result}"
        )
        assert "missing" in result.get("detail", "").lower() or result.get(
            "missing_path"
        ), (
            f"Expected mention of missing evidence, got {result}"
        )

    def test_forbidden_tool_absent_missing_transcript(
        self, tmp_path: Path
    ) -> None:
        from tests.agentic.checks import forbidden_tool_absent

        result = forbidden_tool_absent(evidence_dir=tmp_path, forbidden_tools={"search"})
        assert result["passed"] is False
        assert result.get("undetermined") is True

    def test_tool_args_match_missing_transcript(self, tmp_path: Path) -> None:
        from tests.agentic.checks import tool_args_match

        result = tool_args_match(
            evidence_dir=tmp_path,
            tool_name="messages_before",
            expected_args={"anchor": "current"},
        )
        assert result["passed"] is False
        assert result.get("undetermined") is True

    def test_message_ids_returned_missing_transcript(
        self, tmp_path: Path
    ) -> None:
        from tests.agentic.checks import message_ids_returned

        result = message_ids_returned(evidence_dir=tmp_path, expected_message_ids=["m01"])
        assert result["passed"] is False
        assert result.get("undetermined") is True

    def test_quote_present_missing_final_answer(self, tmp_path: Path) -> None:
        from tests.agentic.checks import quote_present

        result = quote_present(evidence_dir=tmp_path, expected_quotes=["hello"])
        assert result["passed"] is False
        assert result.get("undetermined") is True

    def test_suppressed_ids_absent_missing_evidence(
        self, tmp_path: Path
    ) -> None:
        from tests.agentic.checks import suppressed_ids_absent

        result = suppressed_ids_absent(evidence_dir=tmp_path, suppressed_ids={"m25"})
        assert result["passed"] is False
        assert result.get("undetermined") is True

    def test_deeper_context_called_missing_transcript(
        self, tmp_path: Path
    ) -> None:
        from tests.agentic.checks import deeper_context_called_before_answer

        result = deeper_context_called_before_answer(
            evidence_dir=tmp_path,
            deepen_tools={"messages_before"},
        )
        assert result["passed"] is False
        assert result.get("undetermined") is True

    def test_handled_recoverably_missing_transcript(
        self, tmp_path: Path
    ) -> None:
        from tests.agentic.checks import handled_recoverably

        result = handled_recoverably(evidence_dir=tmp_path)
        assert result["passed"] is False
        assert result.get("undetermined") is True

    def test_evidence_file_present_missing(self, tmp_path: Path) -> None:
        from tests.agentic.checks import evidence_file_present

        result = evidence_file_present(
            evidence_dir=tmp_path, filename="tool_transcript.json"
        )
        assert result["passed"] is False
        assert result.get("undetermined") is True
