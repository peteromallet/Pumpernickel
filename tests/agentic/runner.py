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
from pathlib import Path

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
        print("Cannot load scenarios: sisypy package not available.")
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
    from sisypy.schema import RunMode

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
        print("Sisypy package not available. Cannot execute scenarios.")
        print("Install with: pip install -e '.[agentic]'")
        sys.exit(1)

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


if __name__ == "__main__":
    main()
