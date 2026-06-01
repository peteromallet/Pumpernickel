"""Real-agent and recorded-real evidence helpers for M4 Sisypy validation.

These helpers keep Sisypy's actor-dispatch side deterministic while routing the
project-specific evidence through the actual Veas eval execution path:

`evals.execution.run_eval_turn()` -> `agentic.run_agentic_turn_with_pool()` ->
`registry.call_tool()` -> `capture_tool_calls()`.
"""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5
from unittest.mock import patch

from app.models.user import User
from app.services import agentic
from app.services import hot_context as hot_context_module
from app.services.turn_context import replace_ctx
from app.services.tools.registry import call_tool
from evals.execution import run_eval_turn
from tests.agentic.fixtures.search_nav_cases import (
    SEARCH_NAV_CASES,
    SEARCH_NAV_NAMESPACE,
    SHARED_MESSAGE_POOL,
)
from tests.conftest import FakePool

_USER_ID = uuid5(SEARCH_NAV_NAMESPACE, "real-agent-user")
_PARTNER_ID = uuid5(SEARCH_NAV_NAMESPACE, "real-agent-partner")
_TOPIC_ID = uuid5(SEARCH_NAV_NAMESPACE, "real-agent-topic")
_ALT_TOPIC_ID = uuid5(SEARCH_NAV_NAMESPACE, "real-agent-alt-topic")
_DYAD_ID = uuid5(SEARCH_NAV_NAMESPACE, "real-agent-dyad")
_TRIGGER_MESSAGE_ID = uuid5(SEARCH_NAV_NAMESPACE, "real-agent-trigger")

_DEFAULT_REAL_AGENT_CASE_IDS: tuple[str, ...] = (
    "explicit_message",
    "real-agent-validation-error",
    "real-agent-malformed-cursor",
    "real-agent-missing-current-anchor",
)

_INFRA_ONLY_CASE_IDS: frozenset[str] = frozenset({"real-agent-turncontext-incompatibility"})


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def write_real_agent_evidence(
    *,
    output_dir: Path,
    case_ids: list[str] | None = None,
    prompt_version: str = "v1",
) -> dict[str, Any]:
    selected_case_ids = case_ids or list(_DEFAULT_REAL_AGENT_CASE_IDS)
    payload = asyncio.run(
        _execute_real_agent_cases(
            selected_case_ids=selected_case_ids,
            prompt_version=prompt_version,
        )
    )
    _write_payload(output_dir, payload)
    return payload


def write_recorded_real_evidence(
    *,
    output_dir: Path,
    source_dir: Path,
) -> dict[str, Any]:
    tool_transcript = _read_json(source_dir / "tool_transcript.json")
    messages_seed = _read_json(source_dir / "messages_seed.json")
    expected_behavior = _read_json(source_dir / "expected_behavior.json")
    final_answer = (source_dir / "final_answer.md").read_text(encoding="utf-8")
    hot_context = (source_dir / "hot_context.md").read_text(encoding="utf-8")
    source_infra = _read_json(source_dir / "infrastructure.json")

    transcript_cases = tool_transcript.get("cases", [])
    assertions = _recorded_assertions_from_frozen(
        transcript_cases=transcript_cases,
        expected_cases=expected_behavior.get("must", []),
    )
    infra_issues = list(source_infra.get("issues", []))
    payload = {
        "tool_transcript": {
            "mode": "recorded-real",
            "source": str(source_dir),
            "cases": transcript_cases,
            "tool_calls": tool_transcript.get("tool_calls", []),
        },
        "hot_context": hot_context,
        "messages_seed": {
            **messages_seed,
            "mode": "recorded-real",
            "source": str(source_dir),
        },
        "expected_behavior": {
            **expected_behavior,
            "mode": "recorded-real",
            "source": str(source_dir),
        },
        "final_answer": final_answer,
        "assertions": {
            "mode": "recorded-real",
            "source": str(source_dir),
            "passed": all(case["passed"] for case in assertions),
            "assertions": assertions,
        },
        "infrastructure": {
            "status": "infrastructure" if infra_issues else "ok",
            "infrastructure_failed": bool(infra_issues),
            "reason": (
                source_infra.get("reason")
                or "Recorded-real grading replayed frozen evidence."
            ),
            "issues": infra_issues,
        },
    }
    _write_payload(output_dir, payload)
    return payload


