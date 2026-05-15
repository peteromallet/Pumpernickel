"""Agentic turn lifecycle orchestration."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from datetime import timedelta
from decimal import Decimal
from typing import Any, Mapping
from uuid import UUID

import anthropic

from app.bots.registry import get_bot_spec, primary_topic_id_for
from app.config import get_settings
from app.models.user import User, claim_onboarding_welcome
from app.services import discord, hooks, system_state
from app.services.deepseek import DeepSeekClient
from app.services.hot_context import build_hot_context, render_hot_context
from app.services.hot_context_solo import (
    build_hot_context_solo,
    render_hot_context_solo,
)
from app.services import inbound_queue
from app.services.messaging import send_outbound, sent_contents_for_turn
from app.services.partner_sharing import get_partner_share
from app.services.spend import is_under_cap, record_llm_cost
from app.services.crypto import encrypt_value
from app.services.scope import InboundScope
from app.services.text_safety import clean_user_facing_text
from app.services.tools.registry import (
    STEP_ALLOWED_TOOLS,
    call_tool,
    to_anthropic_tools,
)
from app.services.turn_audit import record_turn_event
from app.services.turn_plan import make_turn_plan, orient_summary, pick_default_skeleton
from app.services.turn_context import (
    BeforePacedSend,
    TurnContext,
    obs_fields,
    partner_of,
)

import tool_schemas as _tool_schemas_module

_TOOL_SCHEMA_VERSION: str = hashlib.sha1(
    open(_tool_schemas_module.__file__, "rb").read()
).hexdigest()[:12]

logger = logging.getLogger(__name__)

_pool: Any | None = None


class AgenticTurnError(Exception):
    failure_reason = "crashed"


class SpendCapExceeded(Exception):
    failure_reason = "spend_cap"


class NewerInboundBeforeFinalSend(Exception):
    pass


class LLMPhaseError(Exception):
    failure_reason = "llm_timeout"


class BoundedLoopExceeded(Exception):
    failure_reason: str

    def __init__(self, message: str = "bounded_loop_exceeded") -> None:
        super().__init__(message)
        self.failure_reason = message


REACTION_DIRECTIVE_RE = re.compile(
    r"^\s*\[react:\s*(?P<emoji>[^\]\s]+)\s*\]\s*$", re.IGNORECASE
)
PACING_CONTEXT_KEYS = (
    "action",
    "reason",
    "wait_s",
    "wait_ms",
    "reaction",
    "source",
    "message_count",
    "typing_active",
    "latest_message_age_s",
    "contains_question",
    "contains_ack",
    "contains_closure",
    "has_media",
    "charge",
    "charges",
)
PACING_SIGNAL_KEYS = (
    "source",
    "message_count",
    "typing_active",
    "latest_message_age_s",
    "contains_question",
    "contains_ack",
    "contains_closure",
    "has_media",
    "charge",
    "charges",
)


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _compact_json_value(value: Any, *, text_limit: int = 180) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= text_limit else value[: text_limit - 3] + "..."
    if isinstance(value, Mapping):
        compact: dict[str, Any] = {}
        for key, item in value.items():
            if item is None:
                continue
            compact[str(key)] = _compact_json_value(item, text_limit=text_limit)
        return compact
    if isinstance(value, (list, tuple, set)):
        return [
            _compact_json_value(item, text_limit=text_limit) for item in list(value)[:8]
        ]
    return str(value)


def _compact_pacing_context(pacing_context: Any) -> dict[str, Any] | None:
    if pacing_context is None:
        return None

    compact: dict[str, Any] = {}
    for key in PACING_CONTEXT_KEYS:
        value = _attr(pacing_context, key)
        if value is not None:
            compact[key] = _compact_json_value(value)

    signal_snapshot = _attr(pacing_context, "signal_snapshot")
    if isinstance(signal_snapshot, Mapping):
        signal_compact = {
            key: _compact_json_value(signal_snapshot[key])
            for key in PACING_SIGNAL_KEYS
            if key in signal_snapshot and signal_snapshot[key] is not None
        }
        if signal_compact:
            compact["signals"] = signal_compact

    preference_snapshot = _attr(pacing_context, "preference_snapshot")
    if isinstance(preference_snapshot, Mapping):
        preference_keys = (
            "conversation_pace",
            "allow_reactions",
            "min_wait_s",
            "max_wait_s",
        )
        preferences = {
            key: _compact_json_value(preference_snapshot[key])
            for key in preference_keys
            if key in preference_snapshot and preference_snapshot[key] is not None
        }
        if preferences:
            compact["preferences"] = preferences

    llm_judgement = _attr(pacing_context, "llm_judgement")
    if isinstance(llm_judgement, Mapping):
        judgement_keys = ("action", "reason", "wait_s", "reaction", "fallback")
        judgement = {
            key: _compact_json_value(llm_judgement[key])
            for key in judgement_keys
            if key in llm_judgement and llm_judgement[key] is not None
        }
        if judgement:
            compact["llm"] = judgement

    if not compact and isinstance(pacing_context, Mapping):
        compact = {
            str(key): _compact_json_value(value)
            for key, value in pacing_context.items()
            if key in PACING_CONTEXT_KEYS and value is not None
        }

    return compact or None


def _trigger_metadata_with_pacing(
    trigger_metadata: Mapping[str, Any] | None,
    pacing_context: Any,
) -> dict[str, Any] | None:
    compact_pacing = _compact_pacing_context(pacing_context)
    if compact_pacing is None:
        return dict(trigger_metadata) if trigger_metadata is not None else None

    metadata = dict(trigger_metadata or {})
    context = dict(metadata.get("context") or {})
    context["pacing"] = compact_pacing
    metadata["context"] = context
    metadata["pacing"] = compact_pacing
    metadata.setdefault("kind", "inbound")
    return metadata


def _block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    block_type = _attr(block, "type")
    data: dict[str, Any] = {"type": block_type}
    if block_type == "text":
        data["text"] = _attr(block, "text", "")
    elif block_type == "tool_use":
        data["id"] = _attr(block, "id")
        data["name"] = _attr(block, "name")
        data["input"] = _attr(block, "input", {}) or {}
    elif block_type == "openai_assistant_message":
        data["message"] = _attr(block, "message", {}) or {}
    elif block_type == "reasoning_content":
        data["reasoning_content"] = _attr(block, "reasoning_content", "")
    return data


def _system_blocks(
    system_prompt: str, hot_context_rendered: str
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": hot_context_rendered},
    ]
    if len(hot_context_rendered) // 4 >= 1024:
        blocks[1]["cache_control"] = {"type": "ephemeral"}
    return blocks


def _anthropic_tools(allowed_tools: set[str]) -> list[dict[str, Any]]:
    tools = [dict(tool) for tool in to_anthropic_tools(allowed_tools)]
    if tools:
        tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _usage_tokens(usage: Any, field: str) -> int:
    value = _attr(usage, field, 0) or 0
    return int(value)


async def _record_response_cost(
    pool: Any,
    usage: Any,
    *,
    input_price: float,
    output_price: float,
) -> None:
    input_rate = Decimal(str(input_price))
    output_rate = Decimal(str(output_price))
    input_tokens = _usage_tokens(usage, "input_tokens")
    cache_create = _usage_tokens(usage, "cache_creation_input_tokens")
    cache_read = _usage_tokens(usage, "cache_read_input_tokens")
    output_tokens = _usage_tokens(usage, "output_tokens")
    regular_input_tokens = max(0, input_tokens - cache_create - cache_read)
    dollars = (
        regular_input_tokens * input_rate
        + cache_create * input_rate * Decimal("1.25")
        + cache_read * input_rate * Decimal("0.10")
        + output_tokens * output_rate
    ) / Decimal("1000000")
    if dollars > 0:
        await record_llm_cost(pool, "text", dollars)


def _deepseek_user_names(settings: Any) -> set[str]:
    return {
        name.strip().casefold()
        for name in settings.deepseek_enabled_user_names.split(",")
        if name.strip()
    }


def _llm_client_and_model_for_user(user: User) -> tuple[Any, str, str]:
    settings = get_settings()
    if user.name.strip().casefold() in _deepseek_user_names(settings):
        return DeepSeekClient(), settings.deepseek_conversational_model, "deepseek"
    client = anthropic.AsyncAnthropic(
        api_key=settings.anthropic_api_key.get_secret_value()
    )
    return client, settings.conversational_model, "anthropic"


async def _create_message_with_retry(
    client: Any,
    *,
    ctx: TurnContext,
    system: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    model: str | None = None,
    provider: str = "anthropic",
    max_tokens: int = 1200,
) -> Any:
    settings = get_settings()
    last_error: Exception | None = None
    for attempt in range(2):
        if not await is_under_cap(ctx.pool, "text"):
            raise SpendCapExceeded("text LLM spend cap exceeded")
        try:
            response = await client.messages.create(
                model=model or settings.conversational_model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=tools,
            )
        except Exception as exc:  # Anthropic SDK transient subclasses vary by version.
            last_error = exc
            if attempt == 0:
                logger.warning(
                    "%s message create failed; retrying once: %s",
                    provider,
                    exc,
                    extra=obs_fields(ctx),
                )
                continue
            raise LLMPhaseError(str(exc)) from exc
        if provider == "deepseek":
            input_price = settings.deepseek_input_usd_per_mtok
            output_price = settings.deepseek_output_usd_per_mtok
        else:
            input_price = settings.anthropic_input_usd_per_mtok
            output_price = settings.anthropic_output_usd_per_mtok
        await _record_response_cost(
            ctx.pool,
            _attr(response, "usage", {}),
            input_price=input_price,
            output_price=output_price,
        )
        return response
    raise LLMPhaseError(str(last_error or f"{provider} message create failed"))


async def run_step(
    client: Any,
    ctx: TurnContext,
    system_prompt: str,
    hot_context_rendered: str,
    allowed_tools: set[str],
    seed_messages: list[dict[str, Any]],
    model: str | None = None,
    provider: str = "anthropic",
    max_tokens: int = 1200,
    max_tool_iterations: int | None = None,
) -> tuple[str, list[dict[str, Any]], int]:
    settings = get_settings()
    if client is None:
        if provider == "deepseek":
            client = DeepSeekClient()
            model = model or settings.deepseek_conversational_model
        else:
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value()
            )

    system = _system_blocks(system_prompt, hot_context_rendered)
    tools = _anthropic_tools(allowed_tools)
    messages = list(seed_messages)
    tool_call_count = 0
    tool_iteration_count = 0
    consecutive_recoverable_errors = 0

    while True:
        response = await _create_message_with_retry(
            client,
            ctx=ctx,
            system=system,
            tools=tools,
            messages=messages,
            model=model,
            provider=provider,
            max_tokens=max_tokens,
        )
        content_blocks = [
            _block_to_dict(block) for block in (_attr(response, "content", []) or [])
        ]
        messages.append({"role": "assistant", "content": content_blocks})
        tool_uses = [
            block for block in content_blocks if block.get("type") == "tool_use"
        ]
        if not tool_uses or _attr(response, "stop_reason") != "tool_use":
            final_text = "\n".join(
                str(block.get("text", "")).strip()
                for block in content_blocks
                if block.get("type") == "text" and str(block.get("text", "")).strip()
            )
            return final_text, messages, tool_call_count

        tool_iteration_count += 1
        if (
            max_tool_iterations is not None
            and tool_iteration_count > max_tool_iterations
        ):
            raise BoundedLoopExceeded(
                f"tool iteration cap exceeded: {max_tool_iterations}"
            )
        tool_results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            if tool_use["name"] != "update_turn_plan":
                tool_call_count += 1
            result = await call_tool(tool_use["name"], tool_use.get("input") or {}, ctx)
            is_error = bool(result.get("is_error") or result.get("error"))
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use["id"],
                    "content": json.dumps(result, default=str),
                    "is_error": is_error,
                }
            )
        messages.append({"role": "user", "content": tool_results})

        # ── Recoverable validation-error correction cap ─────────────────
        _iter_has_recoverable = any(
            bool(tr.get("is_error"))
            for tr in tool_results
            if _tool_result_payload(tr).get("retryable") is True
        )
        if _iter_has_recoverable:
            consecutive_recoverable_errors += 1
        else:
            consecutive_recoverable_errors = 0

        if consecutive_recoverable_errors >= 2:
            _last_failed = tool_results[-1] if tool_results else {}
            _last_payload = _tool_result_payload(_last_failed)
            await record_turn_event(
                ctx.pool,
                ctx.turn_id,
                "tool.validation_cap_exceeded",
                step=ctx.current_step,
                severity="error",
                actor="tool",
                metadata={
                    "tool_name": _last_payload.get("tool_name", "unknown"),
                    "error_code": _last_payload.get("error_code"),
                    "field": _last_payload.get("field"),
                    "correction_hint": _last_payload.get("correction_hint"),
                    "consecutive_recoverable_errors": consecutive_recoverable_errors,
                },
            )
            raise BoundedLoopExceeded(
                "tool_validation_recoverable_exhausted"
            )


def _tool_result_payload(tr: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON content of a tool_result dict back into a payload dict."""
    raw = tr.get("content", "{}")
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
    return raw if isinstance(raw, dict) else {}


