"""Unit tests for tests/agentic/checks.py evidence helper functions.

Uses tiny JSON/text evidence fixtures written into temporary directories.
Covers positive, negative, ordering, recoverable-error, suppressed-ID,
and missing-evidence ``undetermined`` cases for all nine public helpers.
"""

from __future__ import annotations

import json
from pathlib import Path

from tests.agentic.checks import (
    deeper_context_called_before_answer,
    evidence_file_present,
    forbidden_tool_absent,
    handled_recoverably,
    message_ids_returned,
    quote_present,
    required_tool_used,
    suppressed_ids_absent,
    tool_args_match,
)


# ---------------------------------------------------------------------------
# tiny fixture helpers
# ---------------------------------------------------------------------------


def _ps_dir(evidence_dir: Path) -> Path:
    return evidence_dir / "project_specific"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_tool_transcript(
    evidence_dir: Path,
    tool_calls: list[dict] | None = None,
    cases: list[dict] | None = None,
) -> None:
    """Write a minimal tool_transcript.json into project_specific/."""
    payload: dict = {}
    if tool_calls is not None:
        payload["tool_calls"] = tool_calls
    if cases is not None:
        payload["cases"] = cases
    _write(_ps_dir(evidence_dir) / "tool_transcript.json", json.dumps(payload))


def _write_final_answer(evidence_dir: Path, text: str) -> None:
    _write(_ps_dir(evidence_dir) / "final_answer.md", text)


def _tc(name: str, args: dict | None = None, result: dict | None = None) -> dict:
    """Build a single tool-call dict."""
    call: dict = {"tool_name": name}
    if args is not None:
        call["args"] = args
    if result is not None:
        call["result"] = result
    return call


def _sr(
    tool_name: str,
    args: dict | None = None,
    result: dict | None = None,
    retrieved_message_ids: list[str] | None = None,
) -> dict:
    """Build a single step_result dict (used inside cases)."""
    step: dict = {"tool_name": tool_name}
    if args is not None:
        step["args"] = args
    if result is not None:
        step["result"] = result
    if retrieved_message_ids is not None:
        step["retrieved_message_ids"] = retrieved_message_ids
    return step


def _cs_case(name: str, step_results: list[dict]) -> dict:
    """Build a single case dict."""
    return {"name": name, "step_results": step_results}


# ---------------------------------------------------------------------------
# required_tool_used
# ---------------------------------------------------------------------------


class TestRequiredToolUsed:
    def test_missing_transcript_undetermined(self, tmp_path: Path) -> None:
        result = required_tool_used(tmp_path, frozenset({"search"}))
        assert result["passed"] is False
        assert result["undetermined"] is True
        assert result["severity"] == "undetermined"

    def test_no_required_tools_vacuously_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        result = required_tool_used(tmp_path, frozenset())
        assert result["passed"] is True
        assert result["severity"] == "ok"

    def test_required_tool_present_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            tool_calls=[_tc("search"), _tc("messages_before")],
        )
        result = required_tool_used(tmp_path, frozenset({"search"}))
        assert result["passed"] is True

    def test_required_tool_missing_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("messages_after")])
        result = required_tool_used(tmp_path, frozenset({"search"}))
        assert result["passed"] is False
        assert result["severity"] == "error"
        assert "search" in result["detail"]

    def test_required_tools_from_cases(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case("c1", [_sr("search"), _sr("messages_before")]),
            ],
        )
        result = required_tool_used(tmp_path, frozenset({"search", "messages_before"}))
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# forbidden_tool_absent
# ---------------------------------------------------------------------------


class TestForbiddenToolAbsent:
    def test_missing_transcript_undetermined(self, tmp_path: Path) -> None:
        result = forbidden_tool_absent(tmp_path, frozenset({"search"}))
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_no_forbidden_tools_vacuously_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        result = forbidden_tool_absent(tmp_path, frozenset())
        assert result["passed"] is True

    def test_forbidden_tool_absent_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        result = forbidden_tool_absent(tmp_path, frozenset({"delete_message"}))
        assert result["passed"] is True

    def test_forbidden_tool_present_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            tool_calls=[_tc("search"), _tc("delete_message")],
        )
        result = forbidden_tool_absent(tmp_path, frozenset({"delete_message"}))
        assert result["passed"] is False
        assert result["severity"] == "error"
        assert "delete_message" in result["detail"]

    def test_forbidden_tool_from_cases_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case("c1", [_sr("search"), _sr("delete_message")]),
            ],
        )
        result = forbidden_tool_absent(tmp_path, frozenset({"delete_message"}))
        assert result["passed"] is False


