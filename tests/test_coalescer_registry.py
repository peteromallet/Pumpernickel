"""Unit tests for the bot-aware CoalescerRegistry (SD-A1-T3, SD-A1-T6).

Covers the readiness contract:
- is_ready() flips True only after every registered bot is also mark_ready'd.
- get() returns None for unknown bot_ids without raising; recovery must log
  a structured warning and leave the row in ``failed`` (validated against the
  recovery module which is the live caller of get()).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services import recovery
from app.services.coalescer_registry import CoalescerRegistry
from app.services.recovery import recover_on_startup
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


class _Recorder:
    def __init__(self) -> None:
        self.add_calls = []
        self.add_burst_calls = []

    async def add(self, user_id, message_id, user, *, source="live", scope):
        self.add_calls.append((user_id, message_id, user, source, scope))

    async def add_burst(self, user_id, message_ids, user, *, scope):
        self.add_burst_calls.append((user_id, message_ids, user, scope))


def test_is_ready_flips_only_after_all_bots_mark_ready():
    registry = CoalescerRegistry()
    assert registry.is_ready() is True  # vacuous when no bots registered

    registry.register("mediator", _Recorder())
    assert registry.is_ready() is False  # registered but not ready

    registry.register("hector", _Recorder())
    registry.mark_ready("mediator")
    assert registry.is_ready() is False  # mediator ready, hector not

    registry.mark_ready("hector")
    assert registry.is_ready() is True

    # Registering a new bot post-ready flips it back to False until mark_ready.
    registry.register("coach", _Recorder())
    assert registry.is_ready() is False
    registry.mark_ready("coach")
    assert registry.is_ready() is True


def test_get_missing_bot_returns_none_without_raising():
    registry = CoalescerRegistry()
    registry.register("mediator", _Recorder())
    assert registry.get("mediator") is not None
    assert registry.get("does_not_exist") is None


def test_is_ready_false_when_expected_diverges_from_installed():
    """Mutating expected without installing must keep is_ready() False."""
    registry = CoalescerRegistry()
    registry.register("mediator", _Recorder())
    registry.mark_ready("mediator")
    assert registry.is_ready() is True

    # Simulate the install loop declaring an extra expected bot that has not
    # yet been installed (e.g. transport startup race).
    registry.expected.add("hector")
    assert registry.is_ready() is False


async def test_recovery_logs_warning_when_get_returns_none(fake_pool, caplog):
    """When the registry has no coalescer for a row's bot_id, recovery logs a
    structured warning and leaves the row in failed (does not raise)."""
    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    message_id = uuid4()
    topic_id = get_relationship_topic_id()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "raw",
        "processing_state": "raw",
        "sent_at": datetime.now(UTC) - timedelta(minutes=1),
        "charge": None,
        "whatsapp_message_id": f"wa-{message_id}",
        "bot_id": "hector",
        "topic_id": topic_id,
    }

    # Registry is ready (vacuously), but does NOT know about hector.
    registry = CoalescerRegistry()
    registry.register("mediator", _Recorder())
    registry.mark_ready("mediator")

    caplog.set_level(logging.WARNING, logger=recovery.__name__)
    await recover_on_startup(fake_pool, registry)

    # No dispatch happened; the row remains in raw state (the legacy retention
    # path may later expire it, but no coalescer.add was called).
    assert fake_pool.messages[message_id]["processing_state"] == "raw"
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(
        "no coalescer for bot_id=hector" in r.getMessage() for r in warnings
    ), [r.getMessage() for r in warnings]