def set_pool(pool: Any) -> None:
    global _pool
    _pool = pool


def _trigger_charge(hot_context: Any) -> str | None:
    messages = hot_context.trigger_metadata.get("messages", [])
    for message in messages:
        charge = message.get("charge")
        if charge in {"crisis", "charged"}:
            return charge
    return messages[0].get("charge") if messages else None


def _explicit_partner_alert_requested(hot_context: Any) -> bool:
    if bool(hot_context.trigger_metadata.get("explicit_partner_alert_requested")):
        return True
    messages = hot_context.trigger_metadata.get("messages", [])
    for message in messages:
        content = str(message.get("content") or "").lower()
        if not content:
            continue
        asks_to_alert = any(
            phrase in content for phrase in ("tell", "alert", "let", "message", "ask")
        )
        names_partner = any(
            phrase in content for phrase in ("partner", "him", "her", "them")
        )
        if asks_to_alert and names_partner:
            return True
    return False


def _collect_reasoning(messages: list[dict[str, Any]], final_text: str = "") -> str:
    fragments: list[str] = []
    for message in messages:
        if message.get("role") != "assistant":
            continue
        content = message.get("content", "")
        blocks = (
            content
            if isinstance(content, list)
            else [{"type": "text", "text": content}]
        )
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                if text and text != final_text:
                    fragments.append(text)
    return "\n".join(fragments)


