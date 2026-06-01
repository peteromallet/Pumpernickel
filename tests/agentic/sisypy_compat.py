"""Diagnostic-only compatibility shim for external Sisypy unavailability.

This module provides ABSOLUTELY MINIMAL stubs so that `tests/agentic/runner`
and `tests/agentic/adapter` can import without crashing when the external
`sisypy` package from `github.com/peteromallet/sisypy` is not installed.

***** CRITICAL GATING *****
This shim CANNOT and MUST NOT satisfy behavior scenario pass criteria.
Any run that uses this shim must classify all behavior scenarios as
`undetermined` or `not executed`.  The shim only exists to support:
  - `python -m tests.agentic.runner --help`
  - Structural smoke tests (fake mode)
  - Diagnostic output confirming Sisypy is unavailable

To install the real Sisypy package:
  pip install -e ".[agentic]"
  # or equivalently:
  pip install git+https://github.com/peteromallet/sisypy.git@650f80307d7f1d14005b954e254e9be3804f8002
"""

from __future__ import annotations

import abc
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Minimal type stubs matching the external Sisypy schema enough to avoid
# ImportError.  These are intentionally minimal — they do NOT implement
# any real Sisypy behavior.
# ---------------------------------------------------------------------------


class SuccessProofLevel(str, Enum):
    AUTHORED = "authored"
    COMPILED = "compiled"
    VALIDATED = "validated"
    RUNTIME_ATTEMPTED = "runtime_attempted"
    RUNTIME_PROVEN = "runtime_proven"
    ARTIFACT_PROVEN = "artifact_proven"
    QUALITY_ASSESSED = "quality_assessed"


class RunMode(str, Enum):
    STRUCTURAL = "structural"
    LIVE = "live"


@dataclass
class Scenario:
    name: str = ""
    tier: int = 1
    description: str = ""
    brief: str = ""
    mode: RunMode = RunMode.STRUCTURAL
    agents: list["AgentSpec"] = field(default_factory=list)
    budget: dict[str, Any] = field(default_factory=dict)
    priming: list[str] = field(default_factory=list)
    assessment: "Assessment | None" = None
    tags: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ActorRun:
    id: str = ""
    scenario_name: str = ""
    agent_id: str = ""
    mode: RunMode = RunMode.STRUCTURAL
    dispatcher: str = "fake"
    tag: str = ""
    started_at: str = ""
    finished_at: str = ""
    outcome: str = ""
    success_proof_level: SuccessProofLevel = SuccessProofLevel.AUTHORED
    summary: str = ""
    errors: list[str] = field(default_factory=list)
    workdir: str | None = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvidencePack:
    manifest: dict[str, Any] = field(default_factory=dict)
    evidence_dir: str = ""
    files: dict[str, str] = field(default_factory=dict)
    capture_notes: list[str] = field(default_factory=list)
    capture_gaps: dict[str, Any] = field(default_factory=dict)


@dataclass
class Assessment:
    scenario_id: str = ""
    passed: bool = False
    checks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ScenarioOutcome:
    scenario_id: str = ""
    status: str = "undetermined"
    assessment: Assessment | None = None


@dataclass
class AgentSpec:
    name: str = ""
    mode: str = ""


# ---------------------------------------------------------------------------
# ABC stub — matches the shape of AgenticProjectAdapter from sisypy.adapters
# ---------------------------------------------------------------------------


class AgenticProjectAdapter(abc.ABC):
    """Diagnostic skeleton.  Does NOT implement any Sisypy behavior."""

    @abc.abstractmethod
    def build_env(self, scenario: Scenario, run: ActorRun) -> dict[str, str]: ...

    @abc.abstractmethod
    def prime(self, scenario: Scenario, run: ActorRun) -> None: ...

    @abc.abstractmethod
    def capture(self, scenario: Scenario, run: ActorRun, evidence_dir: Path) -> None: ...

    @abc.abstractmethod
    def project_universal_checks(
        self, scenario: Scenario, evidence_dir: Path
    ) -> dict[str, Any]: ...

    @abc.abstractmethod
    def canonical_bypass_patterns(self, scenario: Scenario) -> list[str]: ...

    @abc.abstractmethod
    def classify_success(
        self, scenario: Scenario, evidence_pack: EvidencePack
    ) -> SuccessProofLevel: ...

    @abc.abstractmethod
    def live_prerequisites(self, scenario: Scenario) -> dict[str, bool]: ...

    @abc.abstractmethod
    def command_policy(
        self, scenario: Scenario, run: ActorRun
    ) -> dict[str, Any]: ...


class FakeProjectAdapter(AgenticProjectAdapter):
    """Stub fake adapter — always returns empty sets."""

    def __init__(self, name: str = "fake", repo_root: Path | None = None) -> None:
        self.name = name
        self.repo_root = Path(repo_root) if repo_root else Path.cwd()

    def build_env(self, scenario: Scenario, run: ActorRun) -> dict[str, str]:
        _ = scenario, run
        return {}

    def prime(self, scenario: Scenario, run: ActorRun) -> None:
        _ = scenario, run
        return

    def capture(self, scenario: Scenario, run: ActorRun, evidence_dir: Path) -> None:
        _ = scenario, run
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "project_specific").mkdir(parents=True, exist_ok=True)

    def project_universal_checks(
        self, scenario: Scenario, evidence_dir: Path
    ) -> dict[str, Any]:
        _ = scenario, evidence_dir
        return {}

    def canonical_bypass_patterns(self, scenario: Scenario) -> list[str]:
        _ = scenario
        return []

    def classify_success(
        self, scenario: Scenario, evidence_pack: EvidencePack
    ) -> SuccessProofLevel:
        _ = scenario, evidence_pack
        return SuccessProofLevel.AUTHORED

    def live_prerequisites(self, scenario: Scenario) -> dict[str, bool]:
        _ = scenario
        return {"RUNPOD_API_KEY": True, "budget": True, "timeout": True}

    def command_policy(
        self, scenario: Scenario, run: ActorRun
    ) -> dict[str, Any]:
        _ = scenario, run
        return {
            "allow_patterns": [r".*"],
            "deny_patterns": [],
            "enforce": False,
        }


# ---------------------------------------------------------------------------
# Diagnostic emission
# ---------------------------------------------------------------------------

_SISYPY_UNAVAILABLE_MSG = (
    "*** EXTERNAL SISYPY PACKAGE NOT INSTALLED ***\n"
    "The sisypy package from github.com/peteromallet/sisypy is not available.\n"
    "Using diagnostic-only compatibility shim (tests/agentic/sisypy_compat.py).\n"
    "\n"
    "This shim CANNOT satisfy behavior scenario pass criteria.\n"
    "All behavior scenarios will be classified as 'undetermined'.\n"
    "\n"
    "To install the real Sisypy package:\n"
    "  pip install -e \".[agentic]\"\n"
    "  # or:\n"
    "  pip install git+https://github.com/peteromallet/sisypy.git@650f80307d7f1d14005b954e254e9be3804f8002\n"
)


def _emit_diagnostic() -> None:
    """Emit the Sisypy-unavailable diagnostic to stderr once."""
    print(_SISYPY_UNAVAILABLE_MSG, file=sys.stderr)


# Emit on first import so it's visible during any runner invocation.
_emit_diagnostic()
