"""Regression coverage for the canonical SuperPOM chain profile."""

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
INITIATIVE = ROOT / ".megaplan" / "initiatives" / "superpom-reflections"
CHAIN_PATH = INITIATIVE / "chain.yaml"
README_PATH = INITIATIVE / "README.md"
EXPECTED_PROFILE = "partnered-5"
AFFECTED_MILESTONES = {
    "m2-capture",
    "m3-knowledge-retrieval",
    "m4-hardening-ship",
}


def _parsed_milestones() -> dict[str, dict[str, object]]:
    payload = yaml.safe_load(CHAIN_PATH.read_text(encoding="utf-8"))
    return {milestone["label"]: milestone for milestone in payload["milestones"]}


def test_superpom_m2_through_m4_are_locked_to_partnered_5() -> None:
    milestones = _parsed_milestones()

    assert AFFECTED_MILESTONES <= milestones.keys()
    assert {
        milestones[label]["profile"] for label in AFFECTED_MILESTONES
    } == {EXPECTED_PROFILE}


def test_superpom_canonical_inputs_do_not_reference_recovery_profile() -> None:
    chain_text = CHAIN_PATH.read_text(encoding="utf-8")
    rubric_text = README_PATH.read_text(encoding="utf-8")

    assert "superpom-deepseek-recovery" not in chain_text
    assert rubric_text.count("`partnered-5/full/medium`") == 4
