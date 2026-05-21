"""Non-chat agentic job runner for live prep and other async agentic workflows.

This module provides a reusable runner that opens a non-chat bot_turn, executes
a single run_step with the bot's configured provider chain, and gates on a
required submit tool (submit_live_brief).  It is intentionally separate from
the chat-oriented _run_agentic and does NOT touch inbound queue lifecycle,
outbound sends, or chat-specific claiming/finalizing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.config import get_settings
from app.services.agentic import (
    BoundedLoopExceeded,
    _finalize_turn_atomically,
    _open_nonchat_turn,
    _provider_model,
    run_step,
)
from app.services.crypto import encrypt_value
from app.services.scope import InboundScope
from app.services.tools.registry import _step_allowed
from app.services.turn_audit import record_turn_event
from app.services.turn_context import TurnContext, obs_fields

logger = logging.getLogger(__name__)


@dataclass
class NonchatJobResult:
    """Outcome of a single run_agentic_nonchat_job invocation."""

    success: bool
    brief: dict[str, Any] | None
    failure_reason: str | None
    turn_id: UUID | None
    tool_call_count: int


async def run_agentic_nonchat_job(
    *,
    kind: str,
    user: Any,
    conversation_id: UUID,
    system_task: str,
    max_tool_iterations: int,
    pool: Any,
    bot_spec: Any,
    bot_id: str,
    topic_id: UUID | None,
    partner: Any | None,
    hot_context: str,
    trigger_metadata: dict[str, Any] | None = None,
) -> NonchatJobResult:
    """Run a bounded, non-chat agentic job with the selected bot's identity.

    The job opens a private ``bot_turn`` (kind=*kind*), runs a single
    ``run_step`` against the bot's *provider_chain*, and requires the model
    to call *submit_live_brief* before the tool-iteration cap is exhausted.
    Plain text without a submit, an empty output, or hitting the cap all
    produce a failure result.
    """
    settings = get_settings()
    trigger_metadata = trigger_metadata or {}

    # ── 1. Build prompt / version snapshot ──────────────────────────────
    first_hop_provider = bot_spec.provider_chain[0]
    model_version = _provider_model(first_hop_provider, None)
    system_prompt_version = getattr(bot_spec, "system_prompt_version", "1.0.0")
    prompt_snapshot = system_task

    # ── 2. Open the non-chat turn ───────────────────────────────────────
    turn_id: UUID | None = None
    started_at: datetime | None = None
    try:
        turn_id, started_at = await _open_nonchat_turn(
            pool,
            user.id,
            prompt_snapshot,
            model_version,
            system_prompt_version,
            bot_id=bot_id,
            topic_id=topic_id,
            kind=kind,
            conversation_id=conversation_id,
        )
    except Exception:
        logger.exception(
            "nonchat_job: failed to open turn kind=%s conversation_id=%s bot_id=%s",
            kind,
            conversation_id,
            bot_id,
        )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_prep_submit_missing",
            turn_id=None,
            tool_call_count=0,
        )

    # ── 3. Build TurnContext with live_prep identity ────────────────────
    ctx = TurnContext(
        turn_id=turn_id,
        pool=pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        bot_id=bot_id,
        transport=None,
        user_id=user.id,
        bot_spec=bot_spec,
        binding_id=None,
        participants_shape=getattr(bot_spec, "participants_shape", None),
        primary_topic_id=topic_id,
        primary_topic_slug=getattr(bot_spec, "primary_topic_slug", None),
        channel_id=None,
        read_scopes=getattr(bot_spec, "read_scopes", None),
        write_scopes=getattr(bot_spec, "write_scopes", None),
        cross_topic_policy=getattr(bot_spec, "cross_topic_policy", None),
        dyad_id=None,
        current_step="live_prep",
        turn_started_at=started_at,
        trigger_metadata=trigger_metadata,
    )

    # ── 4. Build allowed_tools from step-based policy ───────────────────
    allowed_tools = _step_allowed(ctx)

    # ── 5. Synthesize a minimal InboundScope for turn finalization ─────
    scope = InboundScope(
        bot_id=bot_id,
        transport=None,
        user_id=user.id,
        topic_id=topic_id or UUID("00000000-0000-0000-0000-000000000000"),
        channel_id=None,
        binding_id=None,
        dyad_id=None,
    )

    tool_call_count = 0
    try:
        # ── 6. Execute the single run_step with provider chain ──────────
        final_text, _messages, tool_call_count = await run_step(
            None,  # client — let run_step build from provider_chain
            ctx,
            system_prompt=prompt_snapshot,
            hot_context_rendered=hot_context or "",
            allowed_tools=allowed_tools,
            seed_messages=[],
            provider_chain=bot_spec.provider_chain,
            max_tool_iterations=max_tool_iterations,
        )

        # ── 7. Evaluate outcome ────────────────────────────────────────
        submitted = ctx.extras.get("submitted_live_brief")
        if submitted:
            # Success path — model called submit_live_brief
            await _finalize_turn_atomically(
                pool,
                turn_id,
                started_at,
                None,  # final_output_message_id
                tool_call_count,
                "live_prep completed",
                outcome="responded",
                scope=scope,
                primary_topic_id=topic_id,
            )
            logger.info(
                "nonchat_job: live_prep submitted successfully turn_id=%s",
                turn_id,
                extra=obs_fields(ctx),
            )
            return NonchatJobResult(
                success=True,
                brief=submitted,
                failure_reason=None,
                turn_id=turn_id,
                tool_call_count=tool_call_count,
            )

        if final_text and final_text.strip():
            # Plain text without submit — model responded but didn't call the gate
            failure_reason = "live_prep_text_no_submit"
            await _finalize_turn_atomically(
                pool,
                turn_id,
                started_at,
                None,
                tool_call_count,
                f"plain text without submit: {final_text[:200]}",
                outcome="failed",
                scope=scope,
                primary_topic_id=topic_id,
                failure_reason=failure_reason,
                failure_class="infra_bug",
                processing_error="live_prep_text_no_submit",
            )
            logger.warning(
                "nonchat_job: live_prep produced text without submit_live_brief "
                "turn_id=%s text_len=%d",
                turn_id,
                len(final_text),
                extra=obs_fields(ctx),
            )
            return NonchatJobResult(
                success=False,
                brief=None,
                failure_reason=failure_reason,
                turn_id=turn_id,
                tool_call_count=tool_call_count,
            )

        # Neither text nor submit — model stopped with no output
        failure_reason = "live_prep_submit_missing"
        await _finalize_turn_atomically(
            pool,
            turn_id,
            started_at,
            None,
            tool_call_count,
            "no text output and no submit_live_brief",
            outcome="failed",
            scope=scope,
            primary_topic_id=topic_id,
            failure_reason=failure_reason,
            failure_class="infra_bug",
            processing_error="live_prep_submit_missing",
        )
        logger.warning(
            "nonchat_job: live_prep produced no output and no submit turn_id=%s",
            turn_id,
            extra=obs_fields(ctx),
        )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason=failure_reason,
            turn_id=turn_id,
            tool_call_count=tool_call_count,
        )

    except BoundedLoopExceeded:
        # Tool cap exhausted without submit_live_brief.
        # Skip _pair_orphan_tool_uses_with_stubs — messages is local to run_step.
        failure_reason = "live_prep_submit_missing"
        await _finalize_turn_atomically(
            pool,
            turn_id,
            started_at,
            None,
            tool_call_count,
            "tool iteration cap exceeded without submit_live_brief",
            outcome="failed",
            scope=scope,
            primary_topic_id=topic_id,
            failure_reason=failure_reason,
            failure_class="infra_bug",
            processing_error="live_prep_submit_missing",
        )
        logger.warning(
            "nonchat_job: live_prep tool cap exhausted without submit turn_id=%s cap=%d",
            turn_id,
            max_tool_iterations,
            extra=obs_fields(ctx),
        )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason=failure_reason,
            turn_id=turn_id,
            tool_call_count=tool_call_count,
        )

    except Exception:
        # Broad catch — any unhandled exception during prep.
        logger.exception(
            "nonchat_job: live_prep crashed turn_id=%s", turn_id, extra=obs_fields(ctx)
        )
        # Best-effort finalization if we have a turn_id
        if turn_id is not None:
            try:
                await _finalize_turn_atomically(
                    pool,
                    turn_id,
                    started_at,
                    None,
                    tool_call_count,
                    "live_prep crashed",
                    outcome="failed",
                    scope=scope,
                    primary_topic_id=topic_id,
                    failure_reason="live_prep_submit_missing",
                    failure_class="infra_bug",
                    processing_error="crashed",
                )
            except Exception:
                logger.exception(
                    "nonchat_job: failed to finalize turn after crash turn_id=%s",
                    turn_id,
                )
        return NonchatJobResult(
            success=False,
            brief=None,
            failure_reason="live_prep_submit_missing",
            turn_id=turn_id,
            tool_call_count=tool_call_count,
        )
