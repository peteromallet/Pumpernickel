"""Recovery-v2 kill switch tests (SD-A1-T9, plan Step 8).

Asserts that engaging ``system_state.recovery_v2_kill`` short-circuits
``_recover_v2_inbound`` while ``_recover_legacy_invariants`` still runs.

Discrimination strategy (per plan T9 (f)): monkeypatch the v2-only mutator
``app.services.inbound_queue.recover_retryable_failed`` and assert call
count == 0 while a legacy retention-expiry UPDATE still fires against the
pool — the legacy SET clause stamps ``processing_state='expired',
handling_result='expired', handled_at=now()`` and is text-distinct from the
v2 retry-path SET clause (``processing_state='raw',
processing_started_at=NULL``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services import inbound_queue, recovery, system_state
from app.services.coalescer_registry import CoalescerRegistry
from app.services.recovery import recover_on_startup
from tests.conftest import FakePool

pytestmark = pytest.mark.anyio


class _Recorder:
    def __init__(self) -> None:
        self.add_calls = []
        self.add_burst_calls = []

    async def add(self, *a, **kw):
        self.add_calls.append((a, kw))

    async def add_burst(self, *a, **kw):
        self.add_burst_calls.append((a, kw))


def _ready_registry() -> CoalescerRegistry:
    registry = CoalescerRegistry()
    registry.register("mediator", _Recorder())
    registry.mark_ready("mediator")
    return registry


def _seed_user(pool: FakePool) -> User:
    user = User(id=uuid4(), name="Maya", phone="15555550100", timezone="UTC")
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    return user


def _seed_failed_message(pool: FakePool, user: User) -> None:
    """Seed a failed row that recovery-v2 would normally flip back to raw."""
    mid = uuid4()
    pool.messages[mid] = {
        "id": mid,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "boom",
        "processing_state": "failed",
        "processing_attempts": 1,
        "handling_result": "failed",
        "failure_class": "retryable_pre_send",
        "next_retry_at": None,
        "sent_at": datetime.now(UTC) - timedelta(minutes=2),
        "charge": None,
        "whatsapp_message_id": f"wa-{mid}",
        "bot_id": "mediator",
        "topic_id": get_relationship_topic_id(),
    }


def _seed_retention_expirable_raw(pool: FakePool, user: User) -> None:
    """Seed a raw row past retention so the legacy retention sweep can expire it."""
    mid = uuid4()
    pool.messages[mid] = {
        "id": mid,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "old",
        "processing_state": "raw",
        "processing_attempts": 0,
        "sent_at": datetime.now(UTC) - timedelta(days=30),
        "charge": None,
        "whatsapp_message_id": f"wa-{mid}",
        "bot_id": "mediator",
        "topic_id": get_relationship_topic_id(),
    }


async def test_kill_switch_skips_v2_but_legacy_still_runs(fake_pool, monkeypatch):
    user = _seed_user(fake_pool)
    _seed_failed_message(fake_pool, user)
    _seed_retention_expirable_raw(fake_pool, user)

    # Engage the kill switch.
    await system_state.recovery_v2_kill(fake_pool)
    assert await system_state.is_recovery_v2_killed(fake_pool) is True

    call_count = 0

    async def _spy_recover_retryable_failed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return 0

    monkeypatch.setattr(
        inbound_queue, "recover_retryable_failed", _spy_recover_retryable_failed
    )
    # Recovery.py imports inbound_queue and dereferences attributes at call
    # time, so the monkeypatch is sufficient — but patch the recovery binding
    # too to be safe against future import-time rebindings.
    monkeypatch.setattr(
        recovery.inbound_queue, "recover_retryable_failed",
        _spy_recover_retryable_failed,
    )

    expirable_raw_ids = [
        mid
        for mid, m in fake_pool.messages.items()
        if m["processing_state"] == "raw"
        and m["sent_at"] < datetime.now(UTC) - timedelta(days=7)
    ]
    assert expirable_raw_ids, "retention sweep needs a raw row past retention"

    await recover_on_startup(fake_pool, _ready_registry())

    # v2 retry-path never ran while the kill switch was engaged.
    assert call_count == 0
    # Legacy retention sweep DID run: the raw row past retention is now expired.
    for mid in expirable_raw_ids:
        assert fake_pool.messages[mid]["processing_state"] == "expired"
        assert fake_pool.messages[mid]["handling_result"] == "expired"


async def test_clear_kill_switch_re_enables_v2(fake_pool, monkeypatch):
    user = _seed_user(fake_pool)
    _seed_failed_message(fake_pool, user)

    await system_state.recovery_v2_kill(fake_pool)
    await system_state.recovery_v2_clear_kill(fake_pool)
    assert await system_state.is_recovery_v2_killed(fake_pool) is False

    call_count = 0

    async def _spy_recover_retryable_failed(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return 0

    monkeypatch.setattr(
        recovery.inbound_queue, "recover_retryable_failed",
        _spy_recover_retryable_failed,
    )

    await recover_on_startup(fake_pool, _ready_registry())
    assert call_count >= 1
