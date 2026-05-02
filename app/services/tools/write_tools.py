"""Write tools for the agentic loop."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.services.checkins import schedule_checkin_record
from app.services.crypto import encrypt_value
from app.config import get_settings
from app.services.messaging import send_outbound, _append_turn_reasoning, _call_oob_hook
from app.services import discord, scoring
from app.services.templates import TemplateCall
from app.services.turn_context import TurnContext
from tool_schemas import (
    AddMemoryInput,
    AddMemoryOutput,
    AddOOBInput,
    AddOOBOutput,
    AddWatchItemInput,
    AddWatchItemOutput,
    AddressWatchItemInput,
    AddressWatchItemOutput,
    CancelScheduledCheckinInput,
    CancelScheduledCheckinOutput,
    CreateThemeInput,
    CreateThemeOutput,
    DeleteOutboundMessageInput,
    DeleteOutboundMessageOutput,
    EditOutboundMessageInput,
    EditOutboundMessageOutput,
    EscalateToPartnerInput,
    EscalateToPartnerOutput,
    LiftOOBInput,
    LiftOOBOutput,
    LogFeedbackInput,
    LogFeedbackOutput,
    LogObservationInput,
    LogObservationOutput,
    ReactToMessageInput,
    ReactToMessageOutput,
    ScheduleCheckinInput,
    ScheduleCheckinOutput,
    SupersedeMemoryInput,
    SupersedeMemoryOutput,
    UpdateMemoryInput,
    UpdateMemoryOutput,
    UpdateOOBInput,
    UpdateOOBOutput,
    UpdateObservationInput,
    UpdateObservationOutput,
    UpdateThemeInput,
    UpdateThemeOutput,
    UpdateUserStyleNotesInput,
    UpdateUserStyleNotesOutput,
    UpdateWatchItemInput,
    UpdateWatchItemOutput,
)

logger = logging.getLogger(__name__)

SCORING_PROMPT_VERSION = scoring.SCORING_PROMPT_VERSION


class ToolCallRejected(Exception):
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__(result.get("error", "tool call rejected"))
        self.result = result


def _json_payload(value: BaseModel | dict[str, Any]) -> str:
    if isinstance(value, BaseModel):
        return value.model_dump_json()
    return json.dumps(value, default=str)


async def _log_tool_call(
    ctx: TurnContext,
    name: str,
    args: BaseModel,
    started_at: datetime,
    result: BaseModel | dict[str, Any],
) -> None:
    duration_ms = max(0, int((datetime.now(UTC) - started_at).total_seconds() * 1000))
    await ctx.pool.execute(
        """
        INSERT INTO tool_calls (turn_id, tool_name, arguments, result, called_at, duration_ms)
        VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6)
        """,
        ctx.turn_id,
        name,
        args.model_dump_json(),
        _json_payload(result),
        started_at,
        duration_ms,
    )


def _start() -> datetime:
    return datetime.now(UTC)


async def _schedule_context_job(
    pool: Any,
    *,
    user_id: Any,
    job_type: str,
    scheduled_for: datetime,
    context_key: str,
    context_id: Any,
) -> None:
    await pool.execute(
        """
        UPDATE scheduled_jobs
        SET status='superseded'
        WHERE job_type=$1
          AND status='pending'
          AND context->>$2 = $3
        """,
        job_type,
        context_key,
        str(context_id),
    )
    await pool.fetchrow(
        """
        INSERT INTO scheduled_jobs (user_id, job_type, scheduled_for, context, status)
        VALUES ($1, $2, $3, $4::jsonb, 'pending')
        RETURNING id, scheduled_for
        """,
        user_id,
        job_type,
        scheduled_for,
        json.dumps({context_key: str(context_id)}),
    )


async def update_user_style_notes(ctx: TurnContext, args: UpdateUserStyleNotesInput) -> UpdateUserStyleNotesOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        "UPDATE users SET style_notes=$1 WHERE id=$2 RETURNING id AS user_id, now() AS updated_at",
        args.notes,
        args.user_id,
    )
    result = UpdateUserStyleNotesOutput(user_id=row["user_id"], updated_at=row["updated_at"])
    await _log_tool_call(ctx, "update_user_style_notes", args, started, result)
    return result


async def add_memory(ctx: TurnContext, args: AddMemoryInput) -> AddMemoryOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO memories (about_user_id, content, content_encrypted, related_theme_ids)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        args.about_user_id,
        args.content,
        encrypt_value(args.content),
        args.related_theme_ids,
    )
    result = AddMemoryOutput(id=row["id"])
    await _log_tool_call(ctx, "add_memory", args, started, result)
    return result


async def update_memory(ctx: TurnContext, args: UpdateMemoryInput) -> UpdateMemoryOutput:
    started = _start()
    sets: list[str] = []
    params: list[Any] = []
    if args.content is not None:
        params.append(args.content)
        sets.append(f"content=${len(params)}")
        params.append(encrypt_value(args.content))
        sets.append(f"content_encrypted=${len(params)}")
    if args.related_theme_ids is not None:
        params.append(args.related_theme_ids)
        sets.append(f"related_theme_ids=${len(params)}")
    if args.status is not None:
        params.append(args.status.value)
        sets.append(f"status=${len(params)}")
    if not sets:
        sets.append("last_referenced_at=now()")
    params.append(args.memory_id)
    row = await ctx.pool.fetchrow(f"UPDATE memories SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    result = UpdateMemoryOutput(id=row["id"])
    await _log_tool_call(ctx, "update_memory", args, started, result)
    return result


async def supersede_memory(ctx: TurnContext, args: SupersedeMemoryInput) -> SupersedeMemoryOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        WITH old AS (
            UPDATE memories SET status='superseded'
            WHERE id=$1
            RETURNING id, about_user_id
        )
        INSERT INTO memories (about_user_id, content, content_encrypted, related_theme_ids, supersedes_memory_id)
        SELECT about_user_id, $2, $3, $4, id FROM old
        RETURNING id AS new_id, $1::uuid AS old_id
        """,
        args.old_memory_id,
        args.new_content,
        encrypt_value(args.new_content),
        args.related_theme_ids,
    )
    result = SupersedeMemoryOutput(new_id=row["new_id"], old_id=row["old_id"])
    await _log_tool_call(ctx, "supersede_memory", args, started, result)
    return result


