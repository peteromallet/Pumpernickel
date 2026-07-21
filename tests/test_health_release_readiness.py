"""Document contract test: release-readiness proof-map integrity.

Asserts that the release-readiness proof-map:
  1. References all three upstream handoff contracts.
  2. Maps core health test selectors.
  3. Does NOT claim any of the forbidden rollout milestones
     (live rollout, production enablement, vendor approval,
      legal review completion, completed dogfood).
"""

from __future__ import annotations

from pathlib import Path

import pytest

PROOF_MAP_PATH = Path(__file__).resolve().parent.parent / "docs" / "health" / "release-readiness-proof.md"

HANDOFF_CONTRACTS = (
    "docs/health/withings-provider-contract.md",
    "docs/health/weight-sleep-read-model-contract.md",
    "docs/health/workout-projection-contract.md",
)

# Patterns that would indicate a positive claim of completion (as opposed to
# a gap or prerequisite).  The document may mention any of these concepts
# in gap rows or descriptive prose; we only flag lines where the phrase
# reads as an affirmative completion claim.
FORBIDDEN_CLAIM_PATTERNS = (
    # Live rollout
    "rollout is live",
    "rollout has been completed",
    "rollout is complete",
    "rollout completed",
    "fully rolled out",
    # Production enablement
    "production is enabled",
    "production enablement complete",
    # Vendor approval (as completed, not as requirement)
    "vendor approval obtained ✅",
    "vendor approval ✅",
    "vendor approved ✅",
    "vendor approval: complete",
    "vendor approval: done",
    # Legal review
    "legal review complete",
    "legal review completed",
    "legal review ✅",
    "legal approved",
    "legal review: done",
    # Dogfood
    "dogfood complete",
    "dogfood completed",
    "dogfood is complete",
    "dogfood ✅",
)


# -- Helpers ----------------------------------------------------------------


def _read_proof_map() -> str:
    """Return the raw markdown content of the release-readiness proof-map."""
    if not PROOF_MAP_PATH.is_file():
        pytest.fail(f"Proof-map not found at {PROOF_MAP_PATH}")
    return PROOF_MAP_PATH.read_text(encoding="utf-8")


def _find_lines(text: str, phrase: str) -> list[str]:
    """Return all lines in *text* that contain *phrase* (case-insensitive)."""
    lower = text.lower()
    return [
        line
        for line in text.splitlines()
        if phrase.lower() in line.lower()
    ]


# -- Contract reference assertions ------------------------------------------


class TestProofMapReferencesHandoffContracts:
    """The proof-map must reference all three settled handoff contracts."""

    def test_references_withings_provider_contract(self) -> None:
        text = _read_proof_map()
        assert "withings-provider-contract.md" in text, (
            "Proof-map must reference docs/health/withings-provider-contract.md"
        )

    def test_references_weight_sleep_read_model_contract(self) -> None:
        text = _read_proof_map()
        assert "weight-sleep-read-model-contract.md" in text, (
            "Proof-map must reference docs/health/weight-sleep-read-model-contract.md"
        )

    def test_references_workout_projection_contract(self) -> None:
        text = _read_proof_map()
        assert "workout-projection-contract.md" in text, (
            "Proof-map must reference docs/health/workout-projection-contract.md"
        )

    def test_three_handoff_contracts_present(self) -> None:
        """All three contract filenames found in the proof-map."""
        text = _read_proof_map()
        for contract in HANDOFF_CONTRACTS:
            assert contract in text, (
                f"Proof-map missing reference to {contract}"
            )


class TestProofMapReferencesHealthSelectors:
    """The proof-map must reference core health test selectors."""

    def test_references_provider_contract_selectors(self) -> None:
        """At least one provider-contract test selector is mapped."""
        text = _read_proof_map()
        assert "test_provider_protocol_stays_minimal_and_withings_shaped" in text, (
            "Proof-map must reference the provider-protocol test selector"
        )

    def test_references_weight_sleep_selectors(self) -> None:
        """At least one weight/sleep test selector is mapped."""
        text = _read_proof_map()
        assert "test_decode_withings_value" in text, (
            "Proof-map must reference a weight/sleep normalizer selector"
        )

    def test_references_workout_projection_selectors(self) -> None:
        """At least one workout-projection test selector is mapped."""
        text = _read_proof_map()
        assert "test_known_category_maps_to_label" in text or (
            "TestResolveWorkoutType" in text
        ), "Proof-map must reference a workout-projection test selector"

    def test_references_measurement_repository_selectors(self) -> None:
        """At least one measurement-repository test selector is mapped."""
        text = _read_proof_map()
        assert "test_replace_normalized_measurements_inserts_rows" in text, (
            "Proof-map must reference a measurement-repository selector"
        )

    def test_references_sleep_repository_selectors(self) -> None:
        """At least one sleep-repository test selector is mapped."""
        text = _read_proof_map()
        assert "test_replace_normalized_sleep_inserts_row" in text, (
            "Proof-map must reference a sleep-repository selector"
        )

    def test_references_projection_applicator_selectors(self) -> None:
        """At least one projection-applicator test selector is mapped."""
        text = _read_proof_map()
        assert "test_disabled_returns_none" in text, (
            "Proof-map must reference an applicator test selector"
        )


class TestProofMapDoesNotClaimRolloutCompletion:
    """The proof-map must NOT claim any completed rollout milestone.

    Gaps and pending-live-validation items are fine; positive
    assertions of completion are forbidden.
    """

    @pytest.mark.parametrize("pattern", FORBIDDEN_CLAIM_PATTERNS)
    def test_no_positive_completion_claim(self, pattern: str) -> None:
        """Check no forbidden claim-of-completion pattern appears."""
        text = _read_proof_map()
        matching_lines = _find_lines(text, pattern)
        if matching_lines:
            lines_str = "\n    ".join(matching_lines)
            pytest.fail(
                f"Proof-map must not contain completion-claim pattern "
                f"{pattern!r}.\nFound in line(s):\n    {lines_str}"
            )

    def test_does_not_claim_rollout_is_live(self) -> None:
        """Readiness proof must not say rollout is live."""
        text = _read_proof_map().lower()
        # Acceptable: describing what would be needed; not acceptable: claiming
        # rollout is live. Check for positive assertions.
        assert "rollout" not in text or all(
            # Allow "rollout-readiness" in the purpose line
            # Allow "blocks production rollout" (gap description)
            phrase not in text
            for phrase in (
                "rollout is live",
                "rollout has been completed",
                "rollout is complete",
            )
        ), "Proof-map must not claim rollout is live"

    def test_gaps_are_not_labeled_complete(self) -> None:
        """Every gap entry must still be marked as pending/blocked, not done."""
        text = _read_proof_map()
        # Quick sanity: the gap summary sections should exist
        assert "### 4.1 Automated Test Gaps" in text
        assert "### 4.2 Pending Live Validation Gaps" in text
        assert "❌" in text or "🔴" in text or "⚪" in text or "⚠️" in text, (
            "Proof-map must mark gaps with appropriate status icons"
        )