async def _append_reasoning(pool: Any, turn_id: UUID, note: str) -> None:
    if not note:
        return
    existing = await pool.fetchval(
        "SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id=$1", turn_id
    )
    updated = f"{existing or ''}\n{note}"
    await pool.execute(
        "UPDATE bot_turns SET reasoning=$1, reasoning_encrypted=$2 WHERE id=$3",
        updated,
        encrypt_value(updated),
        turn_id,
    )


def _extract_reaction_directive(text: str) -> tuple[str | None, str]:
    emoji: str | None = None
    kept_lines: list[str] = []
    for raw_line in text.splitlines():
        match = REACTION_DIRECTIVE_RE.match(raw_line)
        if match and emoji is None:
            emoji = match.group("emoji").strip()
            continue
        kept_lines.append(raw_line)
    return emoji, "\n".join(kept_lines).strip()


async def _react_to_triggering_message(
    pool: Any,
    user: User,
    triggering_message_ids: list[UUID],
    emoji: str,
    *,
    bot_id: str,
) -> bool:
    settings = get_settings()
    if (
        settings.messaging_provider.strip().lower() != "discord"
        or not triggering_message_ids
    ):
        return False
    row = await pool.fetchrow(
        """
        SELECT whatsapp_message_id
        FROM messages
        WHERE id=$1 AND direction='inbound' AND sender_id=$2
        """,
        triggering_message_ids[-1],
        user.id,
    )
    if row is None or not row.get("whatsapp_message_id"):
        return False
    await discord.add_reaction(
        user.phone, row["whatsapp_message_id"], emoji, bot_id=bot_id
    )
    return True


async def _check_outbound_oob(
    pool: Any,
    content: str,
    recipient_id: UUID,
    protected_owner_ids: list[UUID] | None = None,
    *,
    scope: InboundScope,
) -> dict[str, Any]:
    hook = hooks.check_oob
    if hook is None:
        return {
            "verdict": "ok",
            "reason": "OOB hook disabled",
            "suggested_rewrite": None,
            "checker_failed": False,
        }
    try:
        verdict = await hook(
            pool,
            content,
            recipient_id,
            protected_owner_ids=protected_owner_ids,
            bot_id=scope.bot_id,
            topic_id=scope.topic_id,
        )
    except TypeError:
        try:
            verdict = await hook(
                pool, content, recipient_id, protected_owner_ids=protected_owner_ids
            )
        except TypeError:
            try:
                verdict = await hook(pool, content, recipient_id)
            except TypeError:
                verdict = await hook(content, recipient_id)
    if hasattr(verdict, "model_dump"):
        verdict = verdict.model_dump(mode="json")
    verdict.setdefault("suggested_rewrite", verdict.get("rewrite"))
    verdict.setdefault("reason", "")
    verdict.setdefault("checker_failed", False)
    return verdict


async def _resolve_outbound_text(
    pool: Any,
    turn_id: UUID,
    user: User,
    content: str,
    protected_owner_ids: list[UUID] | None = None,
    *,
    scope: InboundScope,
) -> str | None:
    verdict = await _check_outbound_oob(
        pool, content, user.id, protected_owner_ids, scope=scope
    )
    if verdict["verdict"] == "ok":
        if verdict.get("checker_failed"):
            await _append_reasoning(
                pool,
                turn_id,
                f"OOB checker failed open before send: {verdict['reason']}",
            )
        return content
    if verdict["verdict"] == "block":
        await _append_reasoning(
            pool,
            turn_id,
            f"Outbound blocked before send by OOB checker: {verdict['reason']}",
        )
        return None
    suggested = (verdict.get("suggested_rewrite") or "").strip()
    if not suggested:
        await _append_reasoning(
            pool,
            turn_id,
            f"Outbound rewrite requested but no rewrite was supplied: {verdict['reason']}",
        )
        return None
    second = await _check_outbound_oob(
        pool, suggested, user.id, protected_owner_ids, scope=scope
    )
    if second["verdict"] != "ok":
        await _append_reasoning(
            pool,
            turn_id,
            f"Outbound rewrite was not sendable: first={verdict['reason']} second={second['reason']}",
        )
        return None
    await _append_reasoning(
        pool,
        turn_id,
        f"Outbound rewritten by OOB checker before send: {verdict['reason']}",
    )
    return suggested


async def _open_turn(
    pool: Any,
    triggering_message_ids: list[UUID],
    user: User,
    prompt_snapshot: str,
    model_version: str,
    system_prompt_version: str,
    *,
    bot_id: str,
    topic_id: UUID | None,
    bot_spec_version: str,
    hot_context_builder_version: str,
    tool_schema_version: str,
) -> tuple[UUID, datetime]:
    row = await pool.fetchrow(
        """
        INSERT INTO bot_turns (
            triggered_by_message_id, triggering_message_ids, user_in_context,
            system_prompt_version, model_version, prompt_snapshot, prompt_snapshot_encrypted, started_at,
            bot_id, topic_id, bot_spec_version, hot_context_builder_version, tool_schema_version
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, now(), $8, $9, $10, $11, $12)
        RETURNING id, started_at
        """,
        triggering_message_ids[0] if triggering_message_ids else None,
        triggering_message_ids,
        user.id,
        system_prompt_version,
        model_version,
        prompt_snapshot,
        encrypt_value(prompt_snapshot),
        bot_id,
        topic_id,
        bot_spec_version,
        hot_context_builder_version,
        tool_schema_version,
    )
    try:
        started_at = row["started_at"]
    except KeyError:
        started_at = datetime.now(UTC)
    return row["id"], started_at


