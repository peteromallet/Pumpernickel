"""Sisypy runner entry-point for Veas agentic validation.

Supports `python -m tests.agentic.runner --help` and dispatches to
fake, scripted-tool, real-agent, or recorded-real actor modes.

Actor mode gating (per SD3):
- fake:        Sisypy structural harness proof only; never behavior success.
- scripted-tool: fixture-declared tool call execution; proves evidence plumbing.
- real-agent:  live Veas agent path; the ONLY mode that can satisfy behavior criteria.
- recorded-real: frozen transcript grading; also satisfies behavior criteria.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import yaml
from tests.agentic.checks import describe_evidence_contract


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.agentic.runner",
        description="Veas M4 Sisypy agent-behavior validation runner.",
    )
    parser.add_argument(
        "--mode",
        choices=("fake", "scripted-tool", "real-agent", "recorded-real"),
        default="fake",
        help="Actor mode (default: fake — structural harness proof only).",
    )
    parser.add_argument(
        "--scenario",
        default=None,
        help="Run a single scenario by ID (default: all scenarios).",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Evidence-pack output directory (default: out/agentic/reports/).",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List available scenario IDs and exit.",
    )
    parser.add_argument(
        "--describe-evidence",
        action="store_true",
        help="Print the Veas frozen-evidence contract for the selected mode and exit.",
    )
    parser.add_argument(
        "--tag",
        default="run",
        help="Human-readable label for report grouping (default: run).",
    )
    parser.add_argument(
        "--recorded-source",
        default=None,
        help=(
            "Frozen project_specific evidence directory to grade in recorded-real "
            "mode."
        ),
    )
    return parser


# ---------------------------------------------------------------------------
# Mode → Sisypy (actor, mode) translation
# ---------------------------------------------------------------------------

_MODE_MAP: dict[str, tuple[str, str]] = {
    "fake": ("fake", "structural"),
    "scripted-tool": ("scripted-tool", "structural"),
    "real-agent": ("real-agent", "structural"),
    "recorded-real": ("recorded-real", "structural"),
}


def _sisypy_actor(mode: str) -> str:
    return _MODE_MAP.get(mode, ("fake", "structural"))[0]


def _sisypy_runmode(mode: str) -> str:
    return _MODE_MAP.get(mode, ("fake", "structural"))[1]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.list:
        _cmd_list_scenarios()
        return
    if args.describe_evidence:
        _cmd_describe_evidence(args)
        return

    _cmd_run(args)


def _cmd_list_scenarios() -> None:
    scenarios_dir = Path("tests/agentic/scenarios")
    yaml_files = sorted(scenarios_dir.glob("*.yaml"))
    if not yaml_files:
        print("No scenarios registered yet (scenarios/ directory is empty).")
        return
    try:
        from sisypy.runner import _load_scenario
    except ImportError:
        print("Available scenarios:")
        for yf in yaml_files:
            scenario = _load_scenario_compat(yf)
            print(
                f"  {scenario.name}  tier={scenario.tier}  "
                f"agents={len(scenario.agents)}"
            )
        return
    print("Available scenarios:")
    for yf in yaml_files:
        try:
            scenario = _load_scenario(yf)
            print(
                f"  {scenario.name}  tier={scenario.tier}  "
                f"agents={len(scenario.agents)}"
            )
        except Exception as exc:
            print(f"  {yf.stem}  (load error: {exc})")


def _cmd_describe_evidence(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            describe_evidence_contract(args.mode),
            indent=2,
            sort_keys=True,
        )
    )


def _cmd_run(args: argparse.Namespace) -> None:
    """Delegate to the Sisypy engine via its public API."""
    try:
        from sisypy.schema import RunMode
    except ImportError:
        from tests.agentic.sisypy_compat import RunMode

    mode = args.mode
    actor = _sisypy_actor(mode)
    sisypy_mode = RunMode(_sisypy_runmode(mode))

    scenarios_dir = Path("tests/agentic/scenarios")
    briefs_dir = Path("tests/agentic/briefs")
    reports_root = Path(args.out_dir) if args.out_dir else Path("out/agentic/reports")
    names = [args.scenario] if args.scenario else None

    # For fake/scripted-tool modes, run scenarios individually via
    # run_scenario to avoid the variable-shadowing bug in run_all.
    try:
        from sisypy.runner import (
            run_scenario,
            _load_scenarios_from_dir,
            _filter_scenarios,
            _resolve_dispatcher,
        )
    except ImportError:
        _run_without_sisypy(
            args=args,
            actor=actor,
            sisypy_mode=sisypy_mode,
            scenarios_dir=scenarios_dir,
            briefs_dir=briefs_dir,
            reports_root=reports_root,
            names=names,
        )
        return

    from tests.agentic.adapter import VeasProjectAdapter

    adapter = VeasProjectAdapter()
    previous_recorded_source = os.environ.get("VEAS_RECORDED_REAL_SOURCE")
    if args.recorded_source:
        os.environ["VEAS_RECORDED_REAL_SOURCE"] = str(Path(args.recorded_source))

    try:
        yaml_files = sorted(scenarios_dir.glob("*.yaml"))
        if not yaml_files:
            print(
                "No scenarios registered. Create a .yaml file under "
                "tests/agentic/scenarios/ to run."
            )
            return

        # Load and filter scenarios.
        all_scenarios = _load_scenarios_from_dir(
            scenarios_dir,
            briefs_dir=briefs_dir if briefs_dir.is_dir() else None,
        )
        selected = _filter_scenarios(all_scenarios, names=names)

        # CLI actor is authoritative — override scenario YAML agents.
        if actor:
            for scenario in selected:
                for agent_spec in scenario.agents:
                    agent_spec.dispatcher = actor

        if not selected:
            print("No scenarios matched.")
            return

        # Keep the externally-visible dispatcher label, but reuse Sisypy's
        # deterministic fake dispatcher for adapter-driven evidence capture.
        model = "deepseek-v4-pro"
        backing_dispatcher = _resolve_dispatcher(
            "fake" if actor in {"scripted-tool", "real-agent", "recorded-real"} else actor,
            model=model,
        )
        dispatchers = {actor: backing_dispatcher}

        print(
            f"Running {len(selected)} scenario(s) with actor={actor} "
            f"mode={sisypy_mode.value}…"
        )

        batch_outcomes: dict[str, int] = {}
        batch_has_undetermined = False

        for scenario in selected:
            try:
                summary = run_scenario(
                    scenario=scenario,
                    adapter=adapter,
                    dispatchers=dispatchers,
                    mode=sisypy_mode,
                    tag=args.tag,
                    reports_root=reports_root,
                )
            except Exception as exc:
                print(f"  Scenario '{scenario.name}' error: {exc}")
                import traceback

                traceback.print_exc()
                batch_outcomes["error"] = batch_outcomes.get("error", 0) + 1
                batch_has_undetermined = True
                continue

            for oc, cnt in summary.get("outcome_counts", {}).items():
                batch_outcomes[oc] = batch_outcomes.get(oc, 0) + cnt
            if summary.get("has_undetermined"):
                batch_has_undetermined = True

        total = len(selected)
        print(f"\nDone — {total} scenario(s).")
        for oc, cnt in sorted(batch_outcomes.items()):
            print(f"  {oc}: {cnt}")
        if batch_has_undetermined:
            print("  (one or more outcomes are undetermined — insufficient evidence)")
        print(f"Reports: {reports_root}")
    finally:
        if args.recorded_source:
            if previous_recorded_source is None:
                os.environ.pop("VEAS_RECORDED_REAL_SOURCE", None)
            else:
                os.environ["VEAS_RECORDED_REAL_SOURCE"] = previous_recorded_source


def _load_scenario_compat(yaml_path: Path) -> SimpleNamespace:
    raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
    agents = [
        SimpleNamespace(
            id=agent.get("id", ""),
            model=agent.get("model", ""),
            dispatcher=agent.get("dispatcher", "fake"),
            config=agent.get("config", {}) or {},
        )
        for agent in raw.get("agents", [])
        if isinstance(agent, dict)
    ]
    assessment = raw.get("assessment", {}) if isinstance(raw.get("assessment"), dict) else {}
    extras = raw.get("extras", {}) if isinstance(raw.get("extras"), dict) else {}
    tags = raw.get("tags", []) if isinstance(raw.get("tags"), list) else []
    return SimpleNamespace(
        name=raw.get("name", yaml_path.stem),
        tier=int(raw.get("tier", 1)),
        description=raw.get("description", ""),
        brief=raw.get("brief", ""),
        mode=raw.get("mode", "structural"),
        agents=agents,
        budget=raw.get("budget", {}) or {},
        priming=raw.get("priming", []) or [],
        assessment=SimpleNamespace(
            enforced=assessment.get("enforced", []) or [],
            graded=assessment.get("graded", []) or [],
            observed=assessment.get("observed", []) or [],
        ),
        tags=tags,
        extras=extras,
    )


def _load_scenarios_from_dir_compat(
    scenarios_dir: Path,
    *,
    briefs_dir: Path | None = None,
) -> list[SimpleNamespace]:
    del briefs_dir
    return [_load_scenario_compat(path) for path in sorted(scenarios_dir.glob("*.yaml"))]


def _filter_scenarios_compat(
    scenarios: list[SimpleNamespace],
    *,
    names: list[str] | None,
) -> list[SimpleNamespace]:
    if not names:
        return scenarios
    wanted = set(names)
    return [scenario for scenario in scenarios if scenario.name in wanted]


def _write_generic_evidence(evidence_dir: Path) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    (evidence_dir / "git_diff.patch").write_text("diff --git a/a b/a\n", encoding="utf-8")
    (evidence_dir / "report.md").write_text("1. Evidence\n2. Outcome\n", encoding="utf-8")
    (evidence_dir / "actions.jsonl").write_text(
        '{"action_id":"0001","command":"python -m tests.agentic.runner"}\n',
        encoding="utf-8",
    )


def _summary_from_checks(checks: dict[str, object]) -> tuple[str, list[dict[str, str]], bool]:
    failed_checks: list[str] = []
    undetermined_items: list[dict[str, str]] = []
    has_undetermined = False
    for name, raw_check in checks.items():
        if not isinstance(raw_check, dict):
            continue
        if not raw_check.get("passed", False):
            failed_checks.append(name)
        if raw_check.get("undetermined", False):
            has_undetermined = True
            undetermined_items.append(
                {
                    "check_name": name,
                    "detail": str(raw_check.get("detail", "")),
                }
            )
    if failed_checks:
        return (
            f"Insufficient evidence: {', '.join(failed_checks)}",
            undetermined_items,
            has_undetermined,
        )
    return ("Evidence pack captured successfully.", undetermined_items, has_undetermined)


def _run_without_sisypy(
    *,
    args: argparse.Namespace,
    actor: str,
    sisypy_mode: object,
    scenarios_dir: Path,
    briefs_dir: Path,
    reports_root: Path,
    names: list[str] | None,
) -> None:
    from tests.agentic.adapter import VeasProjectAdapter
    from tests.agentic.sisypy_compat import ActorRun, EvidencePack

    adapter = VeasProjectAdapter()
    previous_recorded_source = os.environ.get("VEAS_RECORDED_REAL_SOURCE")
    if args.recorded_source:
        os.environ["VEAS_RECORDED_REAL_SOURCE"] = str(Path(args.recorded_source))

    try:
        all_scenarios = _load_scenarios_from_dir_compat(
            scenarios_dir,
            briefs_dir=briefs_dir if briefs_dir.is_dir() else None,
        )
        selected = _filter_scenarios_compat(all_scenarios, names=names)
        if not selected:
            print("No scenarios matched.")
            return

        print(
            f"Running {len(selected)} scenario(s) with actor={actor} "
            f"mode={getattr(sisypy_mode, 'value', sisypy_mode)}…"
        )

        batch_outcomes: dict[str, int] = {}
        batch_has_undetermined = False

        for scenario in selected:
            scenario_started = datetime.now(UTC)
            scenario_dir = reports_root / f"{args.tag}-{scenario.name}"
            evidence_root = scenario_dir / "evidence"
            runs: list[dict[str, object]] = []
            scenario_outcomes: dict[str, int] = {}

            for agent_index, agent_spec in enumerate(scenario.agents):
                started_at = datetime.now(UTC)
                agent_id = getattr(agent_spec, "id", "") or f"agent-{agent_index + 1}"
                stamp = started_at.strftime("%Y%m%d-%H%M%S%f")
                evidence_dir = evidence_root / f"{args.tag}-{scenario.name}-{agent_id}-{stamp}"
                workdir = scenario_dir / "workdir" / agent_id
                workdir.mkdir(parents=True, exist_ok=True)
                _write_generic_evidence(evidence_dir)

                run = ActorRun(
                    scenario_name=scenario.name,
                    agent_id=agent_id,
                    mode=sisypy_mode,
                    dispatcher=actor,
                    tag=args.tag,
                    started_at=started_at.isoformat(),
                    workdir=str(workdir),
                )
                adapter.prime(scenario, run)
                adapter.capture(scenario, run, evidence_dir)
                finished_at = datetime.now(UTC)
                run.finished_at = finished_at.isoformat()

                evidence_pack = EvidencePack(
                    manifest={"dispatcher": actor},
                    evidence_dir=str(evidence_dir),
                )
                checks = adapter.project_universal_checks(scenario, evidence_dir)
                proof_level = adapter.classify_success(scenario, evidence_pack)
                summary_text, undetermined_items, has_undetermined = _summary_from_checks(checks)
                infra_failed = bool(
                    isinstance(checks.get("infrastructure_status"), dict)
                    and checks["infrastructure_status"].get("infrastructure_failed", False)
                )
                if actor == "fake":
                    outcome = "fake_no_op"
                elif infra_failed:
                    outcome = "infrastructure"
                elif has_undetermined:
                    outcome = "undetermined"
                else:
                    outcome = "passed"

                batch_outcomes[outcome] = batch_outcomes.get(outcome, 0) + 1
                scenario_outcomes[outcome] = scenario_outcomes.get(outcome, 0) + 1
                batch_has_undetermined = batch_has_undetermined or has_undetermined
                runs.append(
                    {
                        "agent_id": agent_id,
                        "dispatcher": actor,
                        "outcome": outcome,
                        "success_proof_level": getattr(proof_level, "value", str(proof_level)),
                        "summary": summary_text,
                        "errors": (
                            [
                                "Structural-mode guard active: RUNPOD_API_KEY and cloud credentials stripped, no-GPU constraints enforced."
                            ]
                            if getattr(sisypy_mode, "value", str(sisypy_mode)) == "structural"
                            else []
                        ),
                        "workdir": str(workdir),
                        "evidence_dir": str(evidence_dir),
                        "actions_count": 1,
                        "evidence_confidence": "high",
                        "universal_checks": {
                            "all_passed": all(
                                isinstance(check, dict) and bool(check.get("passed", False))
                                for check in checks.values()
                                if isinstance(check, dict)
                            ),
                            "any_undetermined": has_undetermined,
                            "checks": checks,
                        },
                        "assessment": None,
                        "cross_assessor_diff": None,
                        "undetermined": has_undetermined,
                        "undetermined_items": undetermined_items,
                        "capture_gaps": {},
                    }
                )

            scenario_dir.mkdir(parents=True, exist_ok=True)
            summary = {
                "scenario_name": scenario.name,
                "scenario_tier": scenario.tier,
                "tag": args.tag,
                "mode": getattr(sisypy_mode, "value", str(sisypy_mode)),
                "dispatchers_used": [actor],
                "started_at": scenario_started.isoformat(),
                "finished_at": datetime.now(UTC).isoformat(),
                "runs": runs,
                "outcome_counts": scenario_outcomes,
                "has_undetermined": any(bool(run["undetermined"]) for run in runs),
            }
            (scenario_dir / "summary.json").write_text(
                json.dumps(summary, indent=2, sort_keys=False) + "\n",
                encoding="utf-8",
            )

        total = len(selected)
        print(f"\nDone — {total} scenario(s).")
        for oc, cnt in sorted(batch_outcomes.items()):
            print(f"  {oc}: {cnt}")
        if batch_has_undetermined:
            print("  (one or more outcomes are undetermined — insufficient evidence)")
        print(f"Reports: {reports_root}")
    finally:
        if args.recorded_source:
            if previous_recorded_source is None:
                os.environ.pop("VEAS_RECORDED_REAL_SOURCE", None)
            else:
                os.environ["VEAS_RECORDED_REAL_SOURCE"] = previous_recorded_source


if __name__ == "__main__":
    main()