async def create_theme(ctx: TurnContext, args: CreateThemeInput) -> CreateThemeOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO themes (title, description, sentiment, health, last_reinforced_at)
        VALUES ($1, $2, $3, $4, now())
        RETURNING id
        """,
        args.title,
        args.description,
        args.sentiment.value,
        args.health.value,
    )
    result = CreateThemeOutput(id=row["id"])
    await _log_tool_call(ctx, "create_theme", args, started, result)
    return result


async def update_theme(ctx: TurnContext, args: UpdateThemeInput) -> UpdateThemeOutput:
    started = _start()
    sets = ["updated_at=now()"]
    params: list[Any] = []
    for field in ("title", "description", "status", "sentiment", "health"):
        value = getattr(args, field)
        if value is not None:
            params.append(value.value if hasattr(value, "value") else value)
            sets.append(f"{field}=${len(params)}")
    if args.mark_reinforced:
        sets.append("last_reinforced_at=now()")
    params.append(args.theme_id)
    row = await ctx.pool.fetchrow(f"UPDATE themes SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    result = UpdateThemeOutput(id=row["id"])
    await _log_tool_call(ctx, "update_theme", args, started, result)
    return result


async def add_watch_item(ctx: TurnContext, args: AddWatchItemInput) -> AddWatchItemOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO watch_items (owner_user_id, content, due_at, related_theme_ids)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        args.owner_user_id,
        args.content,
        args.due_at,
        args.related_theme_ids,
    )
    if args.due_at is not None:
        await _schedule_context_job(
            ctx.pool,
            user_id=args.owner_user_id,
            job_type="watch_item_due",
            scheduled_for=args.due_at,
            context_key="watch_item_id",
            context_id=row["id"],
        )
    result = AddWatchItemOutput(id=row["id"])
    await _log_tool_call(ctx, "add_watch_item", args, started, result)
    return result


async def update_watch_item(ctx: TurnContext, args: UpdateWatchItemInput) -> UpdateWatchItemOutput:
    started = _start()
    sets: list[str] = []
    params: list[Any] = []
    for field in ("content", "due_at", "related_theme_ids"):
        value = getattr(args, field)
        if value is not None:
            params.append(value)
            sets.append(f"{field}=${len(params)}")
    if not sets:
        sets.append("content=content")
    params.append(args.watch_item_id)
    row = await ctx.pool.fetchrow(f"UPDATE watch_items SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    if args.due_at is not None:
        owner_user_id = await ctx.pool.fetchval("SELECT owner_user_id FROM watch_items WHERE id=$1", args.watch_item_id)
        await _schedule_context_job(
            ctx.pool,
            user_id=owner_user_id,
            job_type="watch_item_due",
            scheduled_for=args.due_at,
            context_key="watch_item_id",
            context_id=args.watch_item_id,
        )
    result = UpdateWatchItemOutput(id=row["id"])
    await _log_tool_call(ctx, "update_watch_item", args, started, result)
    return result


async def address_watch_item(ctx: TurnContext, args: AddressWatchItemInput) -> AddressWatchItemOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        UPDATE watch_items
        SET status='addressed', addressing_note=$1, addressed_at=now()
        WHERE id=$2
        RETURNING id, addressed_at
        """,
        args.addressing_note,
        args.watch_item_id,
    )
    result = AddressWatchItemOutput(id=row["id"], addressed_at=row["addressed_at"])
    await _log_tool_call(ctx, "address_watch_item", args, started, result)
    return result


