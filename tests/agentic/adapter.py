"""Veas project adapter for Sisypy agentic validation.

Implements the Sisypy AgenticProjectAdapter ABC to wire the Veas codebase
(tool schemas, nav/search tools, hot-context, registry) into the Sisypy
runner harness.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tests.agentic.checks import (
    BEHAVIOR_ELIGIBLE_DISPATCHERS,
    build_project_evidence_checks,
    capture_project_evidence,
    describe_evidence_contract,
)

# External Sisypy dependency — pinned via pyproject.toml optional-dep.
# Falls back to the diagnostic-only compat shim when unavailable.
try:
    from sisypy.adapters import (  # type: ignore[import-untyped]
        AgenticProjectAdapter,
        ActorRun,
        EvidencePack,
        FakeProjectAdapter,
        Scenario,
        SuccessProofLevel,
    )
except ImportError:
    from tests.agentic.sisypy_compat import (  # type: ignore[assignment]
        AgenticProjectAdapter,
        ActorRun,
        EvidencePack,
        FakeProjectAdapter,
        Scenario,
        SuccessProofLevel,
    )


def _dispatch_evidence(
    project_specific_dir: Path,
    run: Any,
    scenario: Any,
) -> None:
    """Fill project-specific evidence gaps without overwriting existing files.

    Saves any pre-existing infrastructure.json and restores it after
    dispatcher writes so seeded infrastructure state survives.
    """
    infra_path = project_specific_dir / "infrastructure.json"
    saved_infra: bytes | None = None
    if infra_path.exists():
        saved_infra = infra_path.read_bytes()

    if run.dispatcher == "scripted-tool":
        from tests.agentic.scripted_tool import write_scripted_tool_evidence

        if not (project_specific_dir / "tool_transcript.json").exists():
            case_ids = None
            extras = getattr(scenario, "extras", {}) or {}
            raw_case_ids = extras.get("scripted_tool_case_ids")
            if isinstance(raw_case_ids, list):
                case_ids = [
                    str(item) for item in raw_case_ids if isinstance(item, str)
                ]
            write_scripted_tool_evidence(
                output_dir=project_specific_dir,
                case_ids=case_ids or None,
            )
    elif run.dispatcher == "real-agent":
        from tests.agentic.real_agent import write_real_agent_evidence

        if not (project_specific_dir / "tool_transcript.json").exists():
            write_real_agent_evidence(output_dir=project_specific_dir)
    elif run.dispatcher == "recorded-real":
        from tests.agentic.real_agent import (
            recorded_real_source_from_env,
            write_recorded_real_evidence,
        )

        source_dir = recorded_real_source_from_env()
        if source_dir is not None:
            write_recorded_real_evidence(
                output_dir=project_specific_dir,
                source_dir=source_dir,
            )
        # When no source is available, do not write infrastructure.json here.
        # capture_project_evidence will copy any seeded files from workdir,
        # and the checks will surface missing infrastructure as undetermined.

    # Restore seeded infrastructure.json if it was overwritten.
    if saved_infra is not None and infra_path.exists():
        current = infra_path.read_bytes()
        if current != saved_infra:
            infra_path.write_bytes(saved_infra)


class VeasProjectAdapter(AgenticProjectAdapter):
    """Sisypy project adapter for the Veas mediator-bot codebase.

    Responsibilities (filled in by later tasks):
    - Fixture staging: seed the M4 fake pool with scenario messages/topics.
    - Allowed commands: expose M2/M3 nav/search tools.
    - Forbidden commands: block mutation tools during validation runs.
    - Repo mutation capture: freeze tool schemas, registry, hot-context.
    - Project-specific checks: graph/runtime assertions on tool transcript.
    - Live-mode evidence: wire real-agent runs through evals.execution.
    """

    root: Path

    def __init__(self, root: Path | None = None) -> None:
        self.root = (
            Path(root) if root is not None else Path(__file__).resolve().parents[2]
        )
        self.name = "veas"
        self.repo_root = self.root
        self._fallback = FakeProjectAdapter(name="veas", repo_root=self.root)

    # --- Local Veas policy helpers ---

    def allowed_commands(self) -> set[str]:
        return {
            r"^python(?:3)?\b",
            r"^pytest\b",
            r"^uv\b",
            r"^rg\b",
            r"^sed\b",
            r"^cat\b",
            r"^ls\b",
            r"^git\s+(?:status|diff|rev-parse|show)\b",
        }

    def forbidden_commands(self) -> set[str]:
        return {
            r"^git\s+(?:push|reset|rebase|checkout\b.*--)\b",
            r"^rm\s+-rf\b",
            r"^curl\b",
            r"^wget\b",
        }

    def stage_fixtures(self, scenario_id: str) -> dict[str, object]:
        return {}

    def capture_repo_snapshot(self) -> dict[str, object]:
        return {
            "repo_root": str(self.root),
            "pyproject_exists": (self.root / "pyproject.toml").exists(),
            "agentic_dir_exists": (self.root / "tests" / "agentic").exists(),
        }

    # --- Sisypy ABC implementation ---

    def build_env(self, scenario: Scenario, run: ActorRun) -> dict[str, str]:
        env = self._fallback.build_env(scenario, run)
        env.update(
            {
                "VEAS_REPO_ROOT": str(self.root),
                "VEAS_AGENTIC_SCENARIO": scenario.name,
                "VEAS_AGENTIC_DISPATCHER": run.dispatcher,
            }
        )
        pythonpath = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{self.root}{os.pathsep}{pythonpath}" if pythonpath else str(self.root)
        )
        return env

    def prime(self, scenario: Scenario, run: ActorRun) -> None:
        self._fallback.prime(scenario, run)
        scenario_id = scenario.name or run.scenario_name or "unassigned"
        self.stage_fixtures(scenario_id)
        if run.workdir:
            Path(run.workdir).mkdir(parents=True, exist_ok=True)

    def capture(self, scenario: Scenario, run: ActorRun, evidence_dir: Path) -> None:
        self._fallback.capture(scenario, run, evidence_dir)
        project_specific_dir = evidence_dir / "project_specific"
        project_specific_dir.mkdir(parents=True, exist_ok=True)

        # Pre-seed infrastructure.json from workdir so dispatcher writes
        # never mask deliberately-seeded infrastructure state.
        workdir = Path(run.workdir) if run.workdir else None
        infra_path = project_specific_dir / "infrastructure.json"
        if not infra_path.exists() and workdir is not None:
            workdir_infra = workdir / "infrastructure.json"
            if workdir_infra.exists():
                import shutil

                shutil.copy2(workdir_infra, infra_path)

        saved_infra: bytes | None = None
        if infra_path.exists():
            saved_infra = infra_path.read_bytes()

        # Dispatcher-specific writes first so capture_project_evidence
        # can see the generated files.
        _dispatch_evidence(project_specific_dir, run, scenario)

        # Restore saved infrastructure if it was overwritten.
        if saved_infra is not None and infra_path.exists():
            if infra_path.read_bytes() != saved_infra:
                infra_path.write_bytes(saved_infra)

        evidence_manifest = capture_project_evidence(
            repo_root=self.root,
            scenario=scenario,
            workdir=Path(run.workdir) if run.workdir else None,
            evidence_dir=evidence_dir,
        )
        manifest = {
            "scenario": scenario.name,
            "dispatcher": run.dispatcher,
            "mode": getattr(run.mode, "value", str(run.mode)),
            "repo_snapshot": self.capture_repo_snapshot(),
            "command_policy": self.command_policy(scenario, run),
            "live_prerequisites": self.live_prerequisites(scenario),
            "evidence_contract": describe_evidence_contract(run.dispatcher),
        }
        (project_specific_dir / "veas_adapter_manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        evidence_manifest["dispatcher"] = run.dispatcher
        evidence_manifest["scenario_extras"] = getattr(scenario, "extras", {}) or {}
        (project_specific_dir / "veas_evidence_manifest.json").write_text(
            json.dumps(evidence_manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def project_universal_checks(
        self, scenario: Scenario, evidence_dir: Path
    ) -> dict[str, Any]:
        project_specific_dir = evidence_dir / "project_specific"
        manifest_path = project_specific_dir / "veas_adapter_manifest.json"
        evidence_manifest_path = project_specific_dir / "veas_evidence_manifest.json"
        evidence_manifest: dict[str, Any] = {}
        if evidence_manifest_path.exists():
            try:
                evidence_manifest = json.loads(
                    evidence_manifest_path.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError:
                evidence_manifest = {}
        dispatcher = str(evidence_manifest.get("dispatcher", ""))
        checks = {
            "adapter_manifest_present": {
                "passed": manifest_path.exists(),
                "severity": "error",
                "detail": str(manifest_path),
            },
            "behavior_mode_gating": {
                "passed": True,
                "severity": "info",
                "detail": (
                    "Runner-level gating keeps fake/scripted-tool structural-only; "
                    "real-agent and recorded-real are the only behavior-eligible modes."
                ),
            },
        }
        checks.update(
            build_project_evidence_checks(
                scenario=scenario,
                evidence_dir=evidence_dir,
                dispatcher=dispatcher,
                manifest=evidence_manifest,
            )
        )
        if not evidence_manifest_path.exists():
            checks["evidence_manifest_present"] = {
                "passed": False,
                "undetermined": True,
                "severity": "undetermined",
                "detail": str(evidence_manifest_path),
            }
        else:
            checks["evidence_manifest_present"] = {
                "passed": True,
                "severity": "ok",
                "detail": str(evidence_manifest_path),
            }
        return checks

    def canonical_bypass_patterns(self, scenario: Scenario) -> list[str]:
        _ = scenario
        return [
            r"ignore\s+the\s+rubric",
            r"pretend\s+(?:the\s+)?tests?\s+passed",
            r"skip\s+tool\s+usage",
            r"claim\s+success\s+without\s+evidence",
        ]

    def classify_success(
        self, scenario: Scenario, evidence_pack: EvidencePack
    ) -> SuccessProofLevel:
        _ = scenario
        dispatcher = str(evidence_pack.manifest.get("dispatcher", ""))
        evidence_dir = Path(evidence_pack.evidence_dir)
        checks = self.project_universal_checks(scenario, evidence_dir)
        infra_status = checks.get("infrastructure_status", {})
        has_undetermined = any(
            isinstance(result, dict) and bool(result.get("undetermined", False))
            for result in checks.values()
        )
        if infra_status.get("infrastructure_failed"):
            return SuccessProofLevel.AUTHORED
        if dispatcher in BEHAVIOR_ELIGIBLE_DISPATCHERS and not has_undetermined:
            return SuccessProofLevel.VALIDATED
        return SuccessProofLevel.AUTHORED

    def live_prerequisites(self, scenario: Scenario) -> dict[str, bool]:
        _ = scenario
        return {
            "OPENAI_API_KEY": bool(os.environ.get("OPENAI_API_KEY")),
            "VEAS_DATABASE_URL": bool(
                os.environ.get("DATABASE_URL") or os.environ.get("SUPABASE_DB_URL")
            ),
            "budget": True,
            "timeout": True,
        }

    def command_policy(self, scenario: Scenario, run: ActorRun) -> dict[str, Any]:
        _ = scenario
        dispatcher = run.dispatcher
        structural_only = dispatcher in {"fake", "scripted-tool"}
        return {
            "allow_patterns": sorted(self.allowed_commands()),
            "deny_patterns": sorted(self.forbidden_commands()),
            "enforce": structural_only,
        }