async def _execute_real_agent_cases(
    *,
    selected_case_ids: list[str],
    prompt_version: str,
) -> dict[str, Any]:
    transcript_cases: list[dict[str, Any]] = []
    assertion_cases: list[dict[str, Any]] = []
    expected_cases: list[dict[str, Any]] = []
    messages_seed_cases: list[dict[str, Any]] = []
    hot_context_sections: list[str] = []
    final_answer_sections: list[str] = []
    infrastructure_issues: list[dict[str, Any]] = []

    for case_id in selected_case_ids:
        pool, user, case_id, case_def, message_lookup = _build_real_agent_pool(case_id)
        execution, step_results, case_issues = await _run_case(
            pool=pool,
            user=user,
            case_id=case_id,
            case_def=case_def,
            prompt_version=prompt_version,
        )
        transcript_cases.append(
            {
                "case_id": case_id,
                "tool_calls": execution.tool_calls,
                "step_results": step_results,
            }
        )
        assertion = _assert_real_agent_case(
            case_id=case_id,
            case_def=case_def,
            step_results=step_results,
            transcript_json=execution.tool_calls,
        )
        assertion_cases.append(assertion)
        expected_cases.append(_expected_behavior_case(case_id, case_def))
        messages_seed_cases.append(
            _messages_seed_case(
                case_id=case_id,
                case_def=case_def,
                message_lookup=message_lookup,
            )
        )
        hot_context_sections.append(
            _hot_context_section(
                case_id=case_id,
                case_def=case_def,
                message_lookup=message_lookup,
            )
        )
        final_answer_sections.append(_final_answer_section(case_id, case_def))
        infrastructure_issues.extend(case_issues)

    non_infra_assertions = [
        case
        for case in assertion_cases
        if case["case_id"] not in _INFRA_ONLY_CASE_IDS
    ]
    payload = {
        "tool_transcript": {
            "mode": "real-agent",
            "source": "evals.execution.run_eval_turn",
            "tool_calls": [
                {"case_id": case["case_id"], **call}
                for case in transcript_cases
                for call in case["tool_calls"]
            ],
            "cases": transcript_cases,
        },
        "hot_context": "\n\n".join(hot_context_sections).strip() + "\n",
        "messages_seed": {
            "mode": "real-agent",
            "cases": messages_seed_cases,
        },
        "expected_behavior": {
            "mode": "real-agent",
            "must": expected_cases,
        },
        "final_answer": "\n\n".join(final_answer_sections).strip() + "\n",
        "assertions": {
            "mode": "real-agent",
            "passed": all(case["passed"] for case in non_infra_assertions),
            "assertions": assertion_cases,
        },
        "infrastructure": {
            "status": "infrastructure" if infrastructure_issues else "ok",
            "infrastructure_failed": bool(infrastructure_issues),
            "reason": (
                f"{len(infrastructure_issues)} real-agent infrastructure issue(s) recorded."
                if infrastructure_issues
                else "Real-agent eval execution completed without infrastructure incompatibilities."
            ),
            "issues": infrastructure_issues,
        },
    }
    return payload