# ---------------------------------------------------------------------------
# tool_args_match
# ---------------------------------------------------------------------------


class TestToolArgsMatch:
    def test_missing_transcript_undetermined(self, tmp_path: Path) -> None:
        result = tool_args_match(tmp_path, "search", {"query": "hello"})
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_tool_never_called_undetermined(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("messages_before")])
        result = tool_args_match(tmp_path, "search", {"query": "hello"})
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_args_match_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            tool_calls=[
                _tc("search", args={"query": "hello", "limit": 5}),
            ],
        )
        result = tool_args_match(tmp_path, "search", {"query": "hello"})
        assert result["passed"] is True

    def test_args_match_from_cases(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [
                        _sr("messages_before", args={"anchor": "m07", "limit": 3}),
                    ],
                ),
            ],
        )
        result = tool_args_match(
            tmp_path, "messages_before", {"anchor": "m07", "limit": 3}
        )
        assert result["passed"] is True

    def test_args_partial_mismatch_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            tool_calls=[_tc("search", args={"query": "wrong"})],
        )
        result = tool_args_match(tmp_path, "search", {"query": "hello"})
        assert result["passed"] is False
        assert result["severity"] == "error"

    def test_args_match_second_call(self, tmp_path: Path) -> None:
        """If the first call has wrong args but a later one matches, passes."""
        _write_tool_transcript(
            tmp_path,
            tool_calls=[
                _tc("search", args={"query": "wrong"}),
                _tc("search", args={"query": "right", "limit": 10}),
            ],
        )
        result = tool_args_match(tmp_path, "search", {"query": "right"})
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# message_ids_returned
# ---------------------------------------------------------------------------


class TestMessageIdsReturned:
    def test_missing_transcript_undetermined(self, tmp_path: Path) -> None:
        result = message_ids_returned(tmp_path, ["m01", "m02"])
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_no_expected_ids_vacuously_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        result = message_ids_returned(tmp_path, [])
        assert result["passed"] is True

    def test_ids_present_passes(self, tmp_path: Path) -> None:
        """_collect_retrieved_message_ids only reads from cases/step_results,
        so we write a case-based transcript."""
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [
                        _sr(
                            "search",
                            retrieved_message_ids=["m01", "m02", "m03"],
                        ),
                    ],
                ),
            ],
        )
        result = message_ids_returned(tmp_path, ["m01", "m03"])
        assert result["passed"] is True

    def test_ids_from_cases_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [
                        _sr(
                            "search",
                            retrieved_message_ids=["m08", "m09", "m10", "m11"],
                        ),
                    ],
                ),
            ],
        )
        result = message_ids_returned(tmp_path, ["m08", "m10"])
        assert result["passed"] is True

    def test_ids_missing_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [_sr("search", retrieved_message_ids=["m01", "m02"])],
                ),
            ],
        )
        result = message_ids_returned(tmp_path, ["m01", "m99"])
        assert result["passed"] is False
        assert result["severity"] == "error"
        assert "m99" in result["detail"]


# ---------------------------------------------------------------------------
# quote_present
# ---------------------------------------------------------------------------


