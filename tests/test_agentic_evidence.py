from __future__ import annotations

import json
from pathlib import Path

from tests.agentic.adapter import ActorRun, Scenario, VeasProjectAdapter
from tests.agentic.runner import main as runner_main

try:
    from sisypy.schema import EvidencePack, RunMode  # type: ignore[import-untyped]
except ImportError:
    from tests.agentic.sisypy_compat import EvidencePack, RunMode


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_generic_evidence(evidence_dir: Path) -> None:
    _write(evidence_dir / "git_diff.patch", "diff --git a/a b/a\n")
    _write(evidence_dir / "report.md", "1. Evidence\n2. Outcome\n")
    _write(evidence_dir / "actions.jsonl", '{"action_id":"0001","command":"pytest"}\n')


def _seed_project_specific_outputs(workdir: Path, *, infra_ok: bool) -> None:
    _write(workdir / "tool_transcript.json", '{"tool_calls":[]}\n')
    _write(workdir / "hot_context.md", "## Hot context\n")
    _write(workdir / "messages_seed.json", '{"messages":[]}\n')
    _write(workdir / "expected_behavior.json", '{"must":[]}\n')
    _write(workdir / "final_answer.md", "Final answer\n")
    _write(workdir / "assertions.json", '{"assertions":[]}\n')
    _write(
        workdir / "infrastructure.json",
        json.dumps(
            {
                "ok": infra_ok,
                "status": "ok" if infra_ok else "timeout",
                "reason": "db unavailable" if not infra_ok else "",
            }
        ),
    )


def _scenario() -> Scenario:
    return Scenario(
        name="evidence-smoke",
        extras={"fixture_files": ["tests/agentic/fixtures/__init__.py"]},
    )


def test_capture_freezes_repo_and_satisfies_evidence_contract(tmp_path: Path) -> None:
    adapter = VeasProjectAdapter()
    evidence_dir = tmp_path / "evidence"
    workdir = tmp_path / "workdir"
    _seed_generic_evidence(evidence_dir)
    _seed_project_specific_outputs(workdir, infra_ok=True)

    run = ActorRun(
        dispatcher="recorded-real",
        mode=RunMode.LIVE,
        workdir=str(workdir),
    )
    scenario = _scenario()

    adapter.capture(scenario, run, evidence_dir)

    assert (
        evidence_dir / "project_specific" / "frozen_repo" / "tool_schemas.py"
    ).is_file()
    assert (
        evidence_dir
        / "project_specific"
        / "fixtures"
        / "tests/agentic/fixtures/__init__.py"
    ).is_file()
    assert (evidence_dir / "project_specific" / "tool_transcript.json").is_file()

    checks = adapter.project_universal_checks(scenario, evidence_dir)
    assert checks["generic_evidence"]["passed"] is True
    assert checks["project_specific_evidence"]["passed"] is True
    assert checks["infrastructure_status"]["passed"] is True

    proof = adapter.classify_success(
        scenario,
        EvidencePack(
            manifest={"dispatcher": "recorded-real"},
            evidence_dir=str(evidence_dir),
        ),
    )
    assert proof.value == "validated"


def test_missing_required_evidence_is_undetermined(tmp_path: Path) -> None:
    adapter = VeasProjectAdapter()
    evidence_dir = tmp_path / "evidence"
    workdir = tmp_path / "workdir"
    _seed_generic_evidence(evidence_dir)
    _seed_project_specific_outputs(workdir, infra_ok=True)
    (workdir / "tool_transcript.json").unlink()

    run = ActorRun(
        dispatcher="recorded-real",
        mode=RunMode.LIVE,
        workdir=str(workdir),
    )
    scenario = _scenario()
    adapter.capture(scenario, run, evidence_dir)

    checks = adapter.project_universal_checks(scenario, evidence_dir)
    project_specific = checks["project_specific_evidence"]
    assert project_specific["passed"] is False
    assert project_specific["undetermined"] is True
    assert "tool_transcript.json" in project_specific["missing_files"]

    proof = adapter.classify_success(
        scenario,
        EvidencePack(
            manifest={"dispatcher": "recorded-real"},
            evidence_dir=str(evidence_dir),
        ),
    )
    assert proof.value == "authored"


def test_infrastructure_failures_stay_separate_from_behavior_gaps(
    tmp_path: Path,
) -> None:
    adapter = VeasProjectAdapter()
    evidence_dir = tmp_path / "evidence"
    workdir = tmp_path / "workdir"
    _seed_generic_evidence(evidence_dir)
    _seed_project_specific_outputs(workdir, infra_ok=False)
    (workdir / "tool_transcript.json").unlink()
    (workdir / "expected_behavior.json").unlink()

    run = ActorRun(
        dispatcher="real-agent",
        mode=RunMode.LIVE,
        workdir=str(workdir),
    )
    scenario = _scenario()
    adapter.capture(scenario, run, evidence_dir)

    checks = adapter.project_universal_checks(scenario, evidence_dir)
    infrastructure = checks["infrastructure_status"]
    project_specific = checks["project_specific_evidence"]
    generic = checks["generic_evidence"]

    assert infrastructure["passed"] is False
    assert infrastructure["severity"] == "error"
    assert infrastructure["infrastructure_failed"] is True
    assert project_specific["passed"] is True
    assert project_specific["severity"] == "info"
    assert generic["passed"] is True
    assert generic["severity"] == "info"


def test_runner_can_describe_evidence_contract(capsys) -> None:
    runner_main(["--mode", "real-agent", "--describe-evidence"])
    out = json.loads(capsys.readouterr().out)
    assert out["dispatcher"] == "real-agent"
    assert "git_diff.patch" in out["generic_required_files"]
    assert "tool_transcript.json" in out["project_specific_files"]
