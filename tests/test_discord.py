import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from app.config import get_settings
from app.services.discord import (
    DiscordClient,
    DiscordGatewayBot,
    add_reaction,
    catch_up_recent_messages,
    is_allowed_discord_user,
    message_to_meta_payload,
    register_client,
    seed_partner_users,
    send_text,
)
from app.services.pacer import DiscordPacer


def _make_test_client(bot_id: str = "mediator", bot_user_id: str = "123456789") -> DiscordClient:
    """Create a minimal DiscordClient stub for gateway tests."""
    client = DiscordClient.__new__(DiscordClient)
    client.bot_id = bot_id
    client._token = "test.token.here"
    client._http = httpx.AsyncClient()
    client.bot_user_id = bot_user_id
    return client


class _MockRest:
    """Mock DiscordRestClient for capturing REST request calls."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return _MockResponse()

    async def send_message(self, channel_id, *, content):
        self.calls.append(("POST", f"/channels/{channel_id}/messages", {"content": content}))
        return _MockResponse()

    async def send_typing(self, channel_id):
        self.calls.append(("POST", f"/channels/{channel_id}/typing", {}))
        return _MockResponse()

    async def edit_message(self, channel_id, message_id, *, content):
        self.calls.append(("PATCH", f"/channels/{channel_id}/messages/{message_id}", {"content": content}))
        return _MockResponse()

    async def delete_message(self, channel_id, message_id):
        self.calls.append(("DELETE", f"/channels/{channel_id}/messages/{message_id}", {}))
        return _MockResponse()

    async def fetch_channel_messages(self, channel_id, **params):
        self.calls.append(("GET", f"/channels/{channel_id}/messages", params))
        return _MockResponse()


class _MockResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "discord-message-1"}


def test_discord_message_to_meta_payload() -> None:
    payload = message_to_meta_payload(
        {
            "id": "123",
            "content": "hello",
            "timestamp": "2026-04-30T20:00:00.000000+00:00",
            "author": {"id": "456", "username": "maya", "global_name": "Maya"},
        }
    )

    value = payload["entry"][0]["changes"][0]["value"]
    assert value["contacts"][0]["wa_id"] == "456"
    assert value["contacts"][0]["profile"]["name"] == "Maya"
    assert value["messages"][0]["from"] == "456"
    assert value["messages"][0]["id"] == "123"
    assert value["messages"][0]["type"] == "text"
    assert value["messages"][0]["text"]["body"] == "hello"


def test_discord_message_to_meta_payload_uses_configured_name(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    get_settings.cache_clear()

    payload = message_to_meta_payload(
        {
            "id": "123",
            "content": "hello",
            "author": {"id": "456", "username": "pom", "global_name": None},
        }
    )

    value = payload["entry"][0]["changes"][0]["value"]
    assert value["contacts"][0]["profile"]["name"] == "Partner A"
    get_settings.cache_clear()


def test_discord_allowlist_uses_discord_partner_ids(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "789")
    get_settings.cache_clear()

    assert is_allowed_discord_user("456")
    assert is_allowed_discord_user("789")
    assert not is_allowed_discord_user("999")

    get_settings.cache_clear()


async def test_discord_gateway_drops_non_partner(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "789")
    get_settings.cache_clear()
    calls = []

    async def process_inbound(pool, payload, coalescer=None, **kwargs):
        calls.append(payload)

    monkeypatch.setattr("app.services.inbound.process_inbound", process_inbound)
    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, None)
    await bot._handle_message(
        {
            "id": "123",
            "content": "hello",
            "channel_id": "channel-1",
            "author": {"id": "999", "username": "stranger"},
        }
    )

    assert calls == []
    get_settings.cache_clear()


def test_discord_message_to_meta_payload_emits_image_for_attachment() -> None:
    payload = message_to_meta_payload(
        {
            "id": "123",
            "content": "look at this",
            "author": {"id": "456", "username": "maya"},
            "attachments": [
                {
                    "id": "att1",
                    "url": "https://cdn.discordapp.com/attachments/1/2/x.png",
                    "content_type": "image/png",
                    "filename": "x.png",
                }
            ],
        }
    )

    messages = payload["entry"][0]["changes"][0]["value"]["messages"]
    assert [m["type"] for m in messages] == ["text", "image"]
    assert messages[0]["text"]["body"] == "look at this"
    assert messages[1]["id"] == "123:att1"
    assert messages[1]["image"]["id"] == "https://cdn.discordapp.com/attachments/1/2/x.png"


def test_discord_message_to_meta_payload_emits_audio_for_attachment() -> None:
    payload = message_to_meta_payload(
        {
            "id": "123",
            "content": "",
            "author": {"id": "456", "username": "maya"},
            "attachments": [
                {
                    "id": "voice1",
                    "url": "https://cdn.discordapp.com/attachments/1/2/voice-message.ogg",
                    "content_type": "audio/ogg",
                    "filename": "voice-message.ogg",
                    "duration_secs": 7.4,
                }
            ],
        }
    )

    messages = payload["entry"][0]["changes"][0]["value"]["messages"]
    assert len(messages) == 1
    assert messages[0]["id"] == "123:voice1"
    assert messages[0]["type"] == "audio"
    assert messages[0]["audio"]["id"] == "https://cdn.discordapp.com/attachments/1/2/voice-message.ogg"
    assert messages[0]["audio"]["duration"] == 7


def test_discord_message_to_meta_payload_skips_unsupported_attachments() -> None:
    payload = message_to_meta_payload(
        {
            "id": "123",
            "content": "",
            "author": {"id": "456", "username": "maya"},
            "attachments": [
                {"id": "att1", "url": "https://cdn.discordapp.com/x.pdf", "content_type": "application/pdf", "filename": "x.pdf"}
            ],
        }
    )

    messages = payload["entry"][0]["changes"][0]["value"]["messages"]
    assert len(messages) == 1
    assert messages[0]["type"] == "text"
    assert messages[0]["text"]["body"] == ""


async def test_discord_gateway_processes_image_only_message(
    fake_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    get_settings.cache_clear()
    calls = []

    async def process_inbound(pool, payload, coalescer=None, **kwargs):
        calls.append(payload)

    monkeypatch.setattr("app.services.inbound.process_inbound", process_inbound)
    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, None)
    await bot._handle_message(
        {
            "id": "123",
            "content": "",
            "channel_id": "channel-1",
            "author": {"id": "456", "username": "maya"},
            "attachments": [
                {
                    "id": "att1",
                    "url": "https://cdn.discordapp.com/x.jpg",
                    "content_type": "image/jpeg",
                    "filename": "x.jpg",
                }
            ],
        }
    )

    assert len(calls) == 1
    messages = calls[0]["entry"][0]["changes"][0]["value"]["messages"]
    assert messages[0]["type"] == "image"
    get_settings.cache_clear()


async def test_discord_gateway_processes_audio_only_message(
    fake_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    get_settings.cache_clear()
    calls = []

    async def process_inbound(pool, payload, coalescer=None, **kwargs):
        calls.append(payload)

    monkeypatch.setattr("app.services.inbound.process_inbound", process_inbound)
    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, None)
    await bot._handle_message(
        {
            "id": "123",
            "content": "",
            "channel_id": "channel-1",
            "author": {"id": "456", "username": "maya"},
            "attachments": [
                {
                    "id": "voice1",
                    "url": "https://cdn.discordapp.com/voice.ogg",
                    "content_type": "audio/ogg",
                    "filename": "voice.ogg",
                    "duration_secs": 3.2,
                }
            ],
        }
    )

    assert len(calls) == 1
    messages = calls[0]["entry"][0]["changes"][0]["value"]["messages"]
    assert messages[0]["type"] == "audio"
    assert messages[0]["audio"]["duration"] == 3
    get_settings.cache_clear()


async def test_discord_gateway_accepts_partner(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "789")
    get_settings.cache_clear()
    calls = []

    async def process_inbound(pool, payload, coalescer=None, **kwargs):
        calls.append(payload)

    async def send_typing_after_delay(channel_id):
        calls.append({"typing": channel_id})

    monkeypatch.setattr("app.services.inbound.process_inbound", process_inbound)
    monkeypatch.setattr("app.services.discord._send_typing_after_delay", send_typing_after_delay)
    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, None)
    await bot._handle_message(
        {
            "id": "123",
            "content": "hello",
            "channel_id": "channel-1",
            "author": {"id": "456", "username": "maya"},
            }
        )
    await asyncio.sleep(0)

    assert {"typing": "channel-1"} not in calls
    assert any("entry" in call for call in calls)
    get_settings.cache_clear()


async def test_discord_gateway_typing_start_marks_pacer_through_raw_event(
    fake_pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    get_settings.cache_clear()
    pacer = DiscordPacer(fake_pool)

    class Coalescer:
        def __init__(self) -> None:
            self.pacer = pacer

    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, Coalescer(), pacer=pacer)
    await bot._gateway_loop.dispatch_payload(
        {
            "op": 0,
            "t": "TYPING_START",
            "d": {"user_id": "456", "channel_id": "channel-1", "timestamp": 12345},
        }
    )

    user_row = next(row for row in fake_pool.users.values() if row["phone"] == "456")
    typing_state = pacer.typing_state(user_row["id"])
    assert typing_state is not None
    assert typing_state.channel_id == "channel-1"
    assert user_row["name"] == "Partner A"
    get_settings.cache_clear()


async def test_seed_partner_users_upserts_configured_discord_ids(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "discord:789")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_B", "Partner B")
    get_settings.cache_clear()

    await seed_partner_users(fake_pool)

    users = {row["phone"]: row["name"] for row in fake_pool.users.values()}
    assert users == {"456": "Partner A", "789": "Partner B"}
    get_settings.cache_clear()


async def test_add_reaction_calls_discord_reaction_endpoint(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_rest = _MockRest()
    client = _make_test_client()
    client._rest = mock_rest
    async def mock_get_dm_channel_id(user_id):
        assert user_id == "456"
        return "channel-1"
    monkeypatch.setattr(client, "get_dm_channel_id", mock_get_dm_channel_id)
    register_client("mediator", client)

    await add_reaction("discord:456", "message-1", "👋", bot_id="mediator")

    assert mock_rest.calls[0][0] == "PUT"
    assert mock_rest.calls[0][1] == "/channels/channel-1/messages/message-1/reactions/%F0%9F%91%8B/@me"


async def test_discord_send_text_can_suppress_typing_indicator(app_env, monkeypatch: pytest.MonkeyPatch) -> None:
    typing_calls = []
    message_calls = []

    async def get_dm_channel_id(user_id):
        assert user_id == "456"
        return "channel-1"

    async def send_typing(channel_id):
        typing_calls.append(channel_id)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"id": "discord-message-1"}

    class Rest:
        async def send_message(self, channel_id, *, content):
            message_calls.append((channel_id, content))
            return Response()

    client = _make_test_client()
    monkeypatch.setattr(client, "get_dm_channel_id", get_dm_channel_id)
    monkeypatch.setattr(client, "send_typing", send_typing)
    # Replace the client's _rest with our mock
    client._rest = Rest()
    register_client("mediator", client)

    await send_text("discord:456", "quiet", send_typing_indicator=False, bot_id="mediator")
    await send_text("discord:456", "default", bot_id="mediator")

    assert typing_calls == ["channel-1"]
    assert message_calls == [("channel-1", "quiet"), ("channel-1", "default")]


async def test_catch_up_recent_messages_ingests_partner_history(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "")
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()
    await seed_partner_users(fake_pool)
    calls = []

    async def get_dm_channel_id(user_id):
        assert user_id == "456"
        return "channel-1"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"id": "m2", "content": "second", "author": {"id": "456", "username": "p"}},
                {"id": "m1", "content": "first", "author": {"id": "456", "username": "p"}},
            ]

    class Client:
        async def get(self, path, headers=None, params=None):
            calls.append((path, params))
            return Response()

    test_client = _make_test_client()
    monkeypatch.setattr(test_client, "get_dm_channel_id", get_dm_channel_id)
    # Replace the client's _rest with a mock that has fetch_channel_messages
    class MockRest:
        async def fetch_channel_messages(self, channel_id, **params):
            calls.append((f"/channels/{channel_id}/messages", params))
            return Response()
    test_client._rest = MockRest()

    class Coalescer:
        def __init__(self) -> None:
            self.calls = []

        async def add(self, user_id, message_id, user, *, source: str = "live", scope) -> None:
            self.calls.append((user_id, message_id, user, source, scope))

    coalescer = Coalescer()

    count = await catch_up_recent_messages(fake_pool, coalescer, client=test_client, bot_id="mediator")

    assert count == 2
    assert calls == [("/channels/channel-1/messages", {"limit": 50})]
    inbound_ids = {
        row["whatsapp_message_id"]
        for row in fake_pool.messages.values()
        if row["direction"] == "inbound"
    }
    assert inbound_ids == {"m1", "m2"}
    assert [call[3] for call in coalescer.calls] == ["catch_up", "catch_up"]
    get_settings.cache_clear()


async def test_catch_up_replay_is_idempotent(
    fake_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Running catch-up twice over the same REST window inserts only once and
    does not enqueue duplicate coalescer calls."""
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "")
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()
    await seed_partner_users(fake_pool)
    calls = []

    async def get_dm_channel_id(user_id):
        assert user_id == "456"
        return "channel-1"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"id": "m2", "content": "second", "author": {"id": "456", "username": "p"}},
                {"id": "m1", "content": "first", "author": {"id": "456", "username": "p"}},
            ]

    test_client = _make_test_client()
    monkeypatch.setattr(test_client, "get_dm_channel_id", get_dm_channel_id)

    class MockRest:
        async def fetch_channel_messages(self, channel_id, **params):
            calls.append((f"/channels/{channel_id}/messages", params))
            return Response()

    test_client._rest = MockRest()

    class Coalescer:
        def __init__(self) -> None:
            self.calls = []

        async def add(self, user_id, message_id, user, *, source: str = "live", scope) -> None:
            self.calls.append((user_id, message_id, user, source, scope))

    coalescer = Coalescer()
    count = await catch_up_recent_messages(fake_pool, coalescer, client=test_client, bot_id="mediator")
    assert count == 2
    assert len(coalescer.calls) == 2

    # Second pass: 0 inserted, 0 coalescer enqueued (both skipped_existing).
    coalescer2 = Coalescer()
    count2 = await catch_up_recent_messages(fake_pool, coalescer2, client=test_client, bot_id="mediator")
    assert count2 == 0
    assert len(coalescer2.calls) == 0

    inbound_ids = {
        row["whatsapp_message_id"]
        for row in fake_pool.messages.values()
        if row["direction"] == "inbound"
    }
    assert inbound_ids == {"m1", "m2"}
    get_settings.cache_clear()


