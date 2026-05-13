from __future__ import annotations

from uuid import uuid4

import pytest

from app.models.user import User
from app.services.scope import (
    InboundScope,
    scope_from_bot_turn_row,
    scope_from_job_row,
    scope_from_message_row,
)
from app.services.turn_context import TurnContext, obs_fields


def test_scope_from_message_row_uses_real_identity_without_fabricating_transport() -> None:
    user_id = uuid4()
    topic_id = uuid4()

    scope = scope_from_message_row(
        {
            "sender_id": user_id,
            "bot_id": "tante_rosi",
            "topic_id": topic_id,
            "binding_id": uuid4(),
            "dyad_id": None,
        }
    )

    assert scope == InboundScope(
        bot_id="tante_rosi",
        transport=None,
        user_id=user_id,
        topic_id=topic_id,
        channel_id=None,
        binding_id=scope.binding_id,
        dyad_id=None,
    )


def test_scope_from_message_row_does_not_infer_transport_from_provider_message_id() -> None:
    """Discord recovery rows also use whatsapp_message_id as provider id.

    Regression guard: reconstruction must not infer WhatsApp transport just
    because a durable provider-message id exists.
    """
    user_id = uuid4()
    topic_id = uuid4()

    scope = scope_from_message_row(
        {
            "sender_id": user_id,
            "bot_id": "tante_rosi",
            "topic_id": topic_id,
            "whatsapp_message_id": "discord-message-123",
        }
    )

    assert scope.transport is None
    assert scope.channel_id is None
    assert scope.bot_id == "tante_rosi"
    assert scope.topic_id == topic_id


def test_scope_from_bot_turn_row_accepts_user_in_context_alias() -> None:
    user_id = uuid4()
    topic_id = uuid4()

    scope = scope_from_bot_turn_row(
        {
            "user_in_context": user_id,
            "bot_id": "mediator",
            "topic_id": topic_id,
            "context": {"transport": "discord", "channel_id": "dm-123"},
        }
    )

    assert scope.user_id == user_id
    assert scope.topic_id == topic_id
    assert scope.transport == "discord"
    assert scope.channel_id == "dm-123"


def test_scope_from_job_row_requires_bot_topic_and_user() -> None:
    with pytest.raises(ValueError, match="missing topic_id"):
        scope_from_job_row({"user_id": uuid4(), "bot_id": "mediator"})


def test_scope_reconstruction_fails_clearly_for_ambiguous_rows() -> None:
    with pytest.raises(ValueError, match="bot_turn row: missing user_id, user_in_context"):
        scope_from_bot_turn_row({"bot_id": "tante_rosi", "topic_id": uuid4()})

    with pytest.raises(ValueError, match="scheduled_job row: missing bot_id"):
        scope_from_job_row({"user_id": uuid4(), "topic_id": uuid4()})


def test_turn_context_from_scope_allows_solo_partner_and_obs_identity() -> None:
    user = User(id=uuid4(), name="Rosi User", phone="u", timezone="UTC")
    topic_id = uuid4()
    scope = InboundScope(
        bot_id="tante_rosi",
        transport="discord",
        user_id=user.id,
        topic_id=topic_id,
        channel_id="dm-rosi",
        binding_id=uuid4(),
        dyad_id=None,
    )

    ctx = TurnContext.from_scope(
        scope=scope,
        turn_id=uuid4(),
        pool=None,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        participants_shape="solo",
    )

    assert ctx.partner is None
    assert ctx.bot_id == "tante_rosi"
    assert ctx.transport == "discord"
    assert ctx.user_id == user.id
    assert ctx.primary_topic_id == topic_id
    extra = obs_fields(ctx)
    assert extra["bot_id"] == "tante_rosi"
    assert extra["transport"] == "discord"
    assert extra["user_id"] == str(user.id)
    assert extra["topic_id"] == str(topic_id)
    assert extra["channel_id"] == "dm-rosi"
    assert extra["binding_id"] == str(scope.binding_id)
    assert "dyad_id" not in extra
