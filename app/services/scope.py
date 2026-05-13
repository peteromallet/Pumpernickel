"""Canonical inbound identity scope.

InboundScope is the single carrier for per-event identity below transport
gateways. Durable recovery paths may not know transport/channel because those
facts are not currently stored on every row, but they must still carry real
bot, user, and topic identity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

InboundTransport = Literal["discord", "whatsapp"]


@dataclass(frozen=True, slots=True)
class InboundScope:
    bot_id: str
    transport: InboundTransport | None
    user_id: UUID
    topic_id: UUID
    channel_id: str | None
    binding_id: UUID | None
    dyad_id: UUID | None


def scope_from_message_row(row: Any) -> InboundScope:
    """Reconstruct scope from a messages row.

    The row must expose real bot_id, topic_id, and a user id. Transport,
    channel, binding, and dyad are preserved when present, otherwise left
    unknown instead of being fabricated.
    """
    return _scope_from_row(row, user_id_fields=("user_id", "sender_id", "recipient_id"), source="message")


def scope_from_bot_turn_row(row: Any) -> InboundScope:
    """Reconstruct scope from a bot_turns row.

    Callers should SELECT ``user_in_context AS user_id`` where possible. The
    helper also accepts the raw ``user_in_context`` column for recovery code
    that reads directly from bot_turns.
    """
    return _scope_from_row(row, user_id_fields=("user_id", "user_in_context"), source="bot_turn")


def scope_from_job_row(row: Any) -> InboundScope:
    """Reconstruct scope from a scheduled_jobs row."""
    return _scope_from_row(row, user_id_fields=("user_id",), source="scheduled_job")


def _scope_from_row(row: Any, *, user_id_fields: tuple[str, ...], source: str) -> InboundScope:
    context = _as_mapping(_row_value(row, "context")) or {}
    bot_id = _required(row, "bot_id", source=source, context=context)
    topic_id = _required(row, "topic_id", "primary_topic_id", source=source, context=context)
    user_id = _required(row, *user_id_fields, source=source, context=context)
    transport = _optional(row, "transport", source=source, context=context)
    channel_id = _optional(row, "channel_id", source=source, context=context)
    binding_id = _optional(row, "binding_id", source=source, context=context)
    dyad_id = _optional(row, "dyad_id", source=source, context=context)
    return InboundScope(
        bot_id=bot_id,
        transport=transport,
        user_id=user_id,
        topic_id=topic_id,
        channel_id=channel_id,
        binding_id=binding_id,
        dyad_id=dyad_id,
    )


def _required(row: Any, *fields: str, source: str, context: Mapping[str, Any]) -> Any:
    value = _optional(row, *fields, source=source, context=context)
    if value is None:
        joined = ", ".join(fields)
        raise ValueError(f"cannot build InboundScope from {source} row: missing {joined}")
    return value


def _optional(row: Any, *fields: str, source: str, context: Mapping[str, Any]) -> Any:
    del source
    for field in fields:
        value = _row_value(row, field)
        if value is not None:
            return value
        value = context.get(field)
        if value is not None:
            return value
    return None


def _row_value(row: Any, field: str) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(field)
    try:
        return row[field]
    except (KeyError, IndexError, TypeError):
        return getattr(row, field, None)


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None