async def test_catch_up_cross_bot_duplicate(
    fake_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Discord message id already stored for mediator must still be inserted
    for Hector — the idempotency key is (bot_id, discord_message_id)."""
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "")
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()
    await seed_partner_users(fake_pool)

    # Seed existing inbound row for mediator.
    mediator_msg_id = uuid4()
    user_id = next(iter(fake_pool.users))
    fake_pool.messages[mediator_msg_id] = {
        "id": mediator_msg_id,
        "direction": "inbound",
        "sender_id": user_id,
        "recipient_id": None,
        "content": "mediator already saw this",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": None,
        "whatsapp_message_id": "dc-msg-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
        "bot_id": "mediator",
        "topic_id": uuid4(),
    }

    async def get_dm_channel_id(user_id):
        assert user_id == "456"
        return "channel-1"

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return [
                {"id": "dc-msg-1", "content": "shared message", "author": {"id": "456", "username": "p"}},
            ]

    hector_client = _make_test_client(bot_id="hector")
    monkeypatch.setattr(hector_client, "get_dm_channel_id", get_dm_channel_id)

    class MockRest:
        async def fetch_channel_messages(self, channel_id, **params):
            return Response()

    hector_client._rest = MockRest()

    class Coalescer:
        def __init__(self) -> None:
            self.calls = []

        async def add(self, user_id, message_id, user, *, source: str = "live", scope) -> None:
            self.calls.append((user_id, message_id, user, source, scope))

    coalescer = Coalescer()
    count = await catch_up_recent_messages(fake_pool, coalescer, client=hector_client, bot_id="hector")
    assert count == 1
    assert len(coalescer.calls) == 1

    # Both bot rows exist with the same whatsapp_message_id.
    hector_rows = [
        row for row in fake_pool.messages.values()
        if row["direction"] == "inbound" and row["whatsapp_message_id"] == "dc-msg-1" and row["bot_id"] == "hector"
    ]
    mediator_rows = [
        row for row in fake_pool.messages.values()
        if row["direction"] == "inbound" and row["whatsapp_message_id"] == "dc-msg-1" and row["bot_id"] == "mediator"
    ]
    assert len(hector_rows) == 1
    assert len(mediator_rows) == 1
    get_settings.cache_clear()


async def test_gateway_ready_triggers_catch_up(
    fake_pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When _run_once receives READY it must call catch_up_recent_messages with
    the correct bot_id and client."""
    import json as _json

    import websockets as _websockets

    import app.services.discord as _discord_mod

    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()

    catch_up_calls: list[dict] = []

    async def _fake_catch_up(pool, coalescer, *, client, bot_id, limit=50):
        catch_up_calls.append({"bot_id": bot_id, "client": client})
        return 0

    monkeypatch.setattr(_discord_mod, "catch_up_recent_messages", _fake_catch_up)

    # Build a fake websocket that yields HELLO then READY.
    ws_events = [
        _json.dumps({"op": 10, "d": {"heartbeat_interval": 1000}}),
        _json.dumps({"op": 0, "t": "READY", "d": {"user": {"username": "test", "id": "123"}, "session_id": "abc", "guilds": []}}),
    ]

    class _MockWS:
        def __init__(self):
            self._events = iter(ws_events)
            self.sent: list[str] = []

        async def recv(self):
            try:
                return next(self._events)
            except StopIteration:
                raise _websockets.exceptions.ConnectionClosed(None, None)

        async def send(self, data):
            self.sent.append(data)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._events)
            except StopIteration:
                raise StopAsyncIteration

    class _FakeCtx:
        def __init__(self, ws):
            self.ws = ws

        async def __aenter__(self):
            return self.ws

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(_websockets, "connect", lambda url: _FakeCtx(_MockWS()))

    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, None)
    await bot._run_once()

    # Tear down the heartbeat task that _run_once spawned.
    if bot._heartbeat_task and not bot._heartbeat_task.done():
        bot._heartbeat_task.cancel()
        try:
            await bot._heartbeat_task
        except asyncio.CancelledError:
            pass

    assert len(catch_up_calls) == 1
    assert catch_up_calls[0]["bot_id"] == "mediator"
    assert catch_up_calls[0]["client"] is bot.client
    get_settings.cache_clear()


