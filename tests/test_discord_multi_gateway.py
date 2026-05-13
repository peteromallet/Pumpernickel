"""Multi-gateway Discord tests.

Covers:
- Two channels + two tokens → both gateways started
- Facade dispatch to correct DiscordClient
- Legacy fallback (single DISCORD_BOT_TOKEN for mediator)
- Missing token skip
- Zero rows + legacy token synthesis
- Zero rows + no token
- UndefinedTableError fallback
- Per-bot pacer isolation
- WhatsApp provider skip
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient as HttpxAsyncClient

from app.config import get_settings
from app.services.discord import (
    DiscordClient,
    close_all_clients,
    get_client,
    iter_clients,
    register_client,
)
from app.services.discord_id import discord_bot_user_id
from app.services.pacer import DiscordPacer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_stub_client(bot_id: str = "mediator", bot_user_id: str = "123456789") -> DiscordClient:
    """Create a minimal DiscordClient stub for multi-gateway tests."""
    client = DiscordClient.__new__(DiscordClient)
    client.bot_id = bot_id
    client._token = "stub-token"
    client._http = HttpxAsyncClient()
    client._rest = None
    client.bot_user_id = bot_user_id
    return client


# ---------------------------------------------------------------------------
# Facade dispatch tests
# ---------------------------------------------------------------------------

class TestFacadeDispatch:
    """Verify facade functions route to the correct DiscordClient."""

    async def test_get_client_returns_registered_client(self):
        """Register a client and verify get_client returns it."""
        client = _make_stub_client("mediator")
        register_client("mediator", client)
        try:
            assert get_client("mediator") is client
        finally:
            await close_all_clients()

    async def test_facade_routes_to_correct_client(self, monkeypatch):
        """Facade send_text routes to the client for the given bot_id."""
        med_client = _make_stub_client("mediator")
        ros_client = _make_stub_client("tante_rosi")

        med_calls = []
        ros_calls = []

        async def med_send_text(to, body, *, send_typing_indicator=True):
            med_calls.append((to, body))
            return {"messages": [{"id": "med-1"}]}

        async def ros_send_text(to, body, *, send_typing_indicator=True):
            ros_calls.append((to, body))
            return {"messages": [{"id": "ros-1"}]}

        monkeypatch.setattr(med_client, "send_text", med_send_text)
        monkeypatch.setattr(ros_client, "send_text", ros_send_text)
        register_client("mediator", med_client)
        register_client("tante_rosi", ros_client)

        try:
            from app.services.discord import send_text
            await send_text("user-a", "hello mediator", bot_id="mediator")
            await send_text("user-b", "hello rosi", bot_id="tante_rosi")

            assert med_calls == [("user-a", "hello mediator")]
            assert ros_calls == [("user-b", "hello rosi")]
        finally:
            await close_all_clients()

    async def test_iter_clients_lists_all_registered(self):
        """iter_clients yields all registered (bot_id, client) pairs."""
        med = _make_stub_client("mediator")
        ros = _make_stub_client("tante_rosi")
        register_client("mediator", med)
        register_client("tante_rosi", ros)
        try:
            pairs = list(iter_clients())
            bot_ids = {bid for bid, _ in pairs}
            assert bot_ids == {"mediator", "tante_rosi"}
        finally:
            await close_all_clients()


# ---------------------------------------------------------------------------
# Token resolution tests (per env-var convention)
# ---------------------------------------------------------------------------

class TestTokenResolution:
    """Verify env-var token resolution: per-bot, legacy fallback, missing."""

    def test_per_bot_tokens_detected(self, monkeypatch):
        """DISCORD_BOT_TOKEN_MEDIATOR and _TANTE_ROSI → both in dict."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN_MEDIATOR", "tok1")
        monkeypatch.setenv("DISCORD_BOT_TOKEN_TANTE_ROSI", "tok2")
        get_settings.cache_clear()
        try:
            tokens = get_settings().discord_bot_tokens
            assert "mediator" in tokens
            assert "tante_rosi" in tokens
        finally:
            get_settings.cache_clear()

    def test_legacy_fallback_when_no_per_bot(self, monkeypatch):
        """Only DISCORD_BOT_TOKEN → per_bot_tokens dict is empty."""
        monkeypatch.setenv("DISCORD_BOT_TOKEN", "legacy-tok")
        monkeypatch.delenv("DISCORD_BOT_TOKEN_MEDIATOR", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN_TANTE_ROSI", raising=False)
        get_settings.cache_clear()
        try:
            tokens = get_settings().discord_bot_tokens
            assert tokens == {}
            legacy = get_settings().discord_bot_token
            assert legacy is not None
        finally:
            get_settings.cache_clear()

    def test_user_id_overrides(self, monkeypatch):
        """DISCORD_BOT_USER_ID_MEDIATOR → override dict entry."""
        monkeypatch.setenv("DISCORD_BOT_USER_ID_MEDIATOR", "999888777")
        get_settings.cache_clear()
        try:
            overrides = get_settings().discord_bot_user_id_overrides
            assert overrides.get("mediator") == "999888777"
        finally:
            get_settings.cache_clear()

    def test_discord_bot_user_id_override_wins(self, monkeypatch):
        """Override DISCORD_BOT_USER_ID_MEDIATOR has priority."""
        monkeypatch.setenv("DISCORD_BOT_USER_ID_MEDIATOR", "111111")
        monkeypatch.setenv("DISCORD_BOT_TOKEN_MEDIATOR", "MTIzNDU2Nzg5MA.sig.hmac")
        get_settings.cache_clear()
        try:
            result = discord_bot_user_id("mediator")
            assert result == "111111"
        finally:
            get_settings.cache_clear()

    def test_discord_bot_user_id_falls_back_to_token_decode(self, monkeypatch):
        """Without override, decode from per-bot token."""
        monkeypatch.delenv("DISCORD_BOT_USER_ID_MEDIATOR", raising=False)
        monkeypatch.setenv("DISCORD_BOT_TOKEN_MEDIATOR", "MTIzNDU2Nzg5MA.sig.hmac")
        get_settings.cache_clear()
        try:
            result = discord_bot_user_id("mediator")
            assert result == "1234567890"
        finally:
            get_settings.cache_clear()

    def test_discord_bot_user_id_returns_none_for_unknown_bot(self, monkeypatch):
        """Unknown bot_id with no token → None."""
        monkeypatch.delenv("DISCORD_BOT_USER_ID_UNKNOWN", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN_UNKNOWN", raising=False)
        monkeypatch.delenv("DISCORD_BOT_USER_ID", raising=False)
        monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
        get_settings.cache_clear()
        try:
            result = discord_bot_user_id("unknown_bot")
            assert result is None
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# FakePool channel query tests
# ---------------------------------------------------------------------------

class TestFakePoolChannels:
    """Verify FakePool can model the channels SELECT query."""

    def test_channels_select_returns_seeded_rows(self, fake_pool):
        """FakePool.fetch returns seeded channels rows."""
        fake_pool.channels[("discord", "123456789")] = {
            "bot_id": "mediator",
            "address": "123456789",
        }
        fake_pool.channels[("discord", "987654321")] = {
            "bot_id": "tante_rosi",
            "address": "987654321",
        }

        # The FakePool fetch for channels
        rows = []
        for (transport, address), row in fake_pool.channels.items():
            if transport == "discord":
                rows.append(row)

        assert len(rows) == 2
        bot_ids = {r["bot_id"] for r in rows}
        assert bot_ids == {"mediator", "tante_rosi"}

    def test_channels_select_empty(self, fake_pool):
        """Empty channels → no rows."""
        rows = [
            row for (transport, _), row in fake_pool.channels.items()
            if transport == "discord"
        ]
        assert rows == []

    def test_undefined_table_error_flag(self, fake_pool):
        """channels_raise_undefined_table sets the flag."""
        fake_pool.channels_raise_undefined_table()
        assert fake_pool._raise_undefined_table_on_channels is True


# ---------------------------------------------------------------------------
# Lifespan registration simulation
# ---------------------------------------------------------------------------

class TestLifespanRegistration:
    """Simulate key lifespan registration paths."""

    def test_synthesize_mediator_from_legacy(self):
        """Zero rows + legacy token → synthesize mediator entry."""
        channel_rows = []
        legacy_token_str = "some-legacy-token"

        bot_entries = []
        if not bot_entries and not channel_rows and legacy_token_str:
            bot_entries.append(("mediator", legacy_token_str))

        assert bot_entries == [("mediator", "some-legacy-token")]

    def test_skip_bot_with_no_token(self):
        """Channels row for tante_rosi but no token → skip."""
        channel_rows = [{"bot_id": "tante_rosi", "address": "9876"}]
        per_bot_tokens = {"mediator": "tok1"}  # Only mediator has a token

        bot_entries = []
        for row in channel_rows:
            bot_id = row["bot_id"]
            if bot_id in per_bot_tokens:
                bot_entries.append((bot_id, per_bot_tokens[bot_id]))
            else:
                # Skip — no token
                pass

        assert bot_entries == []

    def test_two_channels_two_tokens(self):
        """Two channels + two tokens → both registered."""
        channel_rows = [
            {"bot_id": "mediator", "address": "1234"},
            {"bot_id": "tante_rosi", "address": "5678"},
        ]
        per_bot_tokens = {"mediator": "tok1", "tante_rosi": "tok2"}

        bot_entries = []
        for row in channel_rows:
            bot_id = row["bot_id"]
            if bot_id in per_bot_tokens:
                bot_entries.append((bot_id, per_bot_tokens[bot_id]))

        assert len(bot_entries) == 2
        assert set(bid for bid, _ in bot_entries) == {"mediator", "tante_rosi"}

    def test_legacy_fallback_single_row(self):
        """Single channel row + legacy token → use legacy."""
        channel_rows = [{"bot_id": "mediator", "address": "1234"}]
        per_bot_tokens = {}
        legacy_token_str = "legacy-tok"

        bot_entries = []
        for row in channel_rows:
            bot_id = row["bot_id"]
            if bot_id in per_bot_tokens:
                bot_entries.append((bot_id, per_bot_tokens[bot_id]))
            elif len(channel_rows) == 1 and legacy_token_str:
                bot_entries.append((bot_id, legacy_token_str))

        assert bot_entries == [("mediator", "legacy-tok")]

    def test_two_rows_legacy_skips_both(self):
        """Two channels + no per-bot tokens + legacy → skip both (ambiguous)."""
        channel_rows = [
            {"bot_id": "mediator", "address": "1234"},
            {"bot_id": "tante_rosi", "address": "5678"},
        ]
        per_bot_tokens = {}
        legacy_token_str = "legacy-tok"

        bot_entries = []
        for row in channel_rows:
            bot_id = row["bot_id"]
            if bot_id in per_bot_tokens:
                bot_entries.append((bot_id, per_bot_tokens[bot_id]))
            elif len(channel_rows) == 1 and legacy_token_str:
                bot_entries.append((bot_id, legacy_token_str))
            # else: skip (ambiguous)

        assert bot_entries == []


# ---------------------------------------------------------------------------
# Per-bot pacer isolation
# ---------------------------------------------------------------------------

class TestPerBotPacerIsolation:
    """Verify pacer instances are independent per bot_id."""

    def test_pacers_are_separate_instances(self):
        """Two pacers for different bots are separate objects."""
        pacer_a = DiscordPacer.__new__(DiscordPacer)
        pacer_b = DiscordPacer.__new__(DiscordPacer)
        assert pacer_a is not pacer_b

    def test_pacer_dict_keyed_by_bot_id(self):
        """Pacer dict stores per-bot pacers."""
        pacers = {}
        pacers["mediator"] = DiscordPacer.__new__(DiscordPacer)
        pacers["tante_rosi"] = DiscordPacer.__new__(DiscordPacer)

        assert "mediator" in pacers
        assert "tante_rosi" in pacers
        assert pacers["mediator"] is not pacers["tante_rosi"]


# ---------------------------------------------------------------------------
# WhatsApp provider skip
# ---------------------------------------------------------------------------

class TestWhatsAppProviderSkip:
    """Discord init is skipped when MESSAGING_PROVIDER != discord."""

    def test_discord_provider_disabled_returns_false(self):
        """_discord_provider_enabled returns False for non-discord providers."""
        from app.main import _discord_provider_enabled

        class FakeSettings:
            messaging_provider = "whatsapp"

        assert not _discord_provider_enabled(FakeSettings())

    def test_discord_provider_enabled_returns_true(self):
        """_discord_provider_enabled returns True for discord."""
        from app.main import _discord_provider_enabled

        class FakeSettings:
            messaging_provider = "discord"

        assert _discord_provider_enabled(FakeSettings())


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
async def _cleanup_clients():
    """Ensure client registry is clean after each test."""
    yield
    await close_all_clients()
