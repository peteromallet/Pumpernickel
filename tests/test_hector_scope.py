"""Scope-guard tests for Hector commitment/event tools.

T13: Verify _check_hector_scope rejects cross-user, cross-topic,
and wrong-bot access.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.tools.write_tools import _check_hector_scope, ToolCallRejected
from app.services.turn_context import TurnContext
from app.models.user import User


def _make_ctx(
    *,
    bot_id: str = "hector",
    primary_topic_slug: str = "fitness",
    primary_topic_id=None,
    user_id=None,
) -> TurnContext:
    """Build a minimal TurnContext for scope guard tests."""
    uid = user_id or uuid4()
    tid = primary_topic_id or uuid4()
    user = User(
        id=uid,
        name="TestUser",
        phone="+15555550100",
        timezone="UTC",
        onboarding_state="completed",
    )
    return TurnContext(
        turn_id=uuid4(),
        pool=None,
        user=user,
        partner=None,
        triggering_message_ids=[],
        bot_id=bot_id,
        user_id=uid,
        primary_topic_id=tid,
        primary_topic_slug=primary_topic_slug,
    )


class TestHectorScopeGuard:
    """_check_hector_scope must reject invalid contexts."""

    def test_hector_with_fitness_topic_passes(self):
        """Valid Hector context should not raise."""
        ctx = _make_ctx()
        _check_hector_scope(ctx)  # Should not raise

    def test_coach_bot_rejected(self):
        """Coach bot_id must be rejected."""
        ctx = _make_ctx(bot_id="coach")
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "wrong bot" in str(exc.value)

    def test_tante_rosi_bot_rejected(self):
        """Tante Rosi bot_id must be rejected."""
        ctx = _make_ctx(bot_id="tante_rosi")
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "wrong bot" in str(exc.value)

    def test_mediator_bot_rejected(self):
        """Mediator bot_id must be rejected."""
        ctx = _make_ctx(bot_id="mediator")
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "wrong bot" in str(exc.value)

    def test_wrong_topic_rejected(self):
        """Non-fitness topic must be rejected."""
        ctx = _make_ctx(primary_topic_slug="pregnancy")
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "wrong topic" in str(exc.value)

    def test_relationship_topic_rejected(self):
        """Relationship topic must be rejected."""
        ctx = _make_ctx(primary_topic_slug="relationship")
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "wrong topic" in str(exc.value)

    def test_null_bot_id_rejected(self):
        """None bot_id must be rejected."""
        ctx = _make_ctx()
        # Override bot_id to None via object.__setattr__ on frozen dataclass
        object.__setattr__(ctx, "bot_id", None)
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "missing bot_id" in str(exc.value)

    def test_null_topic_id_rejected(self):
        """None primary_topic_id must be rejected."""
        ctx = _make_ctx()
        object.__setattr__(ctx, "primary_topic_id", None)
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "missing topic_id" in str(exc.value)

    def test_null_user_id_rejected(self):
        """None user.id must be rejected."""
        ctx = _make_ctx()
        # Freeze the user's id by creating user with id=None
        user = User(
            id=None,  # type: ignore[arg-type]
            name="TestUser",
            phone="+15555550100",
            timezone="UTC",
            onboarding_state="completed",
        )
        object.__setattr__(ctx, "user", user)
        with pytest.raises(ToolCallRejected) as exc:
            _check_hector_scope(ctx)
        assert "missing user_id" in str(exc.value)
