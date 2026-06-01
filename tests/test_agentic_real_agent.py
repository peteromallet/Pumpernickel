from __future__ import annotations

import json
from pathlib import Path

from tests.agentic.real_agent import (
    write_real_agent_evidence,
)
from tests.agentic.runner import main


def test_real_agent_runner_emits_eval_execution_evidence(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTIC_SKIP_LLM_ASSESSOR", "1")
    out_dir = tmp_path / "reports"

    main(
        [
            "--mode",
            "real-agent",
            "--scenario",
            "structural-smoke",
            "--out-dir",
            str(out_dir),
            "--tag",
            "real-proof",
        ]
    )

    summary = json.loads(
        (out_dir / "real-proof-structural-smoke" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    run = summary["runs"][0]
    project_specific_dir = Path(run["evidence_dir"]) / "project_specific"
    transcript = json.loads(
        (project_specific_dir / "tool_transcript.json").read_text(encoding="utf-8")
    )
    assertions = json.loads(
        (project_specific_dir / "assertions.json").read_text(encoding="utf-8")
    )
    infrastructure = json.loads(
        (project_specific_dir / "infrastructure.json").read_text(encoding="utf-8")
    )

    assert run["dispatcher"] == "real-agent"
    assert run["success_proof_level"] == "validated"
    assert transcript["mode"] == "real-agent"
    assert transcript["source"] == "evals.execution.run_eval_turn"
    assert transcript["tool_calls"], "expected capture_tool_calls transcript entries"
    assert assertions["passed"] is True
    assert infrastructure["infrastructure_failed"] is False


def test_recorded_real_runner_regrades_frozen_real_agent_transcript(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTIC_SKIP_LLM_ASSESSOR", "1")
    source_dir = tmp_path / "frozen" / "project_specific"
    write_real_agent_evidence(output_dir=source_dir)

    out_dir = tmp_path / "reports"
    main(
        [
            "--mode",
            "recorded-real",
            "--scenario",
            "structural-smoke",
            "--out-dir",
            str(out_dir),
            "--tag",
            "recorded-proof",
            "--recorded-source",
            str(source_dir),
        ]
    )

    summary = json.loads(
        (out_dir / "recorded-proof-structural-smoke" / "summary.json").read_text(
            encoding="utf-8"
        )
    )
    run = summary["runs"][0]
    project_specific_dir = Path(run["evidence_dir"]) / "project_specific"
    transcript = json.loads(
        (project_specific_dir / "tool_transcript.json").read_text(encoding="utf-8")
    )
    assertions = json.loads(
        (project_specific_dir / "assertions.json").read_text(encoding="utf-8")
    )

    assert run["dispatcher"] == "recorded-real"
    assert run["success_proof_level"] == "validated"
    assert transcript["mode"] == "recorded-real"
    assert transcript["source"] == str(source_dir)
    assert assertions["mode"] == "recorded-real"
    assert assertions["passed"] is True


def test_real_agent_infrastructure_errors_land_in_infrastructure_json(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "infra"
    payload = write_real_agent_evidence(
        output_dir=output_dir,
        case_ids=["real-agent-turncontext-incompatibility"],
    )

    infrastructure = payload["infrastructure"]
    assertions = payload["assertions"]

    assert infrastructure["infrastructure_failed"] is True
    assert infrastructure["issues"][0]["kind"] == "turn_context_incompatibility"
    assert assertions["assertions"][0]["case_id"] == "real-agent-turncontext-incompatibility"
    assert assertions["assertions"][0]["passed"] is True