async def test_catch_up_rest_failure_is_logged(
    fake_pool, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """When the REST fetch raises, catch_up_recent_messages logs the error and
    returns 0 without propagating the exception."""
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_B", "")
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    get_settings.cache_clear()
    await seed_partner_users(fake_pool)

    async def get_dm_channel_id(user_id):
        return "channel-1"

    class FailingRest:
        async def fetch_channel_messages(self, channel_id, **params):
            raise httpx.HTTPStatusError(
                "REST failure",
                request=httpx.Request("GET", "https://discord.com/api/v10/channels/x/messages"),
                response=httpx.Response(500),
            )

    test_client = _make_test_client()
    monkeypatch.setattr(test_client, "get_dm_channel_id", get_dm_channel_id)
    test_client._rest = FailingRest()

    import logging

    with caplog.at_level(logging.ERROR):
        count = await catch_up_recent_messages(fake_pool, None, client=test_client, bot_id="mediator")

    assert count == 0
    assert "discord catch-up bot=mediator channel=channel-1 failed" in caplog.text
    get_settings.cache_clear()


async def test_discord_gateway_logs_reaction_feedback(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    monkeypatch.setenv("DISCORD_PARTNER_NAME_A", "Partner A")
    get_settings.cache_clear()
    await seed_partner_users(fake_pool)
    outbound_id = uuid4()
    fake_pool.messages[outbound_id] = {
        "id": outbound_id,
        "direction": "outbound",
        "sender_id": None,
        "recipient_id": next(iter(fake_pool.users)),
        "content": "I hear you.",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": "discord-out-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
        "bot_id": "mediator",
        "topic_id": uuid4(),
    }

    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, None)
    await bot._handle_reaction_add(
        {"user_id": "456", "message_id": "discord-out-1", "emoji": {"name": "👍"}}
    )

    feedback = next(iter(fake_pool.feedback.values()))
    assert feedback["source"] == "reaction"
    assert feedback["target_type"] == "message"
    assert feedback["target_id"] == outbound_id
    assert feedback["sentiment"] == "positive"
    assert feedback["content"] == "👍"
    get_settings.cache_clear()


async def test_discord_gateway_updates_and_deletes_messages(fake_pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_PARTNER_USER_ID_A", "456")
    get_settings.cache_clear()
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": uuid4(),
        "recipient_id": None,
        "content": "old",
        "processing_state": "processed",
        "sent_at": datetime.now(UTC),
        "charge": "routine",
        "whatsapp_message_id": "discord-in-1",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }

    bot = DiscordGatewayBot("mediator", _make_test_client(), fake_pool, None)
    await bot._handle_message_update(
        {"id": "discord-in-1", "content": "new", "author": {"id": "456"}}
    )
    await bot._handle_message_delete({"id": "discord-in-1"})

    assert fake_pool.messages[message_id]["content"] == "new"
    assert fake_pool.messages[message_id]["edit_history"][0]["content"] == "old"
    assert fake_pool.messages[message_id]["edited_at"] is not None
    assert fake_pool.messages[message_id]["deleted_at"] is not None
    get_settings.cache_clear()