async def _complete_turn(
    pool: Any,
    turn_id: UUID,
    started_at: datetime,
    final_output_message_id: UUID | None,
    tool_call_count: int,
    reasoning: str,
) -> None:
    duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
    existing = await pool.fetchval(
        "SELECT COALESCE(reasoning, '') FROM bot_turns WHERE id=$1", turn_id
    )
    note = f"\n{reasoning}" if reasoning else ""
    updated_reasoning = f"{existing or ''}{note}"
    await pool.execute(
        """
        UPDATE bot_turns
        SET final_output_message_id=$1,
            reasoning=$2,
            reasoning_encrypted=$3,
            completed_at=now(),
            duration_ms=$4,
            tool_call_count=$5
        WHERE id=$6
        """,
        final_output_message_id,
        updated_reasoning,
        encrypt_value(updated_reasoning),
        duration_ms,
        tool_call_count,
        turn_id,
    )
    await record_turn_event(
        pool,
        turn_id,
        "turn.completed",
        duration_ms=duration_ms,
        metadata={
            "final_output_message_id": final_output_message_id,
            "tool_call_count": tool_call_count,
        },
    )


async def _record_turn_final_output(
    pool: Any, turn_id: UUID, final_output_message_id: UUID
) -> None:
    await pool.execute(
        """
        UPDATE bot_turns
        SET final_output_message_id=$1
        WHERE id=$2
        """,
        final_output_message_id,
        turn_id,
    )


def _failure_class_for(failure_reason: str) -> str:
    """Map a failure_reason string to a durable-queue failure class.

    Categories (must match exactly for inbound-queue-hardening consumption):
    - tool_validation_recoverable_exhausted
    - tool_infra_transient
    - model_policy_or_instruction_failure
    - database_unexpected
    """
    if failure_reason == "tool_validation_recoverable_exhausted":
        return "tool_validation_recoverable_exhausted"
    if failure_reason in ("llm_timeout",):
        return "tool_infra_transient"
    if failure_reason in (
        "spend_cap",
        "newer_inbound_before_final_send",
    ):
        return "model_policy_or_instruction_failure"
    if failure_reason in ("crashed", "crashed_after_send"):
        return "database_unexpected"
    # Any unknown failure_reason → database_unexpected (safest default)
    return "database_unexpected"


async def _fail_turn(
    pool: Any,
    turn_id: UUID | None,
    failure_reason: str,
    metadata: dict | None = None,
) -> None:
    if turn_id is None:
        return
    await pool.execute(
        "UPDATE bot_turns SET failure_reason=$1 WHERE id=$2", failure_reason, turn_id
    )
    merged_metadata: dict[str, Any] = {
        "failure_reason": failure_reason,
        "failure_class": _failure_class_for(failure_reason),
    }
    if metadata:
        merged_metadata.update(metadata)
    await record_turn_event(
        pool,
        turn_id,
        "turn.failed",
        severity="error",
        metadata=merged_metadata,
    )


async def _defer_for_text_cap(
    pool: Any,
    user: User,
    message_ids: list[UUID],
    *,
    bot_id: str | None = None,
    topic_id: UUID | None = None,
) -> bool:
    if message_ids and bot_id is not None and topic_id is not None:
        await inbound_queue.defer_messages(
            pool,
            message_ids,
            bot_id=bot_id,
            topic_id=topic_id,
        )
    context_payload: dict[str, Any] = {
        "triggering_message_ids": [str(message_id) for message_id in message_ids],
        "reason": "text_spend_cap",
    }
    if bot_id is not None:
        context_payload["bot_id"] = bot_id
    if topic_id is not None:
        context_payload["topic_id"] = str(topic_id)
    row = await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status, bot_id, topic_id)
        SELECT $1, 'deferred_turn', $2, $3::jsonb, 'pending', $4, $5
        WHERE NOT EXISTS (
            SELECT 1 FROM scheduled_jobs
            WHERE user_id = $1 AND job_type = 'deferred_turn' AND status = 'pending'
        )
        RETURNING id, scheduled_for
        """,
        user.id,
        datetime.now(UTC) + timedelta(days=1),
        context_payload,
        bot_id,
        topic_id,
    )
    return row is not None


async def _newer_inbound_exists(
    pool: Any,
    user: User,
    triggering_message_ids: list[UUID],
    *,
    fallback_started_at: datetime | None = None,
    bot_id: str,
) -> bool:
    boundary = fallback_started_at
    if triggering_message_ids:
        trigger_boundary = await pool.fetchval(
            "SELECT MAX(sent_at) FROM messages WHERE id = ANY($1::uuid[])",
            triggering_message_ids,
        )
        if trigger_boundary is not None:
            boundary = trigger_boundary
    if boundary is None:
        return False
    return bool(
        await pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM messages
                WHERE direction='inbound'
                  AND sender_id=$1
                  AND sent_at > $2
                  AND NOT (id = ANY($3::uuid[]))
                  AND bot_id = $4
            )
            """,
            user.id,
            boundary,
            triggering_message_ids,
            bot_id,
        )
    )


STEP_ITERATION_CAPS = {
    "read": 6,
    "consult": 1,
    "respond": 4,
    "record": 8,
    "schedule": 4,
    "done": 0,
}


def _allowed_tools_for_step(ctx: TurnContext) -> set[str]:
    allowed = set(STEP_ALLOWED_TOOLS.get(ctx.current_step, set())) | {
        "update_turn_plan"
    }
    if ctx.current_step == "respond" and not ctx.incremental_sending_enabled:
        allowed.discard("send_message_part")
    return allowed


def _sent_summary(
    delivered_parts: list[str], assistant_text: str, reaction_emoji: str | None
) -> str:
    if delivered_parts:
        return (
            f"You actually sent {len(delivered_parts)} message"
            f"{'' if len(delivered_parts) == 1 else 's'}:\n"
            + "\n\n".join(
                f"{idx + 1}. {content}" for idx, content in enumerate(delivered_parts)
            )
        )
    return f"You sent: {f'[reaction {reaction_emoji}]' if reaction_emoji else (assistant_text or '[silence]')}"


def _build_hot_context_signals(hot_context: Any) -> dict[str, Any]:
    return {
        "recent_message_count": len(getattr(hot_context, "recent_messages", []) or []),
        "open_watch_item_count": len(
            getattr(hot_context, "open_watch_items", []) or []
        ),
        "active_oob_count": len(getattr(hot_context, "active_oob", []) or []),
    }