class TestQuotePresent:
    def test_missing_final_answer_undetermined(self, tmp_path: Path) -> None:
        result = quote_present(tmp_path, ["hello"])
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_quotes_present_passes(self, tmp_path: Path) -> None:
        _write_final_answer(
            tmp_path,
            "The user asked about Dining Preferences and we recommended Italian cuisine.",
        )
        result = quote_present(tmp_path, ["Dining Preferences", "Italian cuisine"])
        assert result["passed"] is True

    def test_case_insensitive_match(self, tmp_path: Path) -> None:
        _write_final_answer(tmp_path, "HELLO WORLD")
        result = quote_present(tmp_path, ["hello world"])
        assert result["passed"] is True

    def test_quote_missing_fails(self, tmp_path: Path) -> None:
        _write_final_answer(tmp_path, "This is the final answer.")
        result = quote_present(tmp_path, ["not present"])
        assert result["passed"] is False
        assert result["severity"] == "error"

    def test_partial_quote_missing_fails(self, tmp_path: Path) -> None:
        _write_final_answer(tmp_path, "First quote present.")
        result = quote_present(tmp_path, ["First quote", "Missing quote"])
        assert result["passed"] is False
        assert "Missing quote" in result["detail"]


# ---------------------------------------------------------------------------
# suppressed_ids_absent
# ---------------------------------------------------------------------------


class TestSuppressedIdsAbsent:
    def test_missing_transcript_undetermined(self, tmp_path: Path) -> None:
        _write_final_answer(tmp_path, "ok")
        result = suppressed_ids_absent(tmp_path, frozenset({"m25"}))
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_missing_final_answer_undetermined(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        result = suppressed_ids_absent(tmp_path, frozenset({"m25"}))
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_no_suppressed_ids_vacuously_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        _write_final_answer(tmp_path, "ok")
        result = suppressed_ids_absent(tmp_path, frozenset())
        assert result["passed"] is True

    def test_suppressed_ids_absent_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [_sr("search", retrieved_message_ids=["m01", "m02"])],
                ),
            ],
        )
        _write_final_answer(tmp_path, "Only m01 and m02 are mentioned.")
        result = suppressed_ids_absent(tmp_path, frozenset({"m25", "m26"}))
        assert result["passed"] is True

    def test_suppressed_id_in_transcript_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [_sr("search", retrieved_message_ids=["m01", "m25", "m02"])],
                ),
            ],
        )
        _write_final_answer(tmp_path, "ok")
        result = suppressed_ids_absent(tmp_path, frozenset({"m25"}))
        assert result["passed"] is False
        assert "m25" in result["detail"]

    def test_suppressed_id_in_final_answer_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [_sr("search", retrieved_message_ids=["m01", "m02"])],
                ),
            ],
        )
        _write_final_answer(tmp_path, "The message m25 contained health info.")
        result = suppressed_ids_absent(tmp_path, frozenset({"m25"}))
        assert result["passed"] is False
        assert "m25" in result["detail"]

    def test_suppressed_id_in_answer_case_insensitive(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        _write_final_answer(tmp_path, "Reference to M25 is here.")
        result = suppressed_ids_absent(tmp_path, frozenset({"m25"}))
        assert result["passed"] is False

    def test_both_transcript_and_answer_leak_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [_sr("search", retrieved_message_ids=["m25", "m01"])],
                ),
            ],
        )
        _write_final_answer(tmp_path, "See m26 for details.")
        result = suppressed_ids_absent(tmp_path, frozenset({"m25", "m26"}))
        assert result["passed"] is False
        detail = result["detail"]
        assert "m25" in detail
        assert "m26" in detail


# ---------------------------------------------------------------------------
# deeper_context_called_before_answer
# ---------------------------------------------------------------------------