async def log_observation(ctx: TurnContext, args: LogObservationInput) -> LogObservationOutput:
    started = _start()
    significance = args.significance
    supporting_message_ids = args.supporting_message_ids or ctx.triggering_message_ids
    logged_args = args.model_copy(update={"supporting_message_ids": supporting_message_ids})
    scoring_prompt_version = SCORING_PROMPT_VERSION
    if significance is None:
        significance, _reason, scoring_prompt_version = await scoring.score_observation(ctx.pool, content=args.content)
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO observations (
            content, content_encrypted, about_user_id, confidence, significance, scoring_prompt_version,
            related_theme_ids, supporting_message_ids, last_reinforced_at
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, now())
        RETURNING id
        """,
        args.content,
        encrypt_value(args.content),
        args.about_user_id,
        args.confidence.value,
        significance,
        scoring_prompt_version,
        args.related_theme_ids,
        supporting_message_ids,
    )
    result = LogObservationOutput(id=row["id"])
    await _log_tool_call(ctx, "log_observation", logged_args, started, result)
    return result


async def update_observation(ctx: TurnContext, args: UpdateObservationInput) -> UpdateObservationOutput:
    started = _start()
    sets = ["last_reinforced_at=now()"]
    params: list[Any] = []
    for field in ("content", "confidence", "status", "related_theme_ids"):
        value = getattr(args, field)
        if value is not None:
            params.append(value.value if hasattr(value, "value") else value)
            sets.append(f"{field}=${len(params)}")
            if field == "content":
                params.append(encrypt_value(value))
                sets.append(f"content_encrypted=${len(params)}")
    params.append(args.observation_id)
    row = await ctx.pool.fetchrow(f"UPDATE observations SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    result = UpdateObservationOutput(id=row["id"])
    await _log_tool_call(ctx, "update_observation", args, started, result)
    return result


async def add_oob(ctx: TurnContext, args: AddOOBInput) -> AddOOBOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO out_of_bounds (
            owner_id, sensitive_core, sensitive_core_encrypted, shareable_context, severity, review_at
        )
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        args.owner_id,
        args.sensitive_core,
        encrypt_value(args.sensitive_core),
        args.shareable_context,
        args.severity.value,
        args.review_at,
    )
    if args.review_at is not None:
        await _schedule_context_job(
            ctx.pool,
            user_id=args.owner_id,
            job_type="oob_review",
            scheduled_for=args.review_at,
            context_key="oob_id",
            context_id=row["id"],
        )
    result = AddOOBOutput(id=row["id"])
    await _log_tool_call(ctx, "add_oob", args, started, result)
    return result


async def update_oob(ctx: TurnContext, args: UpdateOOBInput) -> UpdateOOBOutput:
    started = _start()
    sets: list[str] = []
    params: list[Any] = []
    for field in ("sensitive_core", "shareable_context", "severity", "review_at"):
        value = getattr(args, field)
        if value is not None:
            params.append(value.value if hasattr(value, "value") else value)
            sets.append(f"{field}=${len(params)}")
            if field == "sensitive_core":
                params.append(encrypt_value(value))
                sets.append(f"sensitive_core_encrypted=${len(params)}")
    if not sets:
        sets.append("sensitive_core=sensitive_core")
    params.append(args.oob_id)
    row = await ctx.pool.fetchrow(f"UPDATE out_of_bounds SET {', '.join(sets)} WHERE id=${len(params)} RETURNING id", *params)
    if args.review_at is not None:
        owner_id = await ctx.pool.fetchval("SELECT owner_id FROM out_of_bounds WHERE id=$1", args.oob_id)
        await _schedule_context_job(
            ctx.pool,
            user_id=owner_id,
            job_type="oob_review",
            scheduled_for=args.review_at,
            context_key="oob_id",
            context_id=args.oob_id,
        )
    result = UpdateOOBOutput(id=row["id"])
    await _log_tool_call(ctx, "update_oob", args, started, result)
    return result


async def lift_oob(ctx: TurnContext, args: LiftOOBInput) -> LiftOOBOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        "UPDATE out_of_bounds SET status='lifted' WHERE id=$1 RETURNING id, now() AS lifted_at",
        args.oob_id,
    )
    result = LiftOOBOutput(id=row["id"], lifted_at=row["lifted_at"])
    await _log_tool_call(ctx, "lift_oob", args, started, result)
    return result


async def schedule_checkin(ctx: TurnContext, args: ScheduleCheckinInput) -> ScheduleCheckinOutput:
    started = _start()
    old, row = await schedule_checkin_record(
        ctx.pool,
        args.user_id,
        scheduled_for=args.when,
        context={"about_what": args.about_what, "reason": args.reason},
    )
    result = ScheduleCheckinOutput(
        job_id=row["job_id"],
        superseded_job_id=old["id"] if old is not None else None,
        scheduled_for=row["scheduled_for"],
    )
    await _log_tool_call(ctx, "schedule_checkin", args, started, result)
    return result


async def cancel_scheduled_checkin(ctx: TurnContext, args: CancelScheduledCheckinInput) -> CancelScheduledCheckinOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        UPDATE scheduled_jobs
        SET status='cancelled'
        WHERE user_id=$1 AND job_type='checkin' AND status='pending'
        RETURNING id
        """,
        args.user_id,
    )
    result = CancelScheduledCheckinOutput(
        action="cancelled" if row is not None else "noop",
        cancelled_job_id=row["id"] if row is not None else None,
    )
    await _log_tool_call(ctx, "cancel_scheduled_checkin", args, started, result)
    return result


