from __future__ import annotations

from types import MappingProxyType, SimpleNamespace
from uuid import uuid4

import pytest

from app.config import get_settings
from app.main import _install_bot_coalescer
from app.models.user import User
from app.services.coalescer_registry import CoalescerRegistry
from app.services.pacer import DiscordPacer, PacingDecision
from tests._scope_helpers import make_resolved_scope


def _fresh_app() -> SimpleNamespace:
    """Build a SimpleNamespace mimicking the lifespan's app.state shape.

    Constructs a real CoalescerRegistry plus a read-only MappingProxyType
    over its ``installed`` dict so reads via ``app.state.coalescers[bot_id]``
    continue to work in existing assertions.
    """
    registry = CoalescerRegistry()
    return SimpleNamespace(
        state=SimpleNamespace(
            coalescer_registry=registry,
            coalescers=MappingProxyType(registry.installed),
            discord_pacers={},
        )
    )

pytestmark = pytest.mark.anyio


def _settings(monkeypatch, *, provider: str, pacing_enabled: bool = True):
    monkeypatch.setenv("MESSAGING_PROVIDER", provider)
    monkeypatch.setenv("DISCORD_PACING_ENABLED", "true" if pacing_enabled else "false")
    get_settings.cache_clear()
    return get_settings()


def test_configure_coalescer_keeps_legacy_path_for_non_discord(fake_pool, app_env, monkeypatch):
    app = _fresh_app()
    settings = _settings(monkeypatch, provider="meta")

    _install_bot_coalescer(app, fake_pool, settings, bot_id="mediator")

    assert app.state.discord_pacers == {}
    coalescer = app.state.coalescers["mediator"]
    assert coalescer.pacer is None
    assert coalescer.on_paced_answer is None
    assert coalescer.on_paced_reaction is None


def test_configure_coalescer_keeps_legacy_path_when_discord_pacing_disabled(fake_pool, app_env, monkeypatch):
    app = _fresh_app()
    settings = _settings(monkeypatch, provider="discord", pacing_enabled=False)

    _install_bot_coalescer(app, fake_pool, settings, bot_id="mediator")

    assert app.state.discord_pacers == {}
    coalescer = app.state.coalescers["mediator"]
    assert coalescer.pacer is None
    assert coalescer.on_paced_answer is None
    assert coalescer.on_paced_reaction is None


async def test_configure_coalescer_attaches_discord_pacer_and_paced_callbacks(
    fake_pool,
    app_env,
    monkeypatch,
):
    from app import main

    app = _fresh_app()
    settings = _settings(monkeypatch, provider="discord")

    answer_calls = []
    reaction_calls = []
    typing_calls = []
    thinking_calls = []

    async def fake_run_agentic_turn_with_metadata(
        message_ids,
        user,
        *,
        pacing_context=None,
        trigger_metadata=None,
        before_paced_send=None,
        scope=None,
    ):
        answer_calls.append((message_ids, user, pacing_context, trigger_metadata, before_paced_send))
        if before_paced_send is not None:
            await before_paced_send("human-paced answer")

    async def fake_add_reaction(to, message_id, emoji, *, bot_id="mediator"):
        reaction_calls.append((to, message_id, emoji))

    async def fake_send_typing(channel_id, *, bot_id="mediator"):
        return None

    async def fake_get_dm_channel_id(to, *, bot_id="mediator"):
        assert to == "15555550100"
        return "channel-1"

    async def fake_perform_send_typing(user, channel_id, answer_text, *, send_kind="final", part_index=None):
        typing_calls.append((user.id, channel_id, answer_text, send_kind, part_index))
        return 1.0

    async def fake_perform_thinking_typing_until_stopped(user, channel_id, stop_event):
        thinking_calls.append((user.id, channel_id, stop_event.is_set()))
        await stop_event.wait()

    monkeypatch.setattr(main, "run_agentic_turn_with_metadata", fake_run_agentic_turn_with_metadata)
    monkeypatch.setattr(main.discord, "add_reaction", fake_add_reaction)
    monkeypatch.setattr(main.discord, "send_typing", fake_send_typing)
    monkeypatch.setattr(main.discord, "get_dm_channel_id", fake_get_dm_channel_id)

    _install_bot_coalescer(app, fake_pool, settings, bot_id="mediator")

    mediator_pacer = app.state.discord_pacers["mediator"]
    coalescer = app.state.coalescers["mediator"]
    assert isinstance(mediator_pacer, DiscordPacer)
    assert coalescer.pacer is mediator_pacer
    assert coalescer.debounce_seconds == settings.discord_pacing_burst_window_s
    assert coalescer.on_paced_answer is not None
    assert coalescer.on_paced_reaction is not None
    assert coalescer.on_live_typing is not None
    monkeypatch.setattr(mediator_pacer, "perform_send_typing", fake_perform_send_typing)
    monkeypatch.setattr(
        mediator_pacer,
        "perform_thinking_typing_until_stopped",
        fake_perform_thinking_typing_until_stopped,
    )

    user = User(uuid4(), "Maya", "15555550100", "UTC")
    scope = make_resolved_scope(user_id=user.id)
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "got it",
        "processing_state": "raw",
        "whatsapp_message_id": "discord-message-1",
    }
    decision = PacingDecision(action="answer", reason="ready", signal_snapshot={"source": "live"})

    await coalescer.on_paced_answer([message_id], user, decision, scope=scope)
    assert len(answer_calls) == 1
    assert answer_calls[0][:4] == ([message_id], user, decision, None)
    assert answer_calls[0][4] is not None
    assert thinking_calls == [(user.id, "channel-1", False)]
    assert typing_calls == [(user.id, "channel-1", "human-paced answer", "final", None)]

    reaction_decision = PacingDecision(action="react", reason="ack", reaction="👍")
    await coalescer.on_paced_reaction([message_id], user, reaction_decision, scope=scope)
    assert reaction_calls == [("15555550100", "discord-message-1", "👍")]