async def _run_case(
    *,
    pool: FakePool,
    user: User,
    case_id: str,
    case_def: dict[str, Any],
    prompt_version: str,
) -> tuple[Any, list[dict[str, Any]], list[dict[str, Any]]]:
    step_results: list[dict[str, Any]] = []
    infrastructure_issues: list[dict[str, Any]] = []

    async def fake_run_step(
        client: Any,
        ctx: Any,
        system_prompt: str,
        hot_context_rendered: str,
        allowed_tools: set[str],
        seed_messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> tuple[str, list[dict[str, Any]], int]:
        del client, system_prompt, hot_context_rendered, allowed_tools, seed_messages, kwargs
        if ctx.current_step in {"read", "respond"} and not step_results:
            tool_ctx = ctx if ctx.current_step == "read" else replace_ctx(ctx, current_step="read")
            anchor_payload = _case_anchor_payload(case_id, case_def, pool)
            if anchor_payload is not None:
                tool_ctx.hot_context_window_edge = anchor_payload
                tool_ctx.extras["hot_context_edge"] = anchor_payload
            last_result: dict[str, Any] | None = None
            for step in _steps_for_case(case_id, case_def):
                working_ctx = tool_ctx
                if step.get("mutate_ctx") == "missing_current_anchor":
                    working_ctx = replace_ctx(
                        tool_ctx,
                        hot_context_window_edge=None,
                        extras={
                            **dict(tool_ctx.extras),
                            "hot_context_edge": None,
                        },
                    )
                elif step.get("mutate_ctx") == "missing_primary_topic":
                    working_ctx = replace_ctx(tool_ctx, primary_topic_id=None)
                raw_args = {
                    key: (last_result or {}).get("cursor") if value == "$last_cursor" else value
                    for key, value in dict(step["args"]).items()
                }
                try:
                    result = await call_tool(step["tool_name"], raw_args, working_ctx)
                except Exception as exc:
                    infrastructure_issues.append(
                        {
                            "case_id": case_id,
                            "kind": "turn_context_incompatibility",
                            "reason": str(exc),
                            "tool_name": step["tool_name"],
                        }
                    )
                    step_results.append(
                        {
                            "tool_name": step["tool_name"],
                            "args": raw_args,
                            "exception": type(exc).__name__,
                            "message": str(exc),
                            "retrieved_message_ids": [],
                        }
                    )
                    continue
                last_result = result
                step_results.append(
                    {
                        "tool_name": step["tool_name"],
                        "args": raw_args,
                        "result": result,
                        "retrieved_message_ids": _message_ids_from_result(result),
                    }
                )
            if ctx.current_step == "read":
                return "", [{"role": "assistant", "content": "read complete"}], len(step_results)
        if ctx.current_step == "respond":
            answer = _expected_conclusion(case_id, case_def)
            return answer, [{"role": "assistant", "content": answer}], 0
        return "", [{"role": "assistant", "content": f"{ctx.current_step} complete"}], 0

    async def fake_send_outbound(
        active_pool: FakePool,
        recipient: User,
        content: str,
        bot_turn_id: UUID | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del bot_turn_id, kwargs
        outbound_id = uuid5(SEARCH_NAV_NAMESPACE, f"{case_id}-outbound")
        active_pool.messages[outbound_id] = {
            "id": outbound_id,
            "direction": "outbound",
            "sender_id": None,
            "recipient_id": recipient.id,
            "content": content,
            "processing_state": "processed",
            "sent_at": datetime.now(UTC),
            "charge": None,
            "deleted_at": None,
            "bot_id": "mediator",
            "topic_id": _TOPIC_ID,
            "dyad_id": _DYAD_ID,
        }
        return {
            "status": "sent",
            "message_id": outbound_id,
            "visible_to_user": True,
            "provider_message_id": None,
        }

    async def fake_hybrid_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        return []

    with patch.object(agentic, "run_step", fake_run_step), patch.object(
        agentic, "send_outbound", fake_send_outbound
    ), patch.object(
        hot_context_module, "hybrid_search", fake_hybrid_search
    ):
        execution = await run_eval_turn(
            pool,
            [_TRIGGER_MESSAGE_ID],
            user,
            prompt_version=prompt_version,
        )
    return execution, step_results, infrastructure_issues


def _build_real_agent_pool(
    case_key: str,
) -> tuple[FakePool, User, str, dict[str, Any], dict[str, dict[str, Any]]]:
    user = User(_USER_ID, "You", "15555550100", "Europe/Berlin", "welcomed")
    partner = User(_PARTNER_ID, "Alice", "15555550101", "Europe/Berlin", "welcomed")
    pool = FakePool()
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
        "onboarding_state": "welcomed",
    }
    pool.users[partner.id] = {
        "id": partner.id,
        "name": partner.name,
        "phone": partner.phone,
        "timezone": partner.timezone,
        "onboarding_state": "welcomed",
    }
    pool.dyad_partners[user.id] = partner.id
    pool.dyad_partners[partner.id] = user.id

    case_def = deepcopy(SEARCH_NAV_CASES.get(case_key, {}))
    case_id = str(case_def.get("id") or case_key)
    message_lookup: dict[str, dict[str, Any]] = {}
    for raw in SHARED_MESSAGE_POOL:
        message_id = UUID(raw["id"])
        sender_id = user.id if raw["sender_label"] == "You" else partner.id
        recipient_id = partner.id if raw["sender_label"] == "You" else user.id
        topic_id = _ALT_TOPIC_ID if _is_alt_topic(case_id, raw["id"]) else _TOPIC_ID
        row = {
            "id": message_id,
            "direction": raw["direction"],
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "content": raw["content"],
            "canonical_text": raw["content"],
            "processing_state": "processed",
            "sent_at": _parse_dt(raw["sent_at"]),
            "charge": "routine",
            "whatsapp_message_id": f"wa-{message_id}",
            "media_type": None,
            "media_url": None,
            "media_duration_seconds": None,
            "media_analysis": None,
            "deleted_at": None,
            "edit_history": None,
            "edited_at": None,
            "bot_id": "mediator",
            "topic_id": topic_id,
            "dyad_id": _DYAD_ID,
            "thread_owner_user_id": user.id if raw["sender_label"] == "You" else partner.id,
            "thread_owner_partner_share": "opt_in",
            "search_suppressed_at": raw["sent_at"] if raw.get("suppressed") else None,
        }
        pool.messages[message_id] = row
        message_lookup[raw["id"]] = {
            "id": raw["id"],
            "topic_id": str(topic_id),
            "sent_at": raw["sent_at"],
            "content": raw["content"],
        }

    trigger_sent_at = datetime(2026, 6, 1, 5, 16, tzinfo=UTC)
    pool.messages[_TRIGGER_MESSAGE_ID] = {
        "id": _TRIGGER_MESSAGE_ID,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": _trigger_content(case_id, case_def),
        "canonical_text": _trigger_content(case_id, case_def),
        "processing_state": "raw",
        "sent_at": trigger_sent_at,
        "charge": "routine",
        "whatsapp_message_id": "wa-real-agent-trigger",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "deleted_at": None,
        "edit_history": None,
        "edited_at": None,
        "bot_id": "mediator",
        "topic_id": _TOPIC_ID,
        "dyad_id": _DYAD_ID,
        "thread_owner_user_id": user.id,
        "thread_owner_partner_share": "opt_in",
        "search_suppressed_at": None,
    }
    return pool, user, case_id, case_def, message_lookup


def _steps_for_case(case_id: str, case_def: dict[str, Any]) -> list[dict[str, Any]]:
    if case_id == "search-nav-current-anchor":
        return [
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": 5}},
            {"tool_name": "messages_after", "args": {"anchor": "current", "n": 4}},
        ]
    if case_id == "search-nav-explicit-message":
        return [
            {
                "tool_name": "messages_before",
                "args": {"anchor": case_def["anchor_message_id"], "n": 2},
            },
            {
                "tool_name": "messages_after",
                "args": {"anchor": case_def["anchor_message_id"], "n": 2},
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
                    "mode": "hybrid",
                    "scope": "topic",
                    "limit": 6,
                },
            }
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
                "args": {"anchor": str(uuid5(SEARCH_NAV_NAMESPACE, "missing-anchor")), "n": 5},
            },
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": 8}},
            {"tool_name": "messages_after", "args": {"anchor": "current", "n": 4}},
        ]
    if case_id == "real-agent-validation-error":
        return [
            {"tool_name": "messages_before", "args": {"anchor": "current", "n": "oops"}},
        ]
    if case_id == "real-agent-malformed-cursor":
        return [
            {"tool_name": "scroll", "args": {"cursor": "not-a-real-cursor", "direction": "older", "n": 4}},
        ]
    if case_id == "real-agent-missing-current-anchor":
        return [
            {
                "tool_name": "messages_before",
                "args": {"anchor": "current", "n": 4},
                "mutate_ctx": "missing_current_anchor",
            },
        ]
    if case_id == "real-agent-turncontext-incompatibility":
        return [
            {
                "tool_name": "topic_recent",
                "args": {"n": 4},
                "mutate_ctx": "missing_primary_topic",
            },
        ]
    raise KeyError(f"unsupported real-agent case: {case_id}")