async def escalate_to_partner(ctx: TurnContext, args: EscalateToPartnerInput) -> EscalateToPartnerOutput:
    started = _start()
    if args.from_user_id != ctx.user.id or args.to_user_id != ctx.partner.id:
        logger.warning("escalate_to_partner overriding model-supplied IDs for turn_id=%s", ctx.turn_id)
    allowed_by_crisis = ctx.trigger_charge == "crisis"
    allowed_by_explicit_request = ctx.explicit_partner_alert_requested
    if not allowed_by_crisis and not allowed_by_explicit_request:
        result = {
            "error": "escalation_rejected",
            "reason": "escalate_to_partner requires trusted crisis charge or explicit partner-alert request before sending",
        }
        await _log_tool_call(ctx, "escalate_to_partner", args, started, result)
        raise ToolCallRejected(result)
    template = TemplateCall(name="escalation", params=[ctx.partner.name, ctx.user.name, args.content])
    out_id = await send_outbound(
        ctx.pool,
        ctx.partner,
        args.content,
        template_fallback=template,
        bot_turn_id=ctx.turn_id,
        protected_owner_ids=[ctx.user.id, ctx.partner.id],
    )
    await _append_turn_reasoning(
        ctx.pool,
        ctx.turn_id,
        f"ESCALATION_SENT gate={'crisis' if allowed_by_crisis else 'explicit_partner_alert'} reason={args.reason} outbound_message_id={out_id}",
    )
    result = EscalateToPartnerOutput(action="sent", outbound_message_id=out_id, used_template=False, reason_if_deferred=None)
    await _log_tool_call(ctx, "escalate_to_partner", args, started, result)
    return result


async def _fetch_dyad_message(ctx: TurnContext, message_id: Any) -> Any | None:
    return await ctx.pool.fetchrow(
        """
        SELECT id, direction, sender_id, recipient_id, content, whatsapp_message_id, deleted_at
        FROM messages
        WHERE id=$1
          AND (
            sender_id = ANY($2::uuid[])
            OR recipient_id = ANY($2::uuid[])
          )
        """,
        message_id,
        [ctx.user.id, ctx.partner.id],
    )


