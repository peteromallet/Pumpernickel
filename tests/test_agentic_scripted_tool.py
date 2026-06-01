from __future__ import annotations

import json
from pathlib import Path

from tests.agentic.runner import _sisypy_actor, main


def test_scripted_tool_mode_keeps_its_dispatcher_identity() -> None:
    assert _sisypy_actor("scripted-tool") == "scripted-tool"


def test_scripted_tool_runner_emits_required_evidence_and_stays_structural_only(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "reports"

    main(
        [
            "--mode",
            "scripted-tool",
            "--scenario",
            "structural-smoke",
            "--out-dir",
            str(out_dir),
            "--tag",
            "scripted-proof",
        ]
    )

    summary_path = out_dir / "scripted-proof-structural-smoke" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    run = summary["runs"][0]
    evidence_dir = Path(run["evidence_dir"])
    project_specific_dir = evidence_dir / "project_specific"

    for filename in (
        "tool_transcript.json",
        "hot_context.md",
        "messages_seed.json",
        "expected_behavior.json",
        "final_answer.md",
        "assertions.json",
        "infrastructure.json",
    ):
        assert (project_specific_dir / filename).is_file(), filename

    transcript = json.loads(
        (project_specific_dir / "tool_transcript.json").read_text(encoding="utf-8")
    )
    assertions = json.loads(
        (project_specific_dir / "assertions.json").read_text(encoding="utf-8")
    )
    infrastructure = json.loads(
        (project_specific_dir / "infrastructure.json").read_text(encoding="utf-8")
    )

    assert run["dispatcher"] == "scripted-tool"
    assert run["success_proof_level"] == "authored"
    assert transcript["structural_only"] is True
    assert transcript["tool_calls"], "expected real registry/capture tool calls"
    assert assertions["structural_only"] is True
    assert assertions["passed"] is True
    assert infrastructure["infrastructure_failed"] is False
