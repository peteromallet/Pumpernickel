from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal
from uuid import UUID

from app.models.user import User
from app.bots.registry import get_relationship_topic_id
from app.services import agentic, hooks, whatsapp
from app.services.scope import InboundScope, scope_from_message_row
from evals.capture import capture_tool_calls


@dataclass(frozen=True)
class FakeWhatsAppSend:
    kind: Literal["text", "template"]
    to: str
    payload: str | dict[str, Any]
    delivery_id: str


@dataclass(frozen=True)
class OobCheckRecord:
    content: str
    recipient_id: str
    verdict: dict[str, Any]


@dataclass(frozen=True)
class EvalTurnExecution:
    tool_calls: list[dict[str, Any]]
    whatsapp_sends: list[FakeWhatsAppSend]
    oob_checks: list[OobCheckRecord] | None = None


@asynccontextmanager
async def fake_whatsapp_sends() -> AsyncIterator[list[FakeWhatsAppSend]]:
    sends: list[FakeWhatsAppSend] = []
    original_send_text = whatsapp.send_text
    original_send_template = whatsapp.send_template

    async def send_text(to: str, body: str) -> dict[str, Any]:
        delivery_id = f"eval-text-{len(sends) + 1}"
        sends.append(FakeWhatsAppSend("text", to, body, delivery_id))
        return {"messages": [{"id": delivery_id}]}

    async def send_template(to: str, template_payload: dict[str, Any]) -> dict[str, Any]:
        delivery_id = f"eval-template-{len(sends) + 1}"
        sends.append(FakeWhatsAppSend("template", to, template_payload, delivery_id))
        return {"messages": [{"id": delivery_id}]}

    whatsapp.send_text = send_text
    whatsapp.send_template = send_template
    try:
        yield sends
    finally:
        whatsapp.send_text = original_send_text
        whatsapp.send_template = original_send_template


@asynccontextmanager
async def capture_oob_checks() -> AsyncIterator[list[OobCheckRecord]]:
    checks: list[OobCheckRecord] = []
    original_check_oob = hooks.check_oob

    async def check_oob(*args: Any, **kwargs: Any) -> dict[str, Any]:
        if len(args) == 3:
            pool, content, recipient_id = args
        elif len(args) == 2:
            pool = None
            content, recipient_id = args
        else:
            raise TypeError("check_oob expects content/recipient or pool/content/recipient")

        if original_check_oob is None:
            verdict: Any = {
                "verdict": "ok",
                "reason": "OOB hook disabled",
                "suggested_rewrite": None,
                "checker_failed": False,
            }
        else:
            try:
                if pool is None:
                    verdict = await original_check_oob(content, recipient_id, **kwargs)
                else:
                    verdict = await original_check_oob(pool, content, recipient_id, **kwargs)
            except TypeError:
                try:
                    verdict = await original_check_oob(content, recipient_id, **kwargs)
                except TypeError:
                    verdict = await original_check_oob(content, recipient_id)
        if hasattr(verdict, "model_dump"):
            verdict = verdict.model_dump(mode="json")
        verdict = dict(verdict)
        if "suggested_rewrite" not in verdict and "rewrite" in verdict:
            verdict["suggested_rewrite"] = verdict.get("rewrite")
        verdict.setdefault("checker_failed", False)
        verdict.setdefault("reason", "")
        checks.append(OobCheckRecord(str(content), str(recipient_id), verdict))
        return verdict

    hooks.check_oob = check_oob
    try:
        yield checks
    finally:
        hooks.check_oob = original_check_oob


async def run_eval_turn(
    pool: Any,
    triggering_message_ids: list[UUID],
    user: User,
    *,
    prompt_version: str,
) -> EvalTurnExecution:
    scope = await _scope_for_eval_turn(pool, triggering_message_ids, user)
    with capture_tool_calls() as transcript:
        async with capture_oob_checks() as oob_checks:
            async with fake_whatsapp_sends() as sends:
                await agentic.run_agentic_turn_with_pool(
                    pool,
                    triggering_message_ids,
                    user,
                    scope=scope,
                    prompt_version=prompt_version,
                )
    return EvalTurnExecution(tool_calls=transcript.as_json(), whatsapp_sends=list(sends), oob_checks=list(oob_checks))


async def _scope_for_eval_turn(pool: Any, triggering_message_ids: list[UUID], user: User) -> InboundScope:
    if triggering_message_ids:
        row = await pool.fetchrow(
            """
            SELECT id, sender_id AS user_id, sender_id, bot_id, topic_id, channel_id, binding_id, dyad_id
            FROM messages
            WHERE id=$1
            """,
            triggering_message_ids[0],
        )
        if row is not None and row.get("bot_id") is not None and row.get("topic_id") is not None:
            return scope_from_message_row(row)
    topic_id = get_relationship_topic_id()
    if topic_id is None:
        raise RuntimeError("eval turn requires a relationship topic id when message scope is absent")
    return InboundScope(
        bot_id="mediator",
        transport=None,
        user_id=user.id,
        topic_id=topic_id,
        channel_id=None,
        binding_id=None,
        dyad_id=None,
    )
