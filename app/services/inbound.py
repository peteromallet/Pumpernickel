"""Inbound transport payload processing."""

import logging
import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid5, NAMESPACE_URL

from app.bots.registry import (
    get_bot_spec,
    get_relationship_topic_id,
    primary_topic_id_for,
)
from app.config import get_settings
from app.models.user import claim_onboarding_welcome, upsert_user
from app.services import routing, system_state
from app.services.charge import classify_charge
from app.services.crypto import encrypt_value
from app.services.message_embedding_lifecycle import (
    enqueue_message_embed,
    enqueue_message_embedding_drop,
    enqueue_message_reembed,
)
from app.services.messaging import send_outbound
from app.services.scope import InboundScope, InboundTransport
from app.services.scheduled_job_handlers import seed_weekly_reflections
from app.services.templates import TemplateCall
from app.services.transcription import handle_voice
from app.services.turn_context import obs_fields, partner_of
from app.services.vision import handle_image
from app.services.whitelist import is_allowed_phone

logger = logging.getLogger(__name__)


@dataclass
class InboundProcessResult:
    """Counters aggregated across all messages in a single process_inbound call."""

    inserted: int = 0
    skipped_existing: int = 0
    coalescer_enqueued: int = 0


WELCOME_MESSAGE = (
    "Hi, I'm here as a reflection and mediation assistant for the two of you. "
    "I'm not a therapist, and I'll sometimes get things wrong, so please correct me. "
    "Message me naturally; I'll help reflect, track context, and occasionally ask a clarifying question."
)
PAUSE_CONFIRMATION = "Pausing for now. Message me again when you're ready to resume."


def _parse_sent_at(message: dict[str, Any]) -> datetime:
    if "timestamp" in message:
        return datetime.fromtimestamp(int(message["timestamp"]), UTC)
    return datetime.now(UTC)


def _contact_name(value: dict[str, Any], phone: str) -> str:
    for contact in value.get("contacts", []):
        if contact.get("wa_id") == phone:
            return contact.get("profile", {}).get("name", phone)
    return phone


def _delete_target_id(value: dict[str, Any], message: dict[str, Any]) -> str | None:
    if message.get("type") != "unsupported":
        return None
    for error in value.get("errors", []):
        if error.get("code") == 131051:
            return error.get("message_id") or message.get("context", {}).get("message_id") or message.get("id")
    return None


def _edit_target_id(message: dict[str, Any]) -> str | None:
    if message.get("type") != "text":
        return None
    return message.get("context", {}).get("message_id")


async def _handle_edit(pool: Any, target_id: str, new_content: str) -> None:
    # DEBT-090: UPDATE targets only whatsapp_message_id without bot_id.
    # Accepted sprint debt per SD-006 (no broad queue semantics redesign).
    row = await pool.fetchrow(
        """
        WITH target AS (
            SELECT id, content AS old_content
            FROM messages
            WHERE whatsapp_message_id = $3
        )
        UPDATE messages
        SET edit_history = COALESCE(edit_history, '[]'::jsonb)
                || jsonb_build_array(jsonb_build_object('content', content, 'at', now())),
            content = $1,
            content_encrypted = $2,
            edited_at = now()
        FROM target
        WHERE messages.id = target.id
        RETURNING messages.id, messages.content, messages.media_analysis, target.old_content
        """,
        new_content,
        encrypt_value(new_content),
        target_id,
    )
    if row is not None and row["old_content"] != row["content"]:
        await enqueue_message_reembed(
            pool,
            message_id=row["id"],
            content=row["content"],
            media_analysis=row["media_analysis"],
        )


async def _handle_delete(pool: Any, target_id: str) -> None:
    # DEBT-090: DELETE targets only whatsapp_message_id without bot_id.
    # Accepted sprint debt per SD-006 (no broad queue semantics redesign).
    row = await pool.fetchrow(
        """
        UPDATE messages
        SET deleted_at = now()
        WHERE whatsapp_message_id = $1
        RETURNING id
        """,
        target_id,
    )
    if row is not None:
        await enqueue_message_embedding_drop(pool, message_id=row["id"])


def _reaction_sentiment(emoji: str | None) -> str:
    if emoji in {"👍", "❤️"}:
        return "positive"
    if emoji == "👎":
        return "negative"
    return "mixed"