async def _run_agentic(
    triggering_message_ids: list[UUID],
    user: User,
    *,
    scope: InboundScope,
    trigger_metadata: dict[str, Any] | None = None,
    pool: Any | None = None,
    prompt_version: str | None = None,
    before_paced_send: BeforePacedSend | None = None,
) -> None:
    active_pool = pool or _pool
    if active_pool is not None and await system_state.is_paused(active_pool):
        return
    if active_pool is None:
        raise RuntimeError("agentic pool has not been set")

    settings = get_settings()
    bot_spec = get_bot_spec(scope.bot_id)
    primary_topic_id = scope.topic_id or await primary_topic_id_for(
        active_pool, bot_spec
    )
    selected_prompt_version = prompt_version or settings.system_prompt_version
    llm_client, conversational_model, llm_provider = _llm_client_and_model_for_user(user)
    send_typing_indicator = not bool(
        trigger_metadata and trigger_metadata.get("pacing")
    )
    turn_id: UUID | None = None
    started_at = datetime.now(UTC)
    responded_to_user = False

    # ── Pre-LLM claim gate ──────────────────────────────────────────
    # Atomically claim triggering inbound messages before doing any expensive
    # work (hot context construction, LLM calls).  If none of the triggering
    # messages are claimable, bail out immediately — no hot context, no turn,
    # no LLM, no reply.
    claimed_message_ids: list[UUID] = []
    if triggering_message_ids:
        claimed_message_ids = await inbound_queue.claim_messages_for_turn(
            active_pool,
            triggering_message_ids,
            bot_id=scope.bot_id,
            topic_id=primary_topic_id,
        )
        if not claimed_message_ids:
            logger.info(
                "_run_agentic: zero of %d triggering messages claimable "
                "bot_id=%s topic_id=%s — aborting before hot context",
                len(triggering_message_ids),
                scope.bot_id,
                str(primary_topic_id),
            )
            return

    try:
        if bot_spec.participants_shape == "solo":
            partner = None
            hot_context = await build_hot_context_solo(
                active_pool,
                user,
                triggering_message_ids,
                trigger_metadata,
                primary_topic_id=primary_topic_id,
                bot_id=scope.bot_id,
                allow_cross_topic_peek=getattr(
                    bot_spec.read_scopes, "allow_cross_topic_peek", False
                ),
            )
            rendered_hot_context = render_hot_context_solo(hot_context)
        else:
            partner = await partner_of(active_pool, user)
            hot_context = await build_hot_context(
                active_pool,
                user,
                partner,
                triggering_message_ids,
                trigger_metadata,
                primary_topic_id=primary_topic_id,
                allow_cross_topic_peek=getattr(
                    bot_spec.read_scopes, "allow_cross_topic_peek", False
                ),
                allow_cross_topic_status_injection=getattr(
                    bot_spec.read_scopes, "allow_cross_topic_status_injection", False
                ),
            )
            rendered_hot_context = render_hot_context(hot_context)
        current_user_partner_share = await get_partner_share(
            active_pool,
            user_id=user.id,
            bot_id=scope.bot_id,
        )
        partner_partner_share = (
            await get_partner_share(
                active_pool, user_id=partner.id, bot_id=scope.bot_id
            )
            if partner is not None
            else None
        )
        hot_context_current_user = getattr(hot_context, "current_user", {}) or {}
        hot_context_partner_user = getattr(hot_context, "partner_user", {}) or {}
        current_user_partner_sharing_state = hot_context_current_user.get(
            "partner_sharing_state"
        )
        partner_partner_sharing_state = (
            hot_context_partner_user.get("partner_sharing_state")
            if partner is not None
            else None
        )
        system_prompt = bot_spec.render_system_prompt(
            assistant_name=settings.assistant_name,
            user=user,
            partner=partner,
            prompt_version=selected_prompt_version,
            current_user_partner_share=current_user_partner_share,
            partner_partner_share=partner_partner_share,
            current_user_partner_sharing_state=current_user_partner_sharing_state,
            partner_partner_sharing_state=partner_partner_sharing_state,
        )
        prompt_snapshot = f"{system_prompt}\n\n{rendered_hot_context}"
        bot_spec_version = hashlib.sha1(repr(bot_spec).encode()).hexdigest()[:12]
        turn_id, started_at = await _open_turn(
            active_pool,
            triggering_message_ids,
            user,
            prompt_snapshot,
            conversational_model,
            selected_prompt_version,
            bot_id=scope.bot_id,
            topic_id=primary_topic_id,
            bot_spec_version=bot_spec_version,
            hot_context_builder_version=bot_spec.hot_context_builder_version,
            tool_schema_version=_TOOL_SCHEMA_VERSION,
        )
        # Stamp the active-processing turn on claimed inbound rows.
        if claimed_message_ids:
            await active_pool.execute(
                "UPDATE messages SET handled_by_turn_id=$1 WHERE id = ANY($2::uuid[])",
                turn_id,
                claimed_message_ids,
            )

        await record_turn_event(
            active_pool,
            turn_id,
            "turn.opened",
            metadata={
                "triggered_by_message_id": (
                    triggering_message_ids[0] if triggering_message_ids else None
                ),
                "triggering_message_count": len(triggering_message_ids),
                "user_in_context": user.id,
                "model_version": conversational_model,
                "llm_provider": llm_provider,
                "system_prompt_version": selected_prompt_version,
                "bot_id": scope.bot_id,
                "topic_id": str(primary_topic_id) if primary_topic_id else None,
                "channel_id": scope.channel_id,
                "binding_id": (
                    str(scope.binding_id) if scope.binding_id is not None else None
                ),
                "dyad_id": str(scope.dyad_id) if scope.dyad_id is not None else None,
                "transport": scope.transport,
            },
        )
        charge = _trigger_charge(hot_context)
        explicit_partner_alert_requested = _explicit_partner_alert_requested(
            hot_context
        )
        hot_context_signals = _build_hot_context_signals(hot_context)
        hot_context_signals["bot_id"] = scope.bot_id
        hot_context_signals["primary_topic_slug"] = bot_spec.primary_topic_slug
        skeleton_name = pick_default_skeleton(
            trigger_metadata=hot_context.trigger_metadata,
            charge=charge,
            hot_context_signals=hot_context_signals,
        )
        turn_plan = make_turn_plan(skeleton_name)
        ctx = TurnContext.from_scope(
            scope=scope,
            turn_id=turn_id,
            pool=active_pool,
            user=user,
            partner=partner,
            triggering_message_ids=triggering_message_ids,
            bot_spec=bot_spec,
            participants_shape=bot_spec.participants_shape,
            primary_topic_slug=bot_spec.primary_topic_slug,
            read_scopes=bot_spec.read_scopes,
            write_scopes=bot_spec.write_scopes,
            cross_topic_policy=bot_spec.cross_topic_policy,
            current_step=turn_plan.current,
            turn_plan=turn_plan,
            trigger_charge=charge,
            explicit_partner_alert_requested=explicit_partner_alert_requested,
            turn_started_at=started_at,
            incremental_sending_enabled=(
                settings.messaging_provider.strip().lower() == "discord"
                and settings.discord_multi_message_enabled
            ),
            protected_owner_ids=[user.id] if partner is None else [user.id, partner.id],
            send_typing_indicator=send_typing_indicator,
            before_paced_send=before_paced_send,
            sent_message_parts=[],
            hot_context_rendered=rendered_hot_context,
            trigger_metadata=hot_context.trigger_metadata,
        )
        seed_messages = bot_spec.build_initial_seed(
            trigger_metadata=hot_context.trigger_metadata,
            triggering_message_ids=triggering_message_ids,
            charge=charge,
            orient_header=orient_summary(
                trigger_metadata=hot_context.trigger_metadata,
                charge=charge,
                hot_context_signals=hot_context_signals,
            ),
            plan=turn_plan,
        )
        messages = seed_messages
        tool_call_count = 0
        assistant_text = ""
        respond_text = ""
        reaction_emoji: str | None = None
        sent_summary_for_record: str | None = None
        final_output_message_id: UUID | None = None
        provider_send_failed: bool = False
        reasoning_parts: list[str] = []
        delivered_parts: list[str] = []

        while turn_plan.current != "done":
            ctx.current_step = turn_plan.current
            step_started_at = datetime.now(UTC)
            await record_turn_event(
                active_pool,
                turn_id,
                "step.started",
                step=ctx.current_step,
                metadata={"skeleton_name": turn_plan.skeleton_name},
            )
            try:
                step_text, messages, step_tool_count = await run_step(
                    llm_client,
                    ctx,
                    system_prompt,
                    rendered_hot_context,
                    _allowed_tools_for_step(ctx),
                    messages,
                    model=conversational_model,
                    provider=llm_provider,
                    max_tool_iterations=STEP_ITERATION_CAPS.get(ctx.current_step, 4),
                )
            except Exception as exc:
                await record_turn_event(
                    active_pool,
                    turn_id,
                    "step.failed",
                    step=ctx.current_step,
                    severity="error",
                    duration_ms=max(
                        0,
                        int(
                            (datetime.now(UTC) - step_started_at).total_seconds() * 1000
                        ),
                    ),
                    metadata={"exception_type": type(exc).__name__},
                )
                raise
            await record_turn_event(
                active_pool,
                turn_id,
                "step.completed",
                step=ctx.current_step,
                duration_ms=max(
                    0, int((datetime.now(UTC) - step_started_at).total_seconds() * 1000)
                ),
                metadata={
                    "tool_call_count": step_tool_count,
                    "assistant_text_present": bool(step_text),
                },
            )
            tool_call_count += step_tool_count

            if ctx.current_step == "respond":
                assistant_text = step_text
                # Note: inbound messages are now marked terminal by
                # inbound_queue.complete_messages / fail_messages after the
                # turn completes (see the normal-path finally and exception
                # handler below).  The old early raw→processed UPDATE has
                # been removed (durable-inbound-queue-hardening T4).

                sent_parts = ctx.sent_message_parts or []
                final_output_message_id = (
                    sent_parts[-1]["message_id"]
                    if sent_parts
                    else final_output_message_id
                )
                responded_to_user = responded_to_user or bool(sent_parts)
                if sent_parts and assistant_text:
                    await _append_reasoning(
                        active_pool,
                        turn_id,
                        "Suppressed final respond text because send_message_part already delivered user-visible text.",
                    )
                    assistant_text = ""
                elif assistant_text:
                    assistant_text = clean_user_facing_text(assistant_text)
                    reaction_emoji, assistant_text = _extract_reaction_directive(
                        assistant_text
                    )
                    if reaction_emoji is not None:
                        if await _react_to_triggering_message(
                            active_pool,
                            user,
                            triggering_message_ids,
                            reaction_emoji,
                            bot_id=scope.bot_id,
                        ):
                            await _append_reasoning(
                                active_pool,
                                turn_id,
                                f"Reacted to triggering message with {reaction_emoji}.",
                            )
                            await claim_onboarding_welcome(active_pool, user.id)
                            responded_to_user = True
                    if assistant_text:
                        dyad_owner_ids = ctx.protected_owner_ids
                        sendable_text = await _resolve_outbound_text(
                            active_pool,
                            turn_id,
                            user,
                            assistant_text,
                            dyad_owner_ids,
                            scope=scope,
                        )
                        already_sent = [part["content"] for part in sent_parts]
                        if sendable_text is None:
                            await record_turn_event(
                                active_pool,
                                turn_id,
                                "outbound.withheld",
                                step=ctx.current_step,
                                severity="warning",
                                actor="delivery",
                                message="Final outbound was not sendable after safety checks.",
                                metadata={"reason": "safety_check"},
                            )
                        elif sendable_text and sendable_text not in already_sent:
                            if await _newer_inbound_exists(
                                active_pool,
                                user,
                                triggering_message_ids,
                                fallback_started_at=started_at,
                                bot_id=ctx.bot_id,
                            ):
                                await _append_reasoning(
                                    active_pool,
                                    turn_id,
                                    "Final outbound skipped because a newer inbound message arrived before send.",
                                )
                                await record_turn_event(
                                    active_pool,
                                    turn_id,
                                    "outbound.withheld",
                                    step=ctx.current_step,
                                    severity="warning",
                                    actor="delivery",
                                    message="Final outbound skipped because a newer inbound arrived.",
                                    metadata={"reason": "newer_inbound_before_send"},
                                )
                                assistant_text = ""
                            else:

                                async def before_final_provider_send(
                                    text: str = sendable_text,
                                ) -> None:
                                    if (
                                        before_paced_send is not None
                                        and not send_typing_indicator
                                    ):
                                        await before_paced_send(
                                            text, send_kind="final", part_index=None
                                        )
                                    if await _newer_inbound_exists(
                                        active_pool,
                                        user,
                                        triggering_message_ids,
                                        fallback_started_at=started_at,
                                        bot_id=ctx.bot_id,
                                    ):
                                        raise NewerInboundBeforeFinalSend()

                                try:
                                    send_result = await send_outbound(
                                        active_pool,
                                        user,
                                        sendable_text,
                                        bot_turn_id=turn_id,
                                        protected_owner_ids=dyad_owner_ids,
                                        send_typing_indicator=send_typing_indicator,
                                        scope=scope,
                                        before_provider_send=(
                                            before_final_provider_send
                                            if before_paced_send is not None
                                            and not send_typing_indicator
                                            else None
                                        ),
                                    )
                                    final_output_message_id = send_result["message_id"]
                                    provider_send_failed = send_result["status"] == "provider_failed"
                                    provider_visible = send_result["visible_to_user"]
                                except NewerInboundBeforeFinalSend:
                                    await _append_reasoning(
                                        active_pool,
                                        turn_id,
                                        "Final outbound skipped because a newer inbound message arrived during paced send.",
                                    )
                                    await record_turn_event(
                                        active_pool,
                                        turn_id,
                                        "outbound.withheld",
                                        step=ctx.current_step,
                                        severity="warning",
                                        actor="delivery",
                                        message="Final outbound skipped because a newer inbound arrived during paced send.",
                                        metadata={
                                            "reason": "newer_inbound_during_send"
                                        },
                                    )
                                    assistant_text = ""
                                else:
                                    if send_result["status"] == "provider_failed":
                                        if send_result["visible_to_user"]:
                                            # At least one chunk was delivered before
                                            # the failure.  Record the last successful
                                            # chunk as final output and treat the
                                            # inbound as terminal replied.
                                            await _record_turn_final_output(
                                                active_pool,
                                                turn_id,
                                                final_output_message_id,
                                            )
                                            await record_turn_event(
                                                active_pool,
                                                turn_id,
                                                "outbound.sent_partial",
                                                step=ctx.current_step,
                                                actor="delivery",
                                                metadata={
                                                    "message_id": final_output_message_id,
                                                    "send_kind": "final",
                                                    "partial_failure": True,
                                                },
                                            )
                                            await claim_onboarding_welcome(
                                                active_pool, user.id
                                            )
                                            assistant_text = sendable_text
                                            responded_to_user = True
                                        else:
                                            # No chunk was visible to the user.
                                            # Clear final_output_message_id so
                                            # _complete_turn does not record a
                                            # failed outbound row as the turn's
                                            # final output.
                                            final_output_message_id = None
                                    else:
                                        await _record_turn_final_output(
                                            active_pool, turn_id, final_output_message_id
                                        )
                                        await record_turn_event(
                                            active_pool,
                                            turn_id,
                                            "outbound.sent",
                                            step=ctx.current_step,
                                            actor="delivery",
                                            metadata={
                                                "message_id": final_output_message_id,
                                                "send_kind": "final",
                                            },
                                        )
                                        await claim_onboarding_welcome(active_pool, user.id)
                                        assistant_text = sendable_text
                                        responded_to_user = True
                        elif sendable_text:
                            assistant_text = sendable_text
                elif charge in {"charged", "crisis"}:
                    await _append_reasoning(
                        active_pool,
                        turn_id,
                        "silence; charged trigger but no justification produced",
                    )
                    logger.warning(
                        "charged/crisis trigger produced silence without model justification turn_id=%s",
                        turn_id,
                        extra=obs_fields(ctx),
                    )

                respond_text = assistant_text
                delivered_parts = [
                    part["content"] for part in (ctx.sent_message_parts or [])
                ]
                if not delivered_parts and turn_id is not None:
                    delivered_parts = await sent_contents_for_turn(active_pool, turn_id)
                sent_summary_for_record = _sent_summary(
                    delivered_parts, respond_text, reaction_emoji
                )

            if step_text:
                reasoning_parts.append(
                    _collect_reasoning(
                        messages, step_text if ctx.current_step == "respond" else ""
                    )
                )

            previous_step = ctx.current_step
            next_step = turn_plan.advance()
            if next_step != "done":
                messages.append(
                    bot_spec.build_step_transition_message(
                        plan=turn_plan,
                        sent_summary=(
                            sent_summary_for_record
                            if next_step in {"record", "schedule"}
                            else None
                        ),
                    )
                )
            if previous_step == next_step:
                raise BoundedLoopExceeded(
                    f"turn plan did not advance from step {previous_step}"
                )

        reasoning = "\n".join(part for part in reasoning_parts if part)
        executed_plan = (
            f"Executed turn plan ({turn_plan.skeleton_name}): {turn_plan.trace()}"
        )
        reasoning = "\n".join(part for part in (reasoning, executed_plan) if part)
        await _complete_turn(
            active_pool,
            turn_id,
            started_at,
            final_output_message_id,
            tool_call_count,
            reasoning,
        )

        # ── Mark claimed inbound messages as terminal ──────────────────
        if claimed_message_ids:
            if responded_to_user:
                handling_result = "replied"
            elif provider_send_failed:
                # Provider failed and no user-visible delivery occurred at all.
                # Mark as failed for retry (not terminal 'replied').
                failure_class = _failure_class_for("provider_send_failed")
                error_detail = (
                    f"provider_send_failed"
                    f" [failure_class={failure_class}, retryable=true]"
                )
                await inbound_queue.fail_messages(
                    active_pool,
                    claimed_message_ids,
                    processing_error=error_detail,
                    handled_by_turn_id=turn_id,
                    bot_id=scope.bot_id,
                    topic_id=primary_topic_id,
                )
                return
            elif assistant_text and not responded_to_user:
                # Bot produced text but it was withheld (newer inbound, OOB block)
                handling_result = "withheld_newer_inbound"
            elif not assistant_text and not responded_to_user:
                # Bot intentionally stayed silent
                handling_result = "silent"
            else:
                handling_result = "no_action"
            await inbound_queue.complete_messages(
                active_pool,
                claimed_message_ids,
                handling_result=handling_result,
                handled_by_turn_id=turn_id,
                bot_id=scope.bot_id,
                topic_id=primary_topic_id,
            )
        return

    except SpendCapExceeded:
        if turn_id is not None:
            scheduled = await _defer_for_text_cap(
                active_pool,
                user,
                triggering_message_ids,
                bot_id=scope.bot_id,
                topic_id=primary_topic_id,
            )
            final_output_message_id = None
            if scheduled:
                fallback_text = "I'm running into limits today, will catch up tomorrow."

                async def before_fallback_provider_send(
                    text: str = fallback_text,
                ) -> None:
                    if before_paced_send is not None and not send_typing_indicator:
                        await before_paced_send(
                            text, send_kind="final", part_index=None
                        )
                    if await _newer_inbound_exists(
                        active_pool,
                        user,
                        triggering_message_ids,
                        fallback_started_at=started_at,
                        bot_id=ctx.bot_id,
                    ):
                        raise NewerInboundBeforeFinalSend()

                if await _newer_inbound_exists(
                    active_pool,
                    user,
                    triggering_message_ids,
                    fallback_started_at=started_at,
                    bot_id=ctx.bot_id,
                ):
                    await _append_reasoning(
                        active_pool,
                        turn_id,
                        "Spend cap fallback skipped because a newer inbound message arrived before send.",
                    )
                else:
                    try:
                        fallback_result = await send_outbound(
                            active_pool,
                            user,
                            fallback_text,
                            bot_turn_id=turn_id,
                            send_typing_indicator=send_typing_indicator,
                            scope=scope,
                            before_provider_send=(
                                before_fallback_provider_send
                                if before_paced_send is not None
                                and not send_typing_indicator
                                else None
                            ),
                        )
                        final_output_message_id = fallback_result["message_id"]
                    except NewerInboundBeforeFinalSend:
                        await _append_reasoning(
                            active_pool,
                            turn_id,
                            "Spend cap fallback skipped because a newer inbound message arrived during paced send.",
                        )
            await _complete_turn(
                active_pool,
                turn_id,
                started_at,
                final_output_message_id,
                0,
                "Text LLM spend cap hit; deferred original trigger messages for next-day retry.",
            )
            return
        # No turn was opened before spend cap was hit — defer the messages
        # instead of failing them so they can be retried later.
        if claimed_message_ids:
            await inbound_queue.defer_messages(
                active_pool,
                claimed_message_ids,
                bot_id=scope.bot_id,
                topic_id=primary_topic_id,
            )
            logger.info(
                "SpendCapExceeded before turn opened: deferred %d claimed messages"
                " bot_id=%s topic_id=%s",
                len(claimed_message_ids),
                scope.bot_id,
                str(primary_topic_id),
            )
        return
    except Exception as exc:
        failure_reason = getattr(exc, "failure_reason", "crashed")
        # Collect structured metadata from the exception when available
        fail_metadata: dict[str, Any] | None = None
        if hasattr(exc, "result"):
            exc_result = getattr(exc, "result") or {}
            if isinstance(exc_result, dict):
                fail_metadata = {
                    k: v
                    for k in (
                        "error_code",
                        "field",
                        "retryable",
                        "correction_hint",
                        "failure_class",
                        "tool_name",
                    )
                    if (v := exc_result.get(k)) is not None
                }
        elif hasattr(exc, "__cause__") and exc.__cause__ is not None:
            # Chain: extract from cause exception
            cause = exc.__cause__
            if hasattr(cause, "result"):
                cause_result = getattr(cause, "result") or {}
                if isinstance(cause_result, dict):
                    fail_metadata = {
                        k: v
                        for k in (
                            "error_code",
                            "field",
                            "retryable",
                            "correction_hint",
                            "failure_class",
                            "tool_name",
                        )
                        if (v := cause_result.get(k)) is not None
                    }
        await _fail_turn(active_pool, turn_id, failure_reason, metadata=fail_metadata)

        # ── Mark claimed inbound messages as failed ────────────────────
        if claimed_message_ids:
            if responded_to_user:
                # A user-visible response already occurred — mark terminal
                # as 'replied' (not retryable) but still record the turn failure.
                await inbound_queue.complete_messages(
                    active_pool,
                    claimed_message_ids,
                    handling_result="replied",
                    handled_by_turn_id=turn_id,
                    bot_id=scope.bot_id,
                    topic_id=primary_topic_id,
                )
                logger.warning(
                    "agentic turn failed after outbound was sent: %s",
                    exc,
                    extra=obs_fields(ctx),
                )
                return
            else:
                # No user-visible response — mark failed for potential retry.
                failure_class = _failure_class_for(failure_reason)
                retryable = (
                    fail_metadata.get("retryable", True)
                    if fail_metadata
                    else True
                )
                exc_name = type(exc).__name__
                exc_msg = str(exc)
                if exc_msg:
                    error_detail = (
                        f"{exc_name}: {exc_msg}"
                        f" [failure_class={failure_class}, retryable={retryable}]"
                    )
                else:
                    error_detail = (
                        f"{exc_name}"
                        f" [failure_class={failure_class}, retryable={retryable}]"
                    )
                await inbound_queue.fail_messages(
                    active_pool,
                    claimed_message_ids,
                    processing_error=error_detail[:500],
                    handled_by_turn_id=turn_id,
                    bot_id=scope.bot_id,
                    topic_id=primary_topic_id,
                )
        raise