class TestDeeperContextCalledBeforeAnswer:
    def test_missing_transcript_undetermined(self, tmp_path: Path) -> None:
        result = deeper_context_called_before_answer(tmp_path)
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_no_tool_calls_undetermined(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[])
        result = deeper_context_called_before_answer(tmp_path)
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_no_deepening_tools_fails(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            tool_calls=[_tc("send_message"), _tc("log_event")],
        )
        result = deeper_context_called_before_answer(tmp_path)
        assert result["passed"] is False
        assert result["severity"] == "error"
        assert "No context-deepening tool calls found" in result["detail"]

    def test_deepening_called_passes(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            tool_calls=[
                _tc("search"),
                _tc("messages_before"),
                _tc("send_message"),
            ],
        )
        result = deeper_context_called_before_answer(tmp_path)
        assert result["passed"] is True
        assert "messages_before" in result["detail"]
        assert "search" in result["detail"]

    def test_ordering_last_deepen_recorded(self, tmp_path: Path) -> None:
        """Verify that the last deepening position is correctly reported."""
        _write_tool_transcript(
            tmp_path,
            tool_calls=[
                _tc("messages_before"),
                _tc("search"),
                _tc("send_message"),
                _tc("topic_recent"),
            ],
        )
        result = deeper_context_called_before_answer(tmp_path)
        assert result["passed"] is True
        # topic_recent is at position 4 (1-indexed)
        assert "position 4" in result["detail"]

    def test_custom_deepen_tools(self, tmp_path: Path) -> None:
        """Only the supplied deepen_tools count."""
        _write_tool_transcript(
            tmp_path,
            tool_calls=[_tc("messages_before")],
        )
        # Default deepen_tools includes messages_before; custom excludes it.
        result = deeper_context_called_before_answer(
            tmp_path,
            deepen_tools=frozenset({"search", "search_messages"}),
        )
        assert result["passed"] is False
        assert "No context-deepening tool calls found" in result["detail"]

    def test_deepening_from_cases(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [
                        _sr("messages_before", args={"anchor": "m07"}),
                        _sr("send_message"),
                    ],
                ),
            ],
        )
        result = deeper_context_called_before_answer(tmp_path)
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# handled_recoverably
# ---------------------------------------------------------------------------


class TestHandledRecoverably:
    def test_missing_transcript_undetermined(self, tmp_path: Path) -> None:
        result = handled_recoverably(tmp_path)
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_tool_never_called_undetermined(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path, tool_calls=[_tc("search")])
        result = handled_recoverably(tmp_path, recovery_tool="messages_before")
        assert result["passed"] is False
        assert result["undetermined"] is True

    def test_no_error_ok(self, tmp_path: Path) -> None:
        """If the tool is called but never returns an error, recovery not needed."""
        _write_tool_transcript(
            tmp_path,
            tool_calls=[
                _tc(
                    "messages_before",
                    result={"messages": [], "is_error": False},
                ),
            ],
        )
        result = handled_recoverably(tmp_path, recovery_tool="messages_before")
        assert result["passed"] is True
        assert "recovery not needed" in result["detail"]

    def test_error_recovered_passes(self, tmp_path: Path) -> None:
        """Error on first call, successful retry on second."""
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [
                        _sr(
                            "messages_before",
                            args={"anchor": "m999"},
                            result={"is_error": True, "error": "not_found"},
                        ),
                        _sr(
                            "messages_before",
                            args={"anchor": "m07"},
                            result={"messages": ["m05", "m06"]},
                        ),
                    ],
                ),
            ],
        )
        result = handled_recoverably(tmp_path, recovery_tool="messages_before")
        assert result["passed"] is True
        assert "recovered after error" in result["detail"]

    def test_error_not_recovered_fails(self, tmp_path: Path) -> None:
        """Error on call, no successful retry."""
        _write_tool_transcript(
            tmp_path,
            tool_calls=[
                _tc(
                    "messages_before",
                    result={"is_error": True, "error": "not_found"},
                ),
            ],
        )
        result = handled_recoverably(tmp_path, recovery_tool="messages_before")
        assert result["passed"] is False
        assert result["severity"] == "error"
        assert "never retried" in result["detail"]

    def test_recovery_with_interleaved_other_tools(self, tmp_path: Path) -> None:
        """Error, then some other tool, then successful retry — should still pass."""
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [
                        _sr(
                            "messages_before",
                            args={"anchor": "m999"},
                            result={"is_error": True},
                        ),
                        _sr("search"),
                        _sr(
                            "messages_before",
                            args={"anchor": "m07"},
                            result={"messages": ["m05"]},
                        ),
                    ],
                ),
            ],
        )
        result = handled_recoverably(tmp_path, recovery_tool="messages_before")
        assert result["passed"] is True

    def test_custom_recovery_tool(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            tool_calls=[
                _tc(
                    "search_messages",
                    result={"is_error": True},
                ),
                _tc(
                    "search_messages",
                    result={"messages": ["m01"]},
                ),
            ],
        )
        result = handled_recoverably(tmp_path, recovery_tool="search_messages")
        assert result["passed"] is True


