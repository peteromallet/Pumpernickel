"""Evidence contract helpers for Veas Sisypy runs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

FROZEN_REPO_FILES: tuple[str, ...] = (
    "tool_schemas.py",
    "app/services/tools/read_tools.py",
    "app/services/tools/registry.py",
    "app/services/hot_context.py",
)

GENERIC_REQUIRED_FILES: tuple[str, ...] = (
    "git_diff.patch",
    "report.md",
)

GENERIC_REQUIRED_ONE_OF: dict[str, tuple[str, ...]] = {
    "command_trace": ("actions.jsonl", "command_log.jsonl"),
}

PROJECT_SPECIFIC_FILES: tuple[str, ...] = (
    "tool_transcript.json",
    "hot_context.md",
    "messages_seed.json",
    "expected_behavior.json",
    "final_answer.md",
    "assertions.json",
    "infrastructure.json",
)

BEHAVIOR_ELIGIBLE_DISPATCHERS: frozenset[str] = frozenset(
    {"real-agent", "recorded-real"}
)

INFRA_FAILURE_STATUSES: frozenset[str] = frozenset(
    {
        "blocked",
        "blocked_prerequisite",
        "error",
        "failed",
        "infra_bug",
        "infrastructure",
        "timeout",
        "unavailable",
    }
)


def describe_evidence_contract(dispatcher: str) -> dict[str, Any]:
    return {
        "dispatcher": dispatcher,
        "behavior_eligible": dispatcher in BEHAVIOR_ELIGIBLE_DISPATCHERS,
        "generic_required_files": list(GENERIC_REQUIRED_FILES),
        "generic_required_one_of": {
            key: list(paths) for key, paths in GENERIC_REQUIRED_ONE_OF.items()
        },
        "frozen_repo_files": list(FROZEN_REPO_FILES),
        "project_specific_files": list(PROJECT_SPECIFIC_FILES),
    }


def declared_fixture_files(scenario: Any) -> list[str]:
    extras = getattr(scenario, "extras", {}) or {}
    raw = extras.get("fixture_files") or []
    result: list[str] = []
    for item in raw:
        if isinstance(item, str) and item:
            result.append(item)
    return result


def required_project_specific_files(scenario: Any) -> list[str]:
    extras = getattr(scenario, "extras", {}) or {}
    override = extras.get("required_project_specific_files")
    if isinstance(override, list) and override:
        result: list[str] = []
        for item in override:
            if isinstance(item, str) and item:
                result.append(item)
        if result:
            return result
    return list(PROJECT_SPECIFIC_FILES)


def capture_project_evidence(
    *,
    repo_root: Path,
    scenario: Any,
    workdir: Path | None,
    evidence_dir: Path,
) -> dict[str, Any]:
    project_specific_dir = evidence_dir / "project_specific"
    frozen_repo_dir = project_specific_dir / "frozen_repo"
    fixtures_dir = project_specific_dir / "fixtures"
    project_specific_dir.mkdir(parents=True, exist_ok=True)
    frozen_repo_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir.mkdir(parents=True, exist_ok=True)

    manifest: dict[str, Any] = {
        "declared_fixture_files": declared_fixture_files(scenario),
        "required_project_specific_files": required_project_specific_files(scenario),
        "frozen_repo_files": {},
        "fixture_files": {},
        "project_specific_files": {},
    }

    for rel_path in FROZEN_REPO_FILES:
        src = repo_root / rel_path
        dst = frozen_repo_dir / rel_path
        manifest["frozen_repo_files"][rel_path] = _copy_if_present(src, dst)

    fixtures_tree = sorted(
        str(path.relative_to(repo_root))
        for path in (repo_root / "tests" / "agentic" / "fixtures").rglob("*")
        if path.is_file()
    )
    (project_specific_dir / "fixtures_tree.json").write_text(
        json.dumps(fixtures_tree, indent=2) + "\n",
        encoding="utf-8",
    )

    for rel_path in manifest["declared_fixture_files"]:
        src = repo_root / rel_path
        dst = fixtures_dir / rel_path
        manifest["fixture_files"][rel_path] = _copy_if_present(src, dst)

    for filename in manifest["required_project_specific_files"]:
        candidate = _first_existing_file(
            [
                project_specific_dir / filename,
                evidence_dir / filename,
                *([workdir / filename] if workdir is not None else []),
            ]
        )
        if candidate is None:
            manifest["project_specific_files"][filename] = {"present": False}
            continue

        target = project_specific_dir / filename
        if candidate.resolve() != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(candidate, target)
        manifest["project_specific_files"][filename] = {
            "present": True,
            "source": str(candidate),
            "target": str(target.relative_to(evidence_dir)),
        }

    return manifest


def build_project_evidence_checks(
    *,
    scenario: Any,
    evidence_dir: Path,
    dispatcher: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    infra_status = _read_infrastructure_status(
        evidence_dir / "project_specific" / "infrastructure.json"
    )
    infra_failed = infra_status.get("infrastructure_failed", False)

    checks["frozen_repo_capture"] = _presence_check(
        label="Frozen repo files",
        expected=list(FROZEN_REPO_FILES),
        actual_map=manifest.get("frozen_repo_files", {}),
        allow_undetermined=True,
        skip_reason="",
        infra_failed=False,
    )
    checks["fixture_capture"] = _presence_check(
        label="Declared fixtures",
        expected=declared_fixture_files(scenario),
        actual_map=manifest.get("fixture_files", {}),
        allow_undetermined=True,
        skip_reason="",
        infra_failed=False,
    )

    checks["generic_evidence"] = _generic_evidence_check(
        evidence_dir=evidence_dir,
        allow_undetermined=not infra_failed,
        infra_failed=infra_failed,
    )
    checks["project_specific_evidence"] = _presence_check(
        label="Project-specific evidence",
        expected=required_project_specific_files(scenario),
        actual_map=manifest.get("project_specific_files", {}),
        allow_undetermined=not infra_failed,
        skip_reason="skipped because infrastructure.json recorded an infrastructure failure.",
        infra_failed=infra_failed,
    )
    checks["infrastructure_status"] = infra_status
    return checks


def evidence_contract_satisfied(evidence_dir: Path, dispatcher: str) -> bool:
    manifest = _read_json(
        evidence_dir / "project_specific" / "veas_evidence_manifest.json"
    )
    if not manifest:
        return False
    checks = build_project_evidence_checks(
        scenario=type(
            "ScenarioLike", (), {"extras": manifest.get("scenario_extras", {})}
        )(),
        evidence_dir=evidence_dir,
        dispatcher=dispatcher,
        manifest=manifest,
    )
    return all(
        bool(result.get("passed", False))
        for key, result in checks.items()
        if key != "infrastructure_status"
    ) and bool(checks["infrastructure_status"].get("passed", False))


def _copy_if_present(src: Path, dst: Path) -> dict[str, Any]:
    if not src.is_file():
        return {"present": False, "source": str(src)}
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    return {
        "present": True,
        "source": str(src),
        "target": str(dst),
    }


def _first_existing_file(candidates: list[Path]) -> Path | None:
    for path in candidates:
        if path.is_file():
            return path
    return None


def _generic_evidence_check(
    *,
    evidence_dir: Path,
    allow_undetermined: bool,
    infra_failed: bool,
) -> dict[str, Any]:
    if infra_failed:
        return {
            "passed": True,
            "severity": "info",
            "detail": "Generic evidence requirement skipped because infrastructure.json recorded an infrastructure failure.",
            "missing_files": [],
            "missing_groups": {},
        }

    missing_files = [
        name for name in GENERIC_REQUIRED_FILES if not (evidence_dir / name).is_file()
    ]
    missing_groups = {
        key: list(paths)
        for key, paths in GENERIC_REQUIRED_ONE_OF.items()
        if not any((evidence_dir / path).is_file() for path in paths)
    }
    if missing_files or missing_groups:
        return {
            "passed": False,
            "undetermined": allow_undetermined,
            "severity": "undetermined" if allow_undetermined else "error",
            "detail": (
                "Missing generic frozen evidence files required for Sisypy assessment."
            ),
            "missing_files": missing_files,
            "missing_groups": missing_groups,
        }
    return {
        "passed": True,
        "severity": "ok",
        "detail": "Generic frozen evidence files are present.",
        "missing_files": [],
        "missing_groups": {},
    }


def _presence_check(
    *,
    label: str,
    expected: list[str],
    actual_map: dict[str, Any],
    allow_undetermined: bool,
    skip_reason: str,
    infra_failed: bool,
) -> dict[str, Any]:
    if infra_failed:
        return {
            "passed": True,
            "severity": "info",
            "detail": f"{label} requirement {skip_reason}",
            "missing_files": [],
        }
    if not expected:
        return {
            "passed": True,
            "severity": "info",
            "detail": f"{label}: no scenario-specific files declared.",
            "missing_files": [],
        }
    missing = [
        item
        for item in expected
        if not bool((actual_map.get(item) or {}).get("present"))
    ]
    if missing:
        return {
            "passed": False,
            "undetermined": allow_undetermined,
            "severity": "undetermined" if allow_undetermined else "error",
            "detail": f"{label} missing from frozen evidence pack.",
            "missing_files": missing,
        }
    return {
        "passed": True,
        "severity": "ok",
        "detail": f"{label} captured successfully.",
        "missing_files": [],
    }


def _read_infrastructure_status(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {
            "passed": False,
            "undetermined": True,
            "severity": "undetermined",
            "detail": "infrastructure.json is missing from project-specific evidence.",
            "infrastructure_failed": False,
            "status": "missing",
        }
    data = _read_json(path)
    if data is None:
        return {
            "passed": False,
            "undetermined": True,
            "severity": "undetermined",
            "detail": "infrastructure.json is not valid JSON.",
            "infrastructure_failed": False,
            "status": "invalid",
        }

    status = str(data.get("status", "")).strip().lower()
    failure_class = str(data.get("failure_class", "")).strip().lower()
    ok = data.get("ok")
    infra_failed = bool(
        ok is False
        or status in INFRA_FAILURE_STATUSES
        or failure_class in INFRA_FAILURE_STATUSES
    )
    if infra_failed:
        reason = data.get("reason") or data.get("detail") or status or failure_class
        return {
            "passed": False,
            "severity": "error",
            "detail": f"Infrastructure failure recorded in infrastructure.json: {reason}.",
            "infrastructure_failed": True,
            "status": status or failure_class or "failed",
        }
    return {
        "passed": True,
        "severity": "ok",
        "detail": "infrastructure.json indicates the run environment was healthy enough for behavior grading.",
        "infrastructure_failed": False,
        "status": status or ("ok" if ok is True else "unknown"),
    }


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _ps_dir(evidence_dir: Path) -> Path:
    return evidence_dir / "project_specific"


def _undetermined(detail: str, *, missing_path: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {
        "passed": False,
        "undetermined": True,
        "severity": "undetermined",
        "detail": detail,
    }
    if missing_path:
        result["missing_path"] = missing_path
    return result


def _ok(detail: str) -> dict[str, Any]:
    return {"passed": True, "severity": "ok", "detail": detail}


def _fail(detail: str) -> dict[str, Any]:
    return {"passed": False, "severity": "error", "detail": detail}


def _load_tool_transcript(evidence_dir: Path) -> dict[str, Any] | None:
    transcript_path = _ps_dir(evidence_dir) / "tool_transcript.json"
    return _read_json(transcript_path)


def _collect_tool_names(transcript: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    for case in transcript.get("cases", []):
        for step in case.get("step_results", []):
            seen.add(step.get("tool_name", ""))
    for call in transcript.get("tool_calls", []):
        tn = call.get("tool_name", "")
        if tn:
            seen.add(tn)
    return seen


def _collect_retrieved_message_ids(transcript: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for case in transcript.get("cases", []):
        for step in case.get("step_results", []):
            for mid in step.get("retrieved_message_ids", []):
                ids.add(str(mid))
    return ids


def _collect_tool_call_sequence(transcript: dict[str, Any]) -> list[dict[str, Any]]:
    seq: list[dict[str, Any]] = []
    for case in transcript.get("cases", []):
        for step in case.get("step_results", []):
            seq.append(
                {
                    "tool_name": step.get("tool_name", ""),
                    "args": step.get("args", {}),
                    "result": step.get("result", {}),
                    "retrieved_message_ids": step.get("retrieved_message_ids", []),
                }
            )
    if not seq:
        for call in transcript.get("tool_calls", []):
            seq.append(
                {
                    "tool_name": call.get("tool_name", ""),
                    "args": call.get("args", {}),
                    "result": call.get("result", {}),
                    "retrieved_message_ids": call.get("retrieved_message_ids", []),
                }
            )
    return seq


# ---------------------------------------------------------------------------
# Evidence helper functions for rubric checks
# ---------------------------------------------------------------------------


def required_tool_used(
    evidence_dir: Path,
    required_tools: frozenset[str] | set[str],
) -> dict[str, Any]:
    """Check that every required tool appears in the tool transcript.

    Inspects the frozen ``tool_transcript.json``, not actor narrative.
    Returns ``undetermined`` (never ``passed``) when the transcript is
    missing.
    """
    transcript = _load_tool_transcript(evidence_dir)
    if transcript is None:
        return _undetermined(
            "tool_transcript.json is missing; cannot verify required tools.",
            missing_path=str(_ps_dir(evidence_dir) / "tool_transcript.json"),
        )
    required = set(required_tools)
    if not required:
        return _ok("No required tools declared — check vacuously passes.")
    seen = _collect_tool_names(transcript)
    missing = sorted(required - seen)
    if missing:
        return _fail(
            f"Required tool(s) not used: {', '.join(missing)}. "
            f"Tools observed: {sorted(seen) or '(none)'}."
        )
    return _ok(f"All required tools used: {sorted(required)}.")


def forbidden_tool_absent(
    evidence_dir: Path,
    forbidden_tools: frozenset[str] | set[str],
) -> dict[str, Any]:
    """Check that no forbidden tool appears in the tool transcript.

    Inspects the frozen ``tool_transcript.json``, not actor narrative.
    Returns ``undetermined`` when the transcript is missing.
    """
    transcript = _load_tool_transcript(evidence_dir)
    if transcript is None:
        return _undetermined(
            "tool_transcript.json is missing; cannot verify forbidden tools.",
            missing_path=str(_ps_dir(evidence_dir) / "tool_transcript.json"),
        )
    forbidden = set(forbidden_tools)
    if not forbidden:
        return _ok("No forbidden tools declared — check vacuously passes.")
    seen = _collect_tool_names(transcript)
    violations = sorted(forbidden & seen)
    if violations:
        return _fail(
            f"Forbidden tool(s) used: {', '.join(violations)}."
        )
    return _ok(f"No forbidden tools used (forbidden set: {sorted(forbidden)}).")


def tool_args_match(
    evidence_dir: Path,
    tool_name: str,
    expected_args: dict[str, Any],
) -> dict[str, Any]:
    """Check that at least one call to *tool_name* used the expected args.

    Inspects the frozen ``tool_transcript.json`` for tool-call arguments.
    Returns ``undetermined`` when the transcript is missing or the named
    tool was never called.
    """
    transcript = _load_tool_transcript(evidence_dir)
    if transcript is None:
        return _undetermined(
            f"tool_transcript.json is missing; cannot verify args for {tool_name}.",
            missing_path=str(_ps_dir(evidence_dir) / "tool_transcript.json"),
        )
    seq = _collect_tool_call_sequence(transcript)
    calls = [step for step in seq if step.get("tool_name") == tool_name]
    if not calls:
        return _undetermined(
            f"Tool '{tool_name}' was never called; cannot verify arguments.",
        )
    for step in calls:
        actual_args = step.get("args", {})
        if not isinstance(actual_args, dict):
            continue
        match = True
        for key, expected_val in expected_args.items():
            actual_val = actual_args.get(key)
            if actual_val != expected_val:
                match = False
                break
        if match:
            return _ok(
                f"Tool '{tool_name}' called with matching args: "
                f"{json.dumps(expected_args, sort_keys=True)}."
            )
    sample = json.dumps(calls[0].get("args", {}), sort_keys=True)
    return _fail(
        f"No call to '{tool_name}' matched expected args "
        f"{json.dumps(expected_args, sort_keys=True)}. "
        f"First observed args: {sample}."
    )


def message_ids_returned(
    evidence_dir: Path,
    expected_message_ids: list[str],
) -> dict[str, Any]:
    """Check that expected message IDs appear in tool-call results.

    Aggregates ``retrieved_message_ids`` from every step in the frozen
    ``tool_transcript.json``.  Returns ``undetermined`` when the
    transcript is missing.
    """
    transcript = _load_tool_transcript(evidence_dir)
    if transcript is None:
        return _undetermined(
            "tool_transcript.json is missing; cannot verify retrieved message IDs.",
            missing_path=str(_ps_dir(evidence_dir) / "tool_transcript.json"),
        )
    expected = {str(mid) for mid in expected_message_ids}
    if not expected:
        return _ok("No expected message IDs declared — check vacuously passes.")
    retrieved = _collect_retrieved_message_ids(transcript)
    missing = sorted(expected - retrieved)
    if missing:
        return _fail(
            f"Expected message ID(s) not retrieved: {', '.join(missing)}. "
            f"Retrieved: {sorted(retrieved) or '(none)'}."
        )
    return _ok(f"All {len(expected)} expected message IDs retrieved.")


def quote_present(
    evidence_dir: Path,
    expected_quotes: list[str],
) -> dict[str, Any]:
    """Check that expected quote fragments appear in ``final_answer.md``.

    Each quote is checked as a case‑insensitive substring of the final
    answer text.  Returns ``undetermined`` when ``final_answer.md`` is
    missing.
    """
    answer_path = _ps_dir(evidence_dir) / "final_answer.md"
    answer_text = _read_text(answer_path)
    if answer_text is None:
        return _undetermined(
            "final_answer.md is missing; cannot verify expected quotes.",
            missing_path=str(answer_path),
        )
    answer_lower = answer_text.lower()
    missing: list[str] = []
    for quote in expected_quotes:
        if quote.lower() not in answer_lower:
            missing.append(quote)
    if missing:
        return _fail(
            f"Expected quote(s) not found in final answer: {', '.join(repr(q) for q in missing)}."
        )
    return _ok(f"All {len(expected_quotes)} expected quotes found in final answer.")


def suppressed_ids_absent(
    evidence_dir: Path,
    suppressed_ids: frozenset[str] | set[str],
) -> dict[str, Any]:
    """Check that suppressed message IDs are absent from both the tool
    transcript and the final answer.

    Inspects ``tool_transcript.json`` retrieved message IDs *and*
    ``final_answer.md`` text.  Returns ``undetermined`` when either
    evidence file is missing.
    """
    transcript = _load_tool_transcript(evidence_dir)
    if transcript is None:
        return _undetermined(
            "tool_transcript.json is missing; cannot verify suppressed IDs.",
            missing_path=str(_ps_dir(evidence_dir) / "tool_transcript.json"),
        )
    answer_path = _ps_dir(evidence_dir) / "final_answer.md"
    answer_text = _read_text(answer_path)
    if answer_text is None:
        return _undetermined(
            "final_answer.md is missing; cannot verify suppressed IDs in final answer.",
            missing_path=str(answer_path),
        )

    suppressed = {str(sid) for sid in suppressed_ids}
    if not suppressed:
        return _ok("No suppressed IDs declared — check vacuously passes.")

    retrieved = _collect_retrieved_message_ids(transcript)
    in_transcript = sorted(suppressed & retrieved)
    violations: list[str] = []
    if in_transcript:
        violations.append(
            f"Suppressed ID(s) appeared in tool results: {', '.join(in_transcript)}"
        )

    answer_lower = answer_text.lower()
    for sid in sorted(suppressed):
        if sid.lower() in answer_lower:
            violations.append(f"Suppressed ID '{sid}' found in final_answer.md")

    if violations:
        return _fail("; ".join(violations) + ".")
    return _ok(f"No suppressed IDs ({len(suppressed)}) leaked into evidence.")


def deeper_context_called_before_answer(
    evidence_dir: Path,
    deepen_tools: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """Check that context-deepening tool(s) were called in the tool
    transcript.

    "Before answer" is inferred from the presence of at least one
    deepening call *anywhere* in the transcript — in the Sisypy
    evidence model the tool transcript captures the *entire* tool‑use
    phase that precedes the final answer.

    Returns ``undetermined`` when ``tool_transcript.json`` is missing
    or when no deepening tools are declared.
    """
    transcript = _load_tool_transcript(evidence_dir)
    if transcript is None:
        return _undetermined(
            "tool_transcript.json is missing; cannot verify deepening calls.",
            missing_path=str(_ps_dir(evidence_dir) / "tool_transcript.json"),
        )
    tools = set(deepen_tools) if deepen_tools else {
        "messages_before", "messages_after", "scroll",
        "search", "search_messages", "topic_recent",
    }
    seq = _collect_tool_call_sequence(transcript)
    if not seq:
        return _undetermined(
            "No tool calls recorded in transcript; cannot verify deepening."
        )
    deepening_calls = [
        step for step in seq if step.get("tool_name") in tools
    ]
    if not deepening_calls:
        return _fail(
            f"No context-deepening tool calls found. "
            f"Deepening tools: {sorted(tools)}. "
            f"Tools observed: {sorted({s.get('tool_name', '') for s in seq})}."
        )
    last_deepen_idx = max(
        i for i, s in enumerate(seq) if s.get("tool_name") in tools
    )
    return _ok(
        f"Context-deepening tool(s) called before answer "
        f"(last deepening at position {last_deepen_idx + 1} of {len(seq)}). "
        f"Deepening calls: {[s['tool_name'] for s in deepening_calls]}."
    )


def handled_recoverably(
    evidence_dir: Path,
    recovery_tool: str = "messages_before",
) -> dict[str, Any]:
    """Check that a tool error was followed by a recovery retry.

    Scans the frozen ``tool_transcript.json`` for a call to
    *recovery_tool* that returned ``is_error=True`` **followed by**
    another call to the same tool that succeeded.  Returns
    ``undetermined`` when the transcript is missing or the tool was
    never called.
    """
    transcript = _load_tool_transcript(evidence_dir)
    if transcript is None:
        return _undetermined(
            "tool_transcript.json is missing; cannot verify error recovery.",
            missing_path=str(_ps_dir(evidence_dir) / "tool_transcript.json"),
        )
    seq = _collect_tool_call_sequence(transcript)
    calls = [
        (
            i,
            step.get("result", {}).get("is_error", False),
            step.get("result", {}),
        )
        for i, step in enumerate(seq)
        if step.get("tool_name") == recovery_tool
    ]
    if not calls:
        return _undetermined(
            f"Tool '{recovery_tool}' was never called; cannot verify recovery.",
        )
    error_idx: int | None = None
    success_idx: int | None = None
    for i, is_error, result in calls:
        if is_error:
            if error_idx is None:
                error_idx = i
        else:
            if error_idx is not None and success_idx is None:
                success_idx = i
    if error_idx is None:
        return _ok(
            f"No error returned by '{recovery_tool}'; recovery not needed."
        )
    if success_idx is not None:
        return _ok(
            f"'{recovery_tool}' recovered after error (error at position "
            f"{error_idx + 1}, successful retry at position {success_idx + 1})."
        )
    return _fail(
        f"'{recovery_tool}' returned an error at position {error_idx + 1} "
        f"but was never retried successfully."
    )


def evidence_file_present(
    evidence_dir: Path,
    filename: str,
) -> dict[str, Any]:
    """Check that a named evidence file exists in the evidence directory.

    Searches the ``project_specific/`` subdirectory and the
    evidence‑directory root.  Returns ``undetermined`` (never ``passed``)
    when the file is missing.
    """
    candidates = [
        _ps_dir(evidence_dir) / filename,
        evidence_dir / filename,
    ]
    for candidate in candidates:
        if candidate.is_file():
            return _ok(f"Evidence file '{filename}' is present at {candidate}.")
    return _undetermined(
        f"Evidence file '{filename}' is missing.",
        missing_path=str(candidates[0]),
    )