def _assert_real_agent_case(
    *,
    case_id: str,
    case_def: dict[str, Any],
    step_results: list[dict[str, Any]],
    transcript_json: list[dict[str, Any]],
) -> dict[str, Any]:
    if case_id == "real-agent-validation-error":
        return _error_case_assertion(case_id, transcript_json, "validation:")
    if case_id == "real-agent-malformed-cursor":
        return _error_case_assertion(case_id, transcript_json, "invalid_cursor")
    if case_id == "real-agent-missing-current-anchor":
        return _error_case_assertion(case_id, transcript_json, "missing_current_anchor")
    if case_id == "real-agent-turncontext-incompatibility":
        step = step_results[0] if step_results else {}
        passed = step.get("exception") == "ValueError"
        return {
            "case_id": case_id,
            "passed": passed,
            "details": step,
        }
    seen_tools = {call["tool_name"] for call in transcript_json}
    retrieved_ids = {
        message_id
        for step in step_results
        for message_id in step.get("retrieved_message_ids", [])
    }
    expected_ids = set(case_def.get("expected_message_ids", []))
    required_tools = set(case_def.get("required_tools", []))
    forbidden_tools = set(case_def.get("forbidden_tools", []))
    details = {
        "required_tools_seen": sorted(required_tools & seen_tools),
        "missing_required_tools": sorted(required_tools - seen_tools),
        "forbidden_tools_seen": sorted(forbidden_tools & seen_tools),
        "retrieved_expected_ids": sorted(expected_ids & retrieved_ids),
        "missing_expected_ids": sorted(expected_ids - retrieved_ids),
    }
    if case_id == "search-nav-malformed-recovery":
        first_result = (step_results[0] or {}).get("result", {}) if step_results else {}
        details["recovery_error_observed"] = bool(first_result.get("is_error"))
        details["recovery_error_code"] = first_result.get("error_code") or first_result.get("error")
    passed = (
        not details["missing_required_tools"]
        and not details["forbidden_tools_seen"]
        and bool(details["retrieved_expected_ids"])
    )
    return {
        "case_id": case_id,
        "passed": passed,
        "details": details,
    }


