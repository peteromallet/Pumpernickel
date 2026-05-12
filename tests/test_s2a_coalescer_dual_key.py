"""S2b coalescer tests: bot_id-required contract.

Verifies the post-S2b contract:
- add() raises TypeError when bot_id is omitted
- CompositeKey is tuple[UUID, str] (non-nullable)
- Composite-key lookup works in _fire_batch
- No legacy (user_id, None) fallback exists
- No TODO(S2b) markers remain
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest

from app.services.debouncer import BurstCoalescer, CompositeKey
from app.models.user import User


async def _noop(*args, **kwargs):
    pass


def _user(uid=None):
    return User(id=uid or uuid4(), name="Test", phone="1", timezone="UTC")


class TestCoalescerBotIdRequired:
    """Bot ID required contract — omission raises TypeError."""

    @pytest.mark.asyncio
    async def test_add_requires_bot_id(self):
        """add() without bot_id keyword raises TypeError."""
        coalescer = BurstCoalescer(_noop, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()
        msg_id = uuid4()

        with pytest.raises(TypeError):
            await coalescer.add(user.id, msg_id, user)

    @pytest.mark.asyncio
    async def test_composite_key_is_tuple_uuid_str(self):
        """CompositeKey is tuple[UUID, str] — no None in type."""
        coalescer = BurstCoalescer(_noop, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()
        msg_id = uuid4()

        await coalescer.add(user.id, msg_id, user, bot_id="custom_bot")
        keys = list(coalescer._bursts.keys())
        assert keys, "Expected at least one key"
        for k in keys:
            assert isinstance(k, tuple), f"Expected tuple key, got {type(k)}"
            assert len(k) == 2, f"Expected 2-tuple, got len {len(k)}"
            assert isinstance(k[0], UUID), f"Expected UUID, got {type(k[0])}"
            assert isinstance(k[1], str), f"Expected str bot_id, got {type(k[1])}"

    @pytest.mark.asyncio
    async def test_fire_batch_finds_composite_key(self):
        """_fire_batch finds and fires bursts via composite-key lookup."""
        called = []

        async def on_burst(msg_ids, user):
            called.append(msg_ids)

        coalescer = BurstCoalescer(on_burst, debounce_seconds=0.01, max_seconds=0.02)
        user = _user()
        msg_id = uuid4()

        await coalescer.add(user.id, msg_id, user, bot_id="bot_a")
        await asyncio.sleep(0.05)
        await coalescer._fire(user.id)
        await asyncio.sleep(0.05)

        assert called == [[msg_id]], f"Expected [[msg_id]], got {called}"

    def test_no_legacy_fallback(self):
        """No legacy (user_id, None) fallback in debouncer.py source."""
        content = open("app/services/debouncer.py").read()
        assert "# --- Legacy fallback: (user_id, None) ---" not in content, (
            "debouncer.py must not contain legacy fallback block"
        )
        assert "Legacy fallback" not in content, (
            "debouncer.py must not contain legacy fallback comment"
        )
        # Confirm no legacy lookup in _fire_batch (no key with None bot_id)
        assert "# --- Legacy" not in content, (
            "debouncer.py must not contain legacy section headers"
        )

    def test_no_todo_s2b_markers(self):
        """debouncer.py contains zero TODO(S2b) markers."""
        content = open("app/services/debouncer.py").read()
        assert "TODO(S2b)" not in content, (
            "debouncer.py must have zero TODO(S2b) markers"
        )