async def run_agentic_turn(
    triggering_message_ids: list[UUID], user: User, *, scope: InboundScope
) -> None:
    if not triggering_message_ids:
        logger.warning(
            "run_agentic_turn called without triggering messages for user_id=%s",
            user.id,
            extra={"user_id": str(user.id)},
        )
        return
    await _run_agentic(triggering_message_ids, user, scope=scope)


async def run_agentic_turn_with_metadata(
    triggering_message_ids: list[UUID],
    user: User,
    *,
    pacing_context: Any | None = None,
    trigger_metadata: Mapping[str, Any] | None = None,
    before_paced_send: BeforePacedSend | None = None,
    scope: InboundScope,
) -> None:
    if not triggering_message_ids:
        logger.warning(
            "run_agentic_turn_with_metadata called without triggering messages for user_id=%s",
            user.id,
            extra={"user_id": str(user.id)},
        )
        return
    await _run_agentic(
        triggering_message_ids,
        user,
        scope=scope,
        trigger_metadata=_trigger_metadata_with_pacing(
            trigger_metadata, pacing_context
        ),
        before_paced_send=before_paced_send,
    )


async def run_agentic_job(
    user: User, trigger_metadata: dict[str, Any], *, scope: InboundScope
) -> None:
    await _run_agentic([], user, scope=scope, trigger_metadata=trigger_metadata)


async def run_agentic_turn_with_pool(
    pool: Any,
    triggering_message_ids: list[UUID],
    user: User,
    *,
    scope: InboundScope,
    prompt_version: str,
) -> None:
    if not triggering_message_ids:
        logger.warning(
            "run_agentic_turn_with_pool called without triggering messages for user_id=%s",
            user.id,
            extra={"user_id": str(user.id)},
        )
        return
    await _run_agentic(
        triggering_message_ids,
        user,
        scope=scope,
        pool=pool,
        prompt_version=prompt_version,
    )


async def run_agentic_job_with_pool(
    pool: Any,
    user: User,
    trigger_metadata: dict[str, Any],
    *,
    scope: InboundScope,
    prompt_version: str,
) -> None:
    await _run_agentic(
        [],
        user,
        scope=scope,
        trigger_metadata=trigger_metadata,
        pool=pool,
        prompt_version=prompt_version,
    )
