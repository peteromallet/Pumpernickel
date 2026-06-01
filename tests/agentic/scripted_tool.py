"""Scripted-tool evidence executor for M4 Sisypy behavior fixtures.

This module runs fixture-declared tool sequences through the real registry
bridge so scripted-tool mode proves evidence plumbing without claiming
behavior validation.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from evals.capture import capture_tool_calls

from app.models.user import User
from app.services.turn_context import TurnContext
from app.services.tools.registry import call_tool
from tests.agentic.fake_pool import AgenticFakePool
from tests.agentic.fixtures.search_nav_cases import (
    SEARCH_NAV_CASES,
    SEARCH_NAV_NAMESPACE,
    SHARED_MESSAGE_POOL,
)

_USER_ID = uuid5(SEARCH_NAV_NAMESPACE, "scripted-user")
_PARTNER_ID = uuid5(SEARCH_NAV_NAMESPACE, "scripted-partner")
_TURN_ID = uuid5(SEARCH_NAV_NAMESPACE, "scripted-turn")
_TOPIC_ID = uuid5(SEARCH_NAV_NAMESPACE, "scripted-topic")
_ALT_TOPIC_ID = uuid5(SEARCH_NAV_NAMESPACE, "scripted-alt-topic")
_MISSING_ANCHOR_ID = uuid5(SEARCH_NAV_NAMESPACE, "missing-anchor")


def write_scripted_tool_evidence(
    *,
    output_dir: Path,
    case_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected_case_ids = case_ids or sorted(SEARCH_NAV_CASES.keys())
    selected_cases = [SEARCH_NAV_CASES[case_id] for case_id in selected_case_ids]
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = asyncio.run(_execute_cases(selected_cases))
    _write_json(output_dir / "tool_transcript.json", payload["tool_transcript"])
    _write_text(output_dir / "hot_context.md", payload["hot_context"])
    _write_json(output_dir / "messages_seed.json", payload["messages_seed"])
    _write_json(output_dir / "expected_behavior.json", payload["expected_behavior"])
    _write_text(output_dir / "final_answer.md", payload["final_answer"])
    _write_json(output_dir / "assertions.json", payload["assertions"])
    _write_json(output_dir / "infrastructure.json", payload["infrastructure"])
    return payload


async def _execute_cases(selected_cases: list[dict[str, Any]]) -> dict[str, Any]:
    transcript_cases: list[dict[str, Any]] = []
    assertion_cases: list[dict[str, Any]] = []
    expected_cases: list[dict[str, Any]] = []
    messages_seed_cases: list[dict[str, Any]] = []
    hot_context_sections: list[str] = []
    final_answer_sections: list[str] = []
    infrastructure_issues: list[dict[str, Any]] = []

    for case in selected_cases:
        pool, ctx, message_lookup = _build_case_context(case)
        steps = _scripted_steps_for_case(case, message_lookup)
        with capture_tool_calls() as transcript:
            step_results = []
            last_result: dict[str, Any] | None = None
            for step in steps:
                raw_args = _resolve_args(step["args"], message_lookup, last_result)
                result = await call_tool(step["tool_name"], raw_args, ctx)
                last_result = result
                step_results.append(
                    {
                        "tool_name": step["tool_name"],
                        "args": raw_args,
                        "result": result,
                        "retrieved_message_ids": _message_ids_from_result(result),
                    }
                )
        transcript_json = transcript.as_json()
        transcript_cases.append(
            {
                "case_id": case["id"],
                "tool_calls": transcript_json,
                "step_results": step_results,
            }
        )
        case_assertions = _assert_case(case, step_results, transcript_json)
        assertion_cases.append(case_assertions)
        expected_cases.append(_expected_behavior_case(case))
        messages_seed_cases.append(_messages_seed_case(case, ctx, message_lookup))
        hot_context_sections.append(_hot_context_section(case, ctx, message_lookup))
        final_answer_sections.append(_final_answer_section(case))
        infrastructure = pool.infrastructure_status()
        if infrastructure["issues"]:
            infrastructure_issues.extend(
                [{**issue, "case_id": case["id"]} for issue in infrastructure["issues"]]
            )

    tool_calls = [
        {"case_id": case["case_id"], **call}
        for case in transcript_cases
        for call in case["tool_calls"]
    ]
    overall_ok = (
        all(case["passed"] for case in assertion_cases) and not infrastructure_issues
    )
    return {
        "tool_transcript": {
            "mode": "scripted-tool",
            "structural_only": True,
            "tool_calls": tool_calls,
            "cases": transcript_cases,
        },
        "hot_context": "\n\n".join(hot_context_sections).strip() + "\n",
        "messages_seed": {
            "mode": "scripted-tool",
            "structural_only": True,
            "cases": messages_seed_cases,
        },
        "expected_behavior": {
            "mode": "scripted-tool",
            "structural_only": True,
            "must": expected_cases,
            "note": (
                "Scripted-tool mode proves evidence plumbing only. It must not "
                "be treated as behavior success."
            ),
        },
        "final_answer": "\n\n".join(final_answer_sections).strip() + "\n",
        "assertions": {
            "mode": "scripted-tool",
            "structural_only": True,
            "passed": overall_ok,
            "assertions": assertion_cases,
        },
        "infrastructure": {
            "status": "infrastructure" if infrastructure_issues else "ok",
            "infrastructure_failed": bool(infrastructure_issues),
            "reason": (
                f"{len(infrastructure_issues)} scripted-tool infrastructure issue(s) recorded."
                if infrastructure_issues
                else "All scripted-tool fixture calls stayed within the supported fake-pool surface."
            ),
            "issues": infrastructure_issues,
        },
    }


def _build_case_context(
    case: dict[str, Any],
) -> tuple[AgenticFakePool, TurnContext, dict[str, dict[str, Any]]]:
    user = User(_USER_ID, "You", "15555550100", "Europe/Berlin")
    partner = User(_PARTNER_ID, "Alice", "15555550101", "Europe/Berlin")
    messages, message_lookup = _messages_for_case(case, user, partner)
    pool = AgenticFakePool(
        messages=messages,
        viewer_user_id=user.id,
        partner_user_id=partner.id,
        bot_id="mediator",
        topic_id=_TOPIC_ID,
        turn_id=_TURN_ID,
    )
    anchor_id = _case_anchor_message_id(case)
    anchor_row = message_lookup.get(anchor_id) if anchor_id else None
    hot_context_edge = None
    if anchor_row is not None:
        hot_context_edge = {
            "message_id": anchor_row["id"],
            "sent_at": anchor_row["sent_at"],
        }
    ctx = TurnContext(
        turn_id=_TURN_ID,
        pool=pool,
        user=user,
        partner=partner,
        triggering_message_ids=[UUID(messages[-1]["id"])],
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=_TOPIC_ID,
        current_step="read",
        flat_allowed_tools={
            step["tool_name"] for step in _scripted_steps_for_case(case, message_lookup)
        },
        hot_context_window_edge=hot_context_edge,
        turn_started_at=datetime(2026, 6, 1, 5, 16, tzinfo=UTC),
        extras={"current_anchor": hot_context_edge or {}},
    )
    pool.bot_turns[_TURN_ID] = {"id": _TURN_ID}
    return pool, ctx, message_lookup


def _case_anchor_message_id(case: dict[str, Any]) -> str | None:
    anchor_id = case.get("hot_context_edge_after") or case.get("anchor_message_id")
    if isinstance(anchor_id, str):
        return anchor_id
    if case["id"] == "search-nav-suppressed-deleted":
        return str(uuid5(SEARCH_NAV_NAMESPACE, "m18"))
    return None


def _messages_for_case(
    case: dict[str, Any], user: User, partner: User
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    messages: list[dict[str, Any]] = []
    lookup: dict[str, dict[str, Any]] = {}
    for raw in SHARED_MESSAGE_POOL:
        topic_id = _topic_for_case_message(case["id"], raw["id"])
        sender_id = user.id if raw["sender_label"] == "You" else partner.id
        recipient_id = partner.id if raw["sender_label"] == "You" else user.id
        row = {
            "id": raw["id"],
            "sender_id": str(sender_id),
            "recipient_id": str(recipient_id),
            "thread_owner_user_id": str(partner.id),
            "thread_owner_partner_share": "opt_in",
            "direction": raw["direction"],
            "sent_at": raw["sent_at"],
            "content": raw["content"],
            "bot_id": "mediator",
            "topic_id": str(topic_id),
            "charge": "routine",
            "search_suppressed_at": raw["sent_at"] if raw.get("suppressed") else None,
        }
        messages.append(row)
        lookup[raw["id"]] = row
    return messages, lookup


def _topic_for_case_message(case_id: str, message_id: str) -> UUID:
    if case_id == "search-nav-topic-recent":
        if message_id in {
            str(uuid5(SEARCH_NAV_NAMESPACE, "m16")),
            str(uuid5(SEARCH_NAV_NAMESPACE, "m17")),
            str(uuid5(SEARCH_NAV_NAMESPACE, "m18")),
        }:
            return _ALT_TOPIC_ID
    return _TOPIC_ID


def _scripted_steps_for_case(
    case: dict[str, Any],
    message_lookup: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    case_id = case["id"]
    if case_id == "search-nav-current-anchor":
        return [
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": 5}},
            {"tool_name": "messages_after", "args": {"anchor": "current", "n": 4}},
        ]
    if case_id == "search-nav-explicit-message":
        return [
            {
                "tool_name": "messages_before",
                "args": {"anchor": case["anchor_message_id"], "n": 2},
            },
            {
                "tool_name": "messages_after",
                "args": {"anchor": case["anchor_message_id"], "n": 2},
            },
        ]
    if case_id == "search-nav-scrollback-cursor":
        return [
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": 4}},
            {
                "tool_name": "scroll",
                "args": {"cursor": "$last_cursor", "direction": "older", "n": 4},
            },
        ]
    if case_id == "search-nav-semantic-paraphrase":
        return [
            {
                "tool_name": "search",
                "args": {
                    "query": "seafood restaurant",
                    "mode": "exact",
                    "scope": "topic",
                    "limit": 4,
                },
            },
            {
                "tool_name": "search_messages",
                "args": {"text_contains": "pasta", "limit": 6},
            },
        ]
    if case_id == "search-nav-topic-recent":
        return [{"tool_name": "topic_recent", "args": {"n": 6}}]
    if case_id == "search-nav-insufficient-hot-context":
        return [
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": 14}},
            {"tool_name": "messages_after", "args": {"anchor": "current", "n": 6}},
        ]
    if case_id == "search-nav-suppressed-deleted":
        return [
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": 3}},
            {"tool_name": "messages_after", "args": {"anchor": "current", "n": 4}},
        ]
    if case_id == "search-nav-malformed-recovery":
        return [
            {
                "tool_name": "messages_before",
                "args": {"anchor": str(_MISSING_ANCHOR_ID), "n": 5},
            },
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": 8}},
            {"tool_name": "messages_after", "args": {"anchor": "current", "n": 4}},
        ]
    raise KeyError(f"unsupported scripted-tool case: {case_id}")


def _resolve_args(
    raw_args: dict[str, Any],
    message_lookup: dict[str, dict[str, Any]],
    last_result: dict[str, Any] | None,
) -> dict[str, Any]:
    resolved: dict[str, Any] = {}
    for key, value in raw_args.items():
        if value == "$last_cursor":
            resolved[key] = (last_result or {}).get("cursor")
            continue
        if isinstance(value, str) and value in message_lookup:
            resolved[key] = value
            continue
        resolved[key] = value
    return resolved


def _message_ids_from_result(result: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for item in result.get("messages", []):
        message_id = item.get("message_id")
        if message_id:
            ids.append(str(message_id))
    for item in result.get("hits", []):
        message_id = item.get("message_id") or item.get("id")
        if message_id:
            ids.append(str(message_id))
    return ids


def _assert_case(
    case: dict[str, Any],
    step_results: list[dict[str, Any]],
    transcript_json: list[dict[str, Any]],
) -> dict[str, Any]:
    seen_tools = {call["tool_name"] for call in transcript_json}
    retrieved_ids = {
        message_id
        for step in step_results
        for message_id in step["retrieved_message_ids"]
    }
    expected_ids = set(case.get("expected_message_ids", []))
    required_tools = set(case.get("required_tools", []))
    forbidden_tools = set(case.get("forbidden_tools", []))
    details = {
        "required_tools_seen": sorted(required_tools & seen_tools),
        "missing_required_tools": sorted(required_tools - seen_tools),
        "forbidden_tools_seen": sorted(forbidden_tools & seen_tools),
        "retrieved_expected_ids": sorted(expected_ids & retrieved_ids),
        "missing_expected_ids": sorted(expected_ids - retrieved_ids),
    }
    if case["id"] == "search-nav-malformed-recovery":
        first_result = step_results[0]["result"] if step_results else {}
        details["recovery_error_observed"] = bool(first_result.get("is_error"))
        details["recovery_error_code"] = first_result.get(
            "error_code"
        ) or first_result.get("error")
    passed = (
        not details["missing_required_tools"]
        and not details["forbidden_tools_seen"]
        and bool(details["retrieved_expected_ids"])
    )
    return {
        "case_id": case["id"],
        "passed": passed,
        "question": case["final_answer_grounding"]["question"],
        "details": details,
    }


def _expected_behavior_case(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": case["id"],
        "required_tools": sorted(case.get("required_tools", [])),
        "forbidden_tools": sorted(case.get("forbidden_tools", [])),
        "expected_message_ids": list(case.get("expected_message_ids", [])),
        "expected_quotes": list(case.get("expected_quotes", [])),
        "non_fabrication_expectation": case.get("non_fabrication_expectation"),
        "expected_conclusion": case["final_answer_grounding"]["expected_conclusion"],
    }


def _messages_seed_case(
    case: dict[str, Any],
    ctx: TurnContext,
    message_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected_ids = case.get("expected_message_ids", [])
    return {
        "case_id": case["id"],
        "primary_topic_id": str(ctx.primary_topic_id),
        "hot_context_edge": ctx.hot_context_window_edge,
        "messages": [
            {
                "id": message_id,
                "sent_at": message_lookup[message_id]["sent_at"],
                "content": message_lookup[message_id]["content"],
                "topic_id": message_lookup[message_id]["topic_id"],
            }
            for message_id in expected_ids
            if message_id in message_lookup
        ],
    }


def _hot_context_section(
    case: dict[str, Any],
    ctx: TurnContext,
    message_lookup: dict[str, dict[str, Any]],
) -> str:
    lines = [
        f"## {case['id']}",
        f"Question: {case['final_answer_grounding']['question']}",
    ]
    if ctx.hot_context_window_edge:
        lines.append(
            "Current edge: "
            f"{ctx.hot_context_window_edge['message_id']} at {ctx.hot_context_window_edge['sent_at']}"
        )
    previous = case.get("previous_on_this_topic")
    if isinstance(previous, dict) and previous.get("summary"):
        lines.append("Previous on this topic:")
        lines.append(previous["summary"])
    visible_ids = case.get("hot_context_messages") or []
    if visible_ids:
        lines.append("Visible hot-context messages:")
        for message_id in visible_ids:
            row = message_lookup.get(message_id)
            if row is not None:
                lines.append(f"- {message_id}: {row['content']}")
    return "\n".join(lines)


def _final_answer_section(case: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"## {case['id']}",
            "Scripted-tool structural evidence only; not model-authored behavior evidence.",
            f"Expected answer target: {case['final_answer_grounding']['expected_conclusion']}",
        ]
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_text(path: Path, payload: str) -> None:
    path.write_text(payload, encoding="utf-8")