async def _handle_reaction(
    pool: Any,
    user_id,
    reaction: dict[str, Any],
    *,
    scope: InboundScope,
) -> None:
    target_wa_id = reaction.get("message_id")
    if not target_wa_id:
        return
    target_id = await pool.fetchval(
        "SELECT id FROM messages WHERE whatsapp_message_id=$1 AND direction='outbound'",
        target_wa_id,
    )
    if target_id is None:
        logger.info("ignoring reaction for unknown outbound message_id=%s", target_wa_id,
                     extra=obs_fields(scope))
        return
    emoji = reaction.get("emoji")
    await pool.fetchrow(
        """
        INSERT INTO feedback (from_user_id, target_type, target_id, sentiment, content, source, bot_id, topic_id)
        VALUES ($1, 'message', $2, $3, $4, 'reaction', $5, $6)
        RETURNING id
        """,
        user_id,
        target_id,
        _reaction_sentiment(emoji),
        emoji,
        scope.bot_id,
        scope.topic_id,
    )


async def _control_recipients(pool: Any, user, *, scope: InboundScope) -> list[Any]:
    recipients = [user]
    bot_spec = get_bot_spec(scope.bot_id)
    if bot_spec.participants_shape == "solo":
        return recipients
    try:
        recipients.append(await partner_of(pool, user))
    except ValueError:
        logger.warning(
            "pause/resume command from user_id=%s but partner lookup did not return exactly one user",
            user.id,
            extra=obs_fields(scope),
        )
    return recipients


async def _send_pause_confirmation(pool: Any, recipients: list[Any], paused_by, *, scope: InboundScope) -> None:
    for recipient in recipients:
        await send_outbound(
            pool,
            recipient,
            PAUSE_CONFIRMATION,
            template_fallback=TemplateCall("pause_confirmation", [recipient.name, paused_by.name]),
            ignore_pause=True,
            scope=scope,
        )


async def _handle_pause_command(pool: Any, user, *, scope: InboundScope) -> None:
    await system_state.pause(pool, user.id)
    await system_state.supersede_pending_user_facing_jobs(pool)
    await _send_pause_confirmation(pool, await _control_recipients(pool, user, scope=scope), user, scope=scope)


async def _handle_resume_command(pool: Any, user) -> None:
    await system_state.resume(pool)
    await seed_weekly_reflections(pool)


async def _upsert_user_identity(
    pool: Any,
    *,
    transport: InboundTransport,
    address: str,
    user_id: UUID,
) -> None:
    try:
        await pool.execute(
            """
            INSERT INTO user_identities (transport, address, user_id, verified_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (transport, address)
            DO UPDATE SET user_id = EXCLUDED.user_id,
                          verified_at = COALESCE(user_identities.verified_at, EXCLUDED.verified_at)
            """,
            transport,
            address,
            user_id,
        )
    except Exception:
        logger.debug(
            "_upsert_user_identity: identity write failed; continuing with resolved user",
            exc_info=True,
            extra={"transport": transport, "user_id": str(user_id)},
        )


async def _resolve_scope(
    pool: Any,
    *,
    transport: InboundTransport,
    bot_id: str,
    user_id: UUID,
    channel_id: str | None,
) -> InboundScope:
    """Resolve live inbound scope under a gateway-supplied bot.

    The User row and provider identity are created before this point. The
    binding lookup is therefore keyed by the durable user id, not by a
    best-effort transport lookup that might miss first-time senders.
    """
    binding_id: UUID | None = None
    dyad_id: UUID | None = None
    topic_id: UUID | None
    try:
        bot_spec = get_bot_spec(bot_id)
        topic_id = await primary_topic_id_for(pool, bot_spec)
    except Exception as exc:
        logger.debug(
            "_resolve_scope: primary_topic_id_for failed for bot_id=%s, "
            "falling back to relationship topic: %s",
            bot_id,
            exc,
        )
        topic_id = get_relationship_topic_id()

    try:
        binding = await routing.resolve_binding(pool, bot_id=bot_id, user_id=user_id)
        if binding is not None:
            binding_id = binding.binding_id
            dyad_id = binding.dyad_id
    except Exception:
        logger.debug("_resolve_scope: resolve_binding failed — binding stays None", exc_info=True,
                     extra={"bot_id": bot_id, "topic_id": str(topic_id) if topic_id else None})

    if topic_id is None:
        raise RuntimeError(f"_resolve_scope: no topic_id available for bot_id={bot_id}")

    return InboundScope(
        bot_id=bot_id,
        transport=transport,
        user_id=user_id,
        topic_id=topic_id,
        channel_id=channel_id,
        binding_id=binding_id,
        dyad_id=dyad_id,
    )