async def edit_outbound_message(ctx: TurnContext, args: EditOutboundMessageInput) -> EditOutboundMessageOutput:
    started = _start()
    row = await _fetch_dyad_message(ctx, args.message_id)
    if (
        row is None
        or row["direction"] != "outbound"
        or row["recipient_id"] not in {ctx.user.id, ctx.partner.id}
        or row["whatsapp_message_id"] is None
        or row["deleted_at"] is not None
    ):
        result = EditOutboundMessageOutput(
            action="not_found",
            message_id=args.message_id,
            reason="message is not an editable, delivered bot outbound in this conversation",
        )
        await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
        return result

    if get_settings().messaging_provider.strip().lower() != "discord":
        result = EditOutboundMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            reason="editing already-sent bot messages is currently implemented only for Discord",
        )
        await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
        return result

    verdict = await _call_oob_hook(ctx.pool, args.content, row["recipient_id"], [ctx.user.id, ctx.partner.id])
    if verdict["verdict"] != "ok":
        result = EditOutboundMessageOutput(
            action="blocked",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            reason=verdict["reason"],
            suggested_rewrite=verdict.get("suggested_rewrite"),
        )
        await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
        return result

    recipient_phone = ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    await discord.edit_text(recipient_phone, row["whatsapp_message_id"], args.content)
    await ctx.pool.execute(
        """
        UPDATE messages
        SET edit_history = COALESCE(edit_history, '[]'::jsonb)
                || jsonb_build_array(jsonb_build_object('content', content, 'at', now(), 'reason', $1)),
            content = $2,
            content_encrypted = $3,
            edited_at = now()
        WHERE id = $4
        """,
        args.reason,
        args.content,
        encrypt_value(args.content),
        args.message_id,
    )
    result = EditOutboundMessageOutput(
        action="edited",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        reason=args.reason,
    )
    await _log_tool_call(ctx, "edit_outbound_message", args, started, result)
    return result


async def delete_outbound_message(ctx: TurnContext, args: DeleteOutboundMessageInput) -> DeleteOutboundMessageOutput:
    started = _start()
    row = await _fetch_dyad_message(ctx, args.message_id)
    if (
        row is None
        or row["direction"] != "outbound"
        or row["recipient_id"] not in {ctx.user.id, ctx.partner.id}
        or row["whatsapp_message_id"] is None
        or row["deleted_at"] is not None
    ):
        result = DeleteOutboundMessageOutput(
            action="not_found",
            message_id=args.message_id,
            reason="message is not a deletable, delivered bot outbound in this conversation",
        )
        await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
        return result

    if get_settings().messaging_provider.strip().lower() != "discord":
        result = DeleteOutboundMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            reason="deleting already-sent bot messages is currently implemented only for Discord",
        )
        await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
        return result

    recipient_phone = ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    await discord.delete_text(recipient_phone, row["whatsapp_message_id"])
    await ctx.pool.execute(
        "UPDATE messages SET deleted_at = now(), processing_state='expired' WHERE id=$1",
        args.message_id,
    )
    result = DeleteOutboundMessageOutput(
        action="deleted",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        reason=args.reason,
    )
    await _log_tool_call(ctx, "delete_outbound_message", args, started, result)
    return result


async def react_to_message(ctx: TurnContext, args: ReactToMessageInput) -> ReactToMessageOutput:
    started = _start()
    row = await _fetch_dyad_message(ctx, args.message_id)
    if row is None or row["whatsapp_message_id"] is None or row["deleted_at"] is not None:
        result = ReactToMessageOutput(
            action="not_found",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"] if row is not None else None,
            emoji=args.emoji,
            reason="message is not a delivered, visible message in this conversation",
        )
        await _log_tool_call(ctx, "react_to_message", args, started, result)
        return result

    if get_settings().messaging_provider.strip().lower() != "discord":
        result = ReactToMessageOutput(
            action="unsupported",
            message_id=args.message_id,
            provider_message_id=row["whatsapp_message_id"],
            emoji=args.emoji,
            reason="bot reactions are currently implemented only for Discord",
        )
        await _log_tool_call(ctx, "react_to_message", args, started, result)
        return result

    if row["direction"] == "inbound":
        target_phone = ctx.user.phone if row["sender_id"] == ctx.user.id else ctx.partner.phone
    else:
        target_phone = ctx.user.phone if row["recipient_id"] == ctx.user.id else ctx.partner.phone
    await discord.add_reaction(target_phone, row["whatsapp_message_id"], args.emoji)
    result = ReactToMessageOutput(
        action="reacted",
        message_id=args.message_id,
        provider_message_id=row["whatsapp_message_id"],
        emoji=args.emoji,
        reason=args.reason,
    )
    await _log_tool_call(ctx, "react_to_message", args, started, result)
    return result


async def log_feedback(ctx: TurnContext, args: LogFeedbackInput) -> LogFeedbackOutput:
    started = _start()
    row = await ctx.pool.fetchrow(
        """
        INSERT INTO feedback (from_user_id, target_type, target_id, sentiment, content, source)
        VALUES ($1, $2, $3, $4, $5, $6)
        RETURNING id
        """,
        args.from_user_id,
        args.target_type,
        args.target_id,
        args.sentiment.value,
        args.content,
        args.source,
    )
    result = LogFeedbackOutput(id=row["id"])
    await _log_tool_call(ctx, "log_feedback", args, started, result)
    return result