def _error_case_assertion(
    case_id: str,
    transcript_json: list[dict[str, Any]],
    expected_token: str,
) -> dict[str, Any]:
    call = transcript_json[0] if transcript_json else {}
    result = call.get("result", {})
    passed = expected_token in json.dumps(result, sort_keys=True)
    return {
        "case_id": case_id,
        "passed": passed,
        "details": result,
    }


def _recorded_assertions_from_frozen(
    *,
    transcript_cases: list[dict[str, Any]],
    expected_cases: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    expected_by_case = {case["case_id"]: case for case in expected_cases}
    assertions: list[dict[str, Any]] = []
    for transcript_case in transcript_cases:
        case_id = transcript_case["case_id"]
        expected = expected_by_case.get(case_id, {})
        if expected.get("expected_error_token"):
            assertions.append(
                _error_case_assertion(
                    case_id,
                    transcript_case.get("tool_calls", []),
                    expected["expected_error_token"],
                )
            )
            continue
        case_def = {
            "expected_message_ids": expected.get("expected_message_ids", []),
            "required_tools": expected.get("required_tools", []),
            "forbidden_tools": expected.get("forbidden_tools", []),
        }
        assertions.append(
            _assert_real_agent_case(
                case_id=case_id,
                case_def=case_def,
                step_results=transcript_case.get("step_results", []),
                transcript_json=transcript_case.get("tool_calls", []),
            )
        )
    return assertions


def _expected_behavior_case(case_id: str, case_def: dict[str, Any]) -> dict[str, Any]:
    if case_id == "real-agent-validation-error":
        return {"case_id": case_id, "expected_error_token": "validation:"}
    if case_id == "real-agent-malformed-cursor":
        return {"case_id": case_id, "expected_error_token": "invalid_cursor"}
    if case_id == "real-agent-missing-current-anchor":
        return {"case_id": case_id, "expected_error_token": "missing_current_anchor"}
    if case_id == "real-agent-turncontext-incompatibility":
        return {"case_id": case_id, "expected_error_token": "missing primary_topic_id"}
    return {
        "case_id": case_id,
        "required_tools": sorted(case_def.get("required_tools", [])),
        "forbidden_tools": sorted(case_def.get("forbidden_tools", [])),
        "expected_message_ids": list(case_def.get("expected_message_ids", [])),
        "expected_quotes": list(case_def.get("expected_quotes", [])),
        "expected_conclusion": _expected_conclusion(case_id, case_def),
    }


def _messages_seed_case(
    *,
    case_id: str,
    case_def: dict[str, Any],
    message_lookup: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    expected_ids = case_def.get("expected_message_ids", [])
    return {
        "case_id": case_id,
        "messages": [
            message_lookup[message_id]
            for message_id in expected_ids
            if message_id in message_lookup
        ],
    }


def _hot_context_section(
    *,
    case_id: str,
    case_def: dict[str, Any],
    message_lookup: dict[str, dict[str, Any]],
) -> str:
    lines = [f"## {case_id}"]
    if case_def:
        lines.append(f"Question: {case_def['final_answer_grounding']['question']}")
        for message_id in case_def.get("hot_context_messages", []):
            row = message_lookup.get(message_id)
            if row is not None:
                lines.append(f"- {message_id}: {row['content']}")
    else:
        lines.append(f"Diagnostic case: {case_id}")
    return "\n".join(lines)


def _final_answer_section(case_id: str, case_def: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"## {case_id}",
            _expected_conclusion(case_id, case_def),
        ]
    )


def _expected_conclusion(case_id: str, case_def: dict[str, Any]) -> str:
    if case_def:
        return case_def["final_answer_grounding"]["expected_conclusion"]
    if case_id == "real-agent-validation-error":
        return "The agent should correct the malformed tool arguments instead of fabricating results."
    if case_id == "real-agent-malformed-cursor":
        return "The agent should discard the malformed cursor and restart from a fresh nav result."
    if case_id == "real-agent-missing-current-anchor":
        return "The agent should ask for a concrete anchor or retry once the current anchor is available."
    return "The run should surface an infrastructure incompatibility instead of grading behavior."


def _trigger_content(case_id: str, case_def: dict[str, Any]) -> str:
    if case_def:
        return case_def["final_answer_grounding"]["question"]
    return f"Trigger {case_id}"


def _message_ids_from_result(result: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("messages", "hits"):
        for item in result.get(key, []):
            message_id = item.get("message_id") or item.get("id")
            if message_id:
                ids.append(str(message_id))
    return ids


def _is_alt_topic(case_id: str, message_id: str) -> bool:
    return case_id == "search-nav-topic-recent" and message_id in {
        str(uuid5(SEARCH_NAV_NAMESPACE, "m16")),
        str(uuid5(SEARCH_NAV_NAMESPACE, "m17")),
        str(uuid5(SEARCH_NAV_NAMESPACE, "m18")),
    }


def _case_anchor_payload(
    case_id: str,
    case_def: dict[str, Any],
    pool: FakePool,
) -> dict[str, Any] | None:
    if case_id == "real-agent-missing-current-anchor":
        return None
    anchor_id = case_def.get("hot_context_edge_after") or case_def.get("anchor_message_id")
    if not isinstance(anchor_id, str):
        return None
    row = pool.messages.get(UUID(anchor_id))
    if row is None:
        return None
    return {
        "message_id": anchor_id,
        "sent_at": row["sent_at"].isoformat(),
    }


def _write_payload(output_dir: Path, payload: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "tool_transcript.json").write_text(
        json.dumps(payload["tool_transcript"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "hot_context.md").write_text(
        payload["hot_context"], encoding="utf-8"
    )
    (output_dir / "messages_seed.json").write_text(
        json.dumps(payload["messages_seed"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "expected_behavior.json").write_text(
        json.dumps(payload["expected_behavior"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "final_answer.md").write_text(
        payload["final_answer"], encoding="utf-8"
    )
    (output_dir / "assertions.json").write_text(
        json.dumps(payload["assertions"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_dir / "infrastructure.json").write_text(
        json.dumps(payload["infrastructure"], indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def recorded_real_source_from_env() -> Path | None:
    raw = os.environ.get("VEAS_RECORDED_REAL_SOURCE", "").strip()
    if not raw:
        return None
    return Path(raw)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