# ---------------------------------------------------------------------------
# evidence_file_present
# ---------------------------------------------------------------------------


class TestEvidenceFilePresent:
    def test_file_in_project_specific(self, tmp_path: Path) -> None:
        _write(_ps_dir(tmp_path) / "expected_behavior.json", "{}")
        result = evidence_file_present(tmp_path, "expected_behavior.json")
        assert result["passed"] is True
        assert result["severity"] == "ok"

    def test_file_in_evidence_root(self, tmp_path: Path) -> None:
        _write(tmp_path / "git_diff.patch", "diff")
        result = evidence_file_present(tmp_path, "git_diff.patch")
        assert result["passed"] is True

    def test_file_missing_undetermined(self, tmp_path: Path) -> None:
        result = evidence_file_present(tmp_path, "nonexistent.json")
        assert result["passed"] is False
        assert result["undetermined"] is True
        assert result["severity"] == "undetermined"

    def test_project_specific_checked_first(self, tmp_path: Path) -> None:
        """project_specific/ is checked before evidence_dir root."""
        _write(_ps_dir(tmp_path) / "shared.json", "from ps")
        _write(tmp_path / "shared.json", "from root")
        result = evidence_file_present(tmp_path, "shared.json")
        assert result["passed"] is True
        # The detail should mention project_specific path
        assert "project_specific" in result["detail"]


# ---------------------------------------------------------------------------
# combined scenarios — checks used together against a single evidence pack
# ---------------------------------------------------------------------------