async def _insert_message(
    pool: Any,
    user_id,
    content: str | None,
    wa_id: str,
    sent_at: datetime,
    media_type: str | None = None,
    media_url: str | None = None,
    duration: int | None = None,
    media_analysis: dict[str, Any] | None = None,
    charge: str | None = None,
    *,
    bot_id: str | None = None,
    topic_id: UUID | None = None,
):
    row = await pool.fetchrow(
        """
        INSERT INTO messages
            (direction, sender_id, content, content_encrypted, processing_state, whatsapp_message_id, sent_at,
             media_type, media_url, media_duration_seconds, media_analysis, charge, bot_id, topic_id)
        VALUES ('inbound', $1, $2, $3, 'raw', $4, $5, $6, $7, $8, $9, $10, $11, $12)
        ON CONFLICT (bot_id, whatsapp_message_id) DO NOTHING
        RETURNING id
        """,
        user_id,
        content,
        encrypt_value(content),
        wa_id,
        sent_at,
        media_type,
        media_url,
        duration,
        media_analysis,
        charge,
        bot_id,
        topic_id,
    )
    if row is not None:
        await enqueue_message_embed(
            pool,
            message_id=row["id"],
            content=content,
            media_analysis=media_analysis,
        )
    return row


async def _coalescer_add(
    coalescer: Any,
    user: Any,
    message_id: UUID,
    *,
    source: str,
    scope: InboundScope,
) -> None:
    kwargs: dict[str, Any] = {"source": source}
    try:
        parameters = inspect.signature(coalescer.add).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "scope" in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
        kwargs["scope"] = scope
    else:
        kwargs["bot_id"] = scope.bot_id
    await coalescer.add(user.id, message_id, user, **kwargs)


