"""Verify edit/delete/react tools no-op (return action='unsupported') when ctx.partner is None.

This guards Tante Rosi (a solo bot) from attempting to call Discord write APIs
that require a partner phone number.
"""

from __future__ import annotations

from uuid import uuid4

from app.services.tools.write_tools import (
    edit_outbound_message,
    EditOutboundMessageInput,
    EditOutboundMessageOutput,
    delete_outbound_message,
    DeleteOutboundMessageInput,
    DeleteOutboundMessageOutput,
    react_to_message,
    ReactToMessageInput,
    ReactToMessageOutput,
)
from app.services.turn_context import TurnContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_ctx(fake_pool, *, partner=None, bot_id="tante_rosi", user_phone="15555550100"):
    """Build a minimal TurnContext for solo-bot guard tests."""
    from datetime import UTC, datetime
    from app.models.user import User

    user_id = uuid4()
    fake_pool.users[user_id] = {
        "id": user_id,
        "name": "Solo User",
        "phone": user_phone,
        "timezone": "UTC",
        "onboarding_state": "pending",
        "pacing_preferences": {},
        "cross_thread_sharing_default": None,
        "pregnancy_edd": None,
        "pregnancy_dating_basis": None,
        "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None,
        "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None,
        "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    user = User(id=user_id, name="Solo User", phone=user_phone, timezone="UTC")

    return TurnContext(
        turn_id=uuid4(),
        pool=fake_pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        turn_started_at=datetime.now(UTC),
        incremental_sending_enabled=False,
        send_typing_indicator=False,
        before_paced_send=None,
        sent_message_parts=[],
        bot_id=bot_id,
        user_id=user_id,
        primary_topic_id=uuid4(),
    )


def _seed_outbound_message(fake_pool, message_id, recipient_id, whatsapp_id):
    """Seed an outbound message row for the test."""
    from datetime import UTC, datetime
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": recipient_id,
        "content": "original content",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": None,
        "whatsapp_message_id": whatsapp_id,
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
        "bot_turn_id": None,
        "outbound_part_key": None,
        "outbound_part_index": None,
    }


# ---------------------------------------------------------------------------
# edit_outbound_message solo-bot guard
# ---------------------------------------------------------------------------

class TestEditOutboundMessageSoloGuard:
    """edit_outbound_message returns action='unsupported' when ctx.partner is None."""

    async def test_no_partner_returns_unsupported(self, fake_pool):
        """Solo bot → unsupported."""
        user_id = uuid4()
        fake_pool.users[user_id] = {
            "id": user_id,
            "name": "Solo User",
            "phone": "15555550100",
            "timezone": "UTC",
            "onboarding_state": "pending",
            "pacing_preferences": {},
            "cross_thread_sharing_default": None,
        }
        message_id = uuid4()
        _seed_outbound_message(fake_pool, message_id, user_id, "discord-msg-1")

        ctx = _make_ctx(fake_pool, partner=None, bot_id="tante_rosi", user_phone="15555550100")

        result = await edit_outbound_message(
            ctx, EditOutboundMessageInput(message_id=str(message_id), content="edited", reason="test edit")
        )

        assert isinstance(result, EditOutboundMessageOutput)
        assert result.action == "unsupported"
        assert result.message_id == message_id
        assert "solo" in result.reason.lower()

    async def test_with_partner_proceeds_normally(self, fake_pool, monkeypatch):
        """With partner → not blocked by solo guard (happy path)."""
        from app.models.user import User
        from app.config import get_settings

        monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
        get_settings.cache_clear()

        user_id = uuid4()
        partner_id = uuid4()
        fake_pool.users[user_id] = {
            "id": user_id, "name": "A", "phone": "15555550100", "timezone": "UTC",
            "onboarding_state": "pending", "pacing_preferences": {},
            "cross_thread_sharing_default": None,
        }
        fake_pool.users[partner_id] = {
            "id": partner_id, "name": "B", "phone": "15555550101", "timezone": "UTC",
            "onboarding_state": "pending", "pacing_preferences": {},
            "cross_thread_sharing_default": None,
        }
        user = User(id=user_id, name="A", phone="15555550100", timezone="UTC")
        partner = User(id=partner_id, name="B", phone="15555550101", timezone="UTC")

        message_id = uuid4()
        _seed_outbound_message(fake_pool, message_id, user_id, "discord-msg-1")

        ctx = _make_ctx(fake_pool, partner=partner, bot_id="mediator", user_phone="15555550100")
        # Override with actual user
        ctx.user = user

        edit_called = []

        async def fake_edit_text(to, msg_id, content, *, bot_id):
            edit_called.append((to, msg_id, content, bot_id))

        monkeypatch.setattr("app.services.tools.write_tools.discord.edit_text", fake_edit_text)

        result = await edit_outbound_message(
            ctx, EditOutboundMessageInput(message_id=str(message_id), content="edited", reason="test partner edit")
        )

        assert isinstance(result, EditOutboundMessageOutput)
        assert result.action != "unsupported"
        assert edit_called


# ---------------------------------------------------------------------------
# delete_outbound_message solo-bot guard
# ---------------------------------------------------------------------------

class TestDeleteOutboundMessageSoloGuard:
    """delete_outbound_message returns action='unsupported' when ctx.partner is None."""

    async def test_no_partner_returns_unsupported(self, fake_pool):
        """Solo bot → unsupported."""
        user_id = uuid4()
        fake_pool.users[user_id] = {
            "id": user_id, "name": "Solo User", "phone": "15555550100", "timezone": "UTC",
            "onboarding_state": "pending", "pacing_preferences": {},
            "cross_thread_sharing_default": None,
        }
        message_id = uuid4()
        _seed_outbound_message(fake_pool, message_id, user_id, "discord-msg-1")

        ctx = _make_ctx(fake_pool, partner=None, bot_id="tante_rosi", user_phone="15555550100")

        result = await delete_outbound_message(
            ctx, DeleteOutboundMessageInput(message_id=str(message_id), reason="cleanup")
        )

        assert isinstance(result, DeleteOutboundMessageOutput)
        assert result.action == "unsupported"
        assert "solo" in result.reason.lower()


# ---------------------------------------------------------------------------
# react_to_message solo-bot guard
# ---------------------------------------------------------------------------

class TestReactToMessageSoloGuard:
    """react_to_message returns action='unsupported' when ctx.partner is None."""

    async def test_no_partner_returns_unsupported(self, fake_pool):
        """Solo bot → unsupported."""
        user_id = uuid4()
        fake_pool.users[user_id] = {
            "id": user_id, "name": "Solo User", "phone": "15555550100", "timezone": "UTC",
            "onboarding_state": "pending", "pacing_preferences": {},
            "cross_thread_sharing_default": None,
        }
        message_id = uuid4()
        _seed_outbound_message(fake_pool, message_id, user_id, "discord-msg-1")

        ctx = _make_ctx(fake_pool, partner=None, bot_id="tante_rosi", user_phone="15555550100")

        result = await react_to_message(
            ctx, ReactToMessageInput(message_id=str(message_id), emoji="👍", reason="feedback")
        )

        assert isinstance(result, ReactToMessageOutput)
        assert result.action == "unsupported"
        assert "solo" in result.reason.lower()