class TestCombinedScenarios:
    """End-to-end-like tests that exercise multiple checks against the same
    frozen evidence pack, modelling real rubric evaluation."""

    def test_positional_navigation_passes_all_checks(self, tmp_path: Path) -> None:
        """Messages before/after for explicit-message anchor returns correct
        IDs and the answer quotes them."""
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "explicit_message",
                    [
                        _sr(
                            "messages_before",
                            args={"anchor": "m07", "limit": 2},
                            retrieved_message_ids=["m05", "m06"],
                        ),
                        _sr(
                            "messages_after",
                            args={"anchor": "m07", "limit": 2},
                            retrieved_message_ids=["m08", "m09"],
                        ),
                    ],
                ),
            ],
        )
        _write_final_answer(
            tmp_path,
            "The messages around m07 are:\n- m05: ...\n- m06: ...\n"
            "- m08: ...\n- m09: ...",
        )

        # All checks should pass.
        assert required_tool_used(
            tmp_path, frozenset({"messages_before", "messages_after"})
        )["passed"]
        assert forbidden_tool_absent(
            tmp_path, frozenset({"search", "search_messages"})
        )["passed"]
        assert tool_args_match(
            tmp_path, "messages_before", {"anchor": "m07", "limit": 2}
        )["passed"]
        assert message_ids_returned(
            tmp_path, ["m05", "m06", "m08", "m09"]
        )["passed"]
        assert quote_present(tmp_path, ["m05", "m06", "m08", "m09"])["passed"]
        assert deeper_context_called_before_answer(tmp_path)["passed"]
        assert handled_recoverably(tmp_path)["passed"]

    def test_suppressed_case_checks(self, tmp_path: Path) -> None:
        """Agent uses valid tools only, avoids suppressed IDs, answer
        acknowledges unavailability."""
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "suppressed_deleted",
                    [
                        _sr(
                            "messages_before",
                            args={"anchor": "current", "limit": 10},
                            retrieved_message_ids=["m17", "m18", "m19", "m20"],
                        ),
                    ],
                ),
            ],
        )
        _write_final_answer(
            tmp_path,
            "Based on visible messages:\n"
            "- m17 covers trip planning\n"
            "- m18 covers hotel booking\n"
            "Two messages that are unavailable have been excluded from this analysis.",
        )

        # Suppressed IDs m25, m26 must not be in transcript results.
        assert suppressed_ids_absent(
            tmp_path, frozenset({"m25", "m26"})
        )["passed"]
        # Forbidden tools must be absent.
        assert forbidden_tool_absent(
            tmp_path, frozenset({"delete_message", "edit_message"})
        )["passed"]
        # Required tool used.
        assert required_tool_used(
            tmp_path, frozenset({"messages_before"})
        )["passed"]

    def test_deepening_insufficient_hot_context(self, tmp_path: Path) -> None:
        """The 'Previous on this topic' gist is insufficient; agent must
        call messages_before to deepen context before answering."""
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "insufficient_hot_context",
                    [
                        _sr("messages_before", args={"anchor": "current", "limit": 8}),
                        _sr(
                            "messages_before",
                            args={"anchor": "m19", "limit": 6},
                            retrieved_message_ids=[
                                "m13", "m14", "m15", "m16", "m17", "m18",
                            ],
                        ),
                        _sr("send_message"),
                    ],
                ),
            ],
        )
        _write_final_answer(
            tmp_path,
            "Deepening into earlier context:\n"
            "m13-m18 cover the trip planning in detail.",
        )

        # Deepening must be called.
        assert deeper_context_called_before_answer(tmp_path)["passed"]
        # The deepening call at position 2 is recorded.
        result = deeper_context_called_before_answer(tmp_path)
        assert "position 2" in result["detail"]

    def test_recoverable_error_retry(self, tmp_path: Path) -> None:
        """Agent hits a malformed anchor, detects error, retries with valid
        anchor, then answers."""
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "malformed_unsupported_recovery",
                    [
                        _sr(
                            "messages_before",
                            args={"anchor": "m999"},
                            result={"is_error": True, "error": "not_found"},
                        ),
                        _sr(
                            "messages_before",
                            args={"anchor": "m19"},
                            result={
                                "messages": [
                                    {"id": "m13"}, {"id": "m14"}, {"id": "m15"},
                                    {"id": "m16"}, {"id": "m17"}, {"id": "m18"},
                                ],
                            },
                            retrieved_message_ids=[
                                "m13", "m14", "m15", "m16", "m17", "m18",
                            ],
                        ),
                        _sr("send_message"),
                    ],
                ),
            ],
        )
        _write_final_answer(
            tmp_path,
            "After retrying with a valid anchor:\n"
            "- m13-m18 cover trip planning thoroughly.",
        )

        assert handled_recoverably(tmp_path)["passed"]
        # Error was at position 1, successful retry at position 2
        result = handled_recoverably(tmp_path)
        assert "position 1" in result["detail"]
        assert "position 2" in result["detail"]


# ---------------------------------------------------------------------------
# edge cases — empty / malformed evidence
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_tool_transcript(self, tmp_path: Path) -> None:
        _write_tool_transcript(tmp_path)
        # Empty transcript means no tools seen.
        r = required_tool_used(tmp_path, frozenset({"search"}))
        assert r["passed"] is False  # missing required tool

    def test_malformed_transcript_json(self, tmp_path: Path) -> None:
        _write(_ps_dir(tmp_path) / "tool_transcript.json", "not json")
        r = required_tool_used(tmp_path, frozenset({"search"}))
        assert r["passed"] is False
        assert r["undetermined"] is True

    def test_empty_final_answer(self, tmp_path: Path) -> None:
        _write_tool_transcript(
            tmp_path,
            cases=[
                _cs_case(
                    "c1",
                    [_sr("search", retrieved_message_ids=["m01"])],
                ),
            ],
        )
        _write_final_answer(tmp_path, "")
        # Empty answer still works — no quotes will match.
        r = quote_present(tmp_path, ["something"])
        assert r["passed"] is False

    def test_missing_both_evidence_files(self, tmp_path: Path) -> None:
        """When both tool_transcript.json and final_answer.md are missing,
        suppressed_ids_absent returns undetermined (transcript check first)."""
        r = suppressed_ids_absent(tmp_path, frozenset({"m25"}))
        assert r["passed"] is False
        assert r["undetermined"] is True
        assert "tool_transcript.json" in r.get("missing_path", "")