async def process_inbound(
    pool: Any,
    payload: dict[str, Any],
    coalescer: Any | None = None,
    *,
    transport: Literal["discord", "whatsapp"],
    bot_id: str,
    coalescer_source: str = "live",
) -> InboundProcessResult:
    result = InboundProcessResult()
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            # Process image messages first within a single inbound value so that
            # vision.handle_image populates media_analysis before any text-driven
            # coalescer add fires. This keeps a Discord/WhatsApp message that
            # bundles text + image attachments to a single agentic turn that
            # already knows about the image, instead of racing into two replies.
            messages_in_value = list(value.get("messages", []))
            messages_in_value.sort(key=lambda m: 0 if m.get("type") == "image" else 1)
            for message in messages_in_value:
                delete_target = _delete_target_id(value, message)
                if delete_target is not None:
                    await _handle_delete(pool, delete_target)
                    continue

                edit_target = _edit_target_id(message)
                if edit_target is not None:
                    await _handle_edit(pool, edit_target, message["text"]["body"])
                    continue

                phone = message["from"]
                if not is_allowed_phone(phone):
                    # obs N/A: scope unresolved before upsert
                    logger.warning("dropping non-whitelisted sender %s", phone)
                    continue

                user = await upsert_user(
                    pool,
                    _contact_name(value, phone),
                    phone,
                    get_settings().default_user_timezone,
                )
                await _upsert_user_identity(pool, transport=transport, address=phone, user_id=user.id)
                channel_id = message.get("channel_id") or value.get("channel_id")
                scope = await _resolve_scope(
                    pool,
                    transport=transport,
                    bot_id=bot_id,
                    user_id=user.id,
                    channel_id=str(channel_id) if channel_id else None,
                )
                wa_type = message["type"]
                sent_at = _parse_sent_at(message)
                wa_id = message["id"]

                if wa_type == "reaction":
                    await _handle_reaction(pool, user.id, message.get("reaction", {}), scope=scope)
                    continue

                if wa_type == "text":
                    content = message["text"]["body"]
                    if content.strip() in {"/pause", "/resume"}:
                        charge_label = "routine"
                    else:
                        charge_label = (await classify_charge(pool, content)).charge
                    row = await _insert_message(pool, user.id, content, wa_id, sent_at, charge=charge_label, bot_id=scope.bot_id, topic_id=scope.topic_id)
                    if row is not None:
                        result.inserted += 1
                    else:
                        result.skipped_existing += 1
                    if row is not None and content.strip() == "/pause":
                        await _handle_pause_command(pool, user, scope=scope)
                        continue
                    if row is not None and content.strip() == "/resume":
                        await _handle_resume_command(pool, user)
                        continue
                    if row is not None and await system_state.is_paused(pool):
                        continue
                    if row is not None and coalescer is not None and not await system_state.is_paused(pool):
                        await _coalescer_add(coalescer, user, row["id"], source=coalescer_source, scope=scope)
                        result.coalescer_enqueued += 1
                    continue

                if wa_type == "audio":
                    duration = message["audio"].get("duration")
                    row = await _insert_message(pool, user.id, None, wa_id, sent_at, "voice", duration=duration, bot_id=scope.bot_id, topic_id=scope.topic_id)
                    if row is not None:
                        result.inserted += 1
                    else:
                        result.skipped_existing += 1
                    if row is not None:
                        paused = await system_state.is_paused(pool)
                        if not paused and await claim_onboarding_welcome(pool, user.id):
                            await send_outbound(pool, user, WELCOME_MESSAGE, scope=scope)
                        await handle_voice(pool, row["id"], message["audio"]["id"], user, None if paused else coalescer, duration, scope=scope)
                    continue

                if wa_type == "image":
                    row = await _insert_message(pool, user.id, None, wa_id, sent_at, "image", bot_id=scope.bot_id, topic_id=scope.topic_id)
                    if row is not None:
                        result.inserted += 1
                    else:
                        result.skipped_existing += 1
                    if row is not None:
                        paused = await system_state.is_paused(pool)
                        if not paused and await claim_onboarding_welcome(pool, user.id):
                            await send_outbound(pool, user, WELCOME_MESSAGE, scope=scope)
                        await handle_image(pool, row["id"], message["image"]["id"], user, None if paused else coalescer, scope=scope)
                    continue

                row = await _insert_message(
                    pool,
                    user.id,
                    None,
                    wa_id,
                    sent_at,
                    "document",
                    media_analysis={"kind": wa_type},
                    bot_id=scope.bot_id,
                    topic_id=scope.topic_id,
                )
                if row is not None:
                    result.inserted += 1
                else:
                    result.skipped_existing += 1
                if row is not None and await system_state.is_paused(pool):
                    continue
                if row is not None and await claim_onboarding_welcome(pool, user.id):
                    await send_outbound(pool, user, WELCOME_MESSAGE, scope=scope)
                if row is not None and coalescer is not None and not await system_state.is_paused(pool):
                    await _coalescer_add(coalescer, user, row["id"], source=coalescer_source, scope=scope)
                    result.coalescer_enqueued += 1

    return result


def twilio_form_to_meta_payload(form: dict[str, str]) -> dict[str, Any]:
    """Convert Twilio's application/x-www-form-urlencoded webhook to our Meta-shaped ingester."""
    from_value = form.get("From", "")
    phone = from_value.removeprefix("whatsapp:")
    wa_id = form.get("MessageSid") or form.get("SmsMessageSid") or str(uuid5(NAMESPACE_URL, repr(sorted(form.items()))))
    body = form.get("Body", "")
    num_media = int(form.get("NumMedia") or "0")
    profile_name = form.get("ProfileName") or phone

    if num_media > 0:
        content_type = form.get("MediaContentType0", "")
        media_url = form.get("MediaUrl0", "")
        if content_type.startswith("image/"):
            message = {"from": phone, "id": wa_id, "timestamp": str(int(datetime.now(UTC).timestamp())), "type": "image", "image": {"id": media_url}}
        elif content_type.startswith("audio/"):
            message = {"from": phone, "id": wa_id, "timestamp": str(int(datetime.now(UTC).timestamp())), "type": "audio", "audio": {"id": media_url}}
        else:
            message = {"from": phone, "id": wa_id, "timestamp": str(int(datetime.now(UTC).timestamp())), "type": "document"}
    else:
        message = {"from": phone, "id": wa_id, "timestamp": str(int(datetime.now(UTC).timestamp())), "type": "text", "text": {"body": body}}

    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": phone, "profile": {"name": profile_name}}],
                            "messages": [message],
                        }
                    }
                ]
            }
        ]
    }
