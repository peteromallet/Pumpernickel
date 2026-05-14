"""Verify scope threading through non-agentic outbound paths.

Non-agentic callers that still run outside an agentic turn must not lose bot
identity. Media handlers and direct outbound calls must route through
InboundScope instead of loose bot_id/topic_id kwargs.
"""

from __future__ import annotations

from uuid import uuid4

from app.services.messaging import send_outbound


# ---------------------------------------------------------------------------
# send_outbound scope threading
# ---------------------------------------------------------------------------


class TestSendOutboundScopeThreading:
    """Verify send_outbound requires scope and forwards scope.bot_id."""

    async def test_send_outbound_with_mediator_scope(
        self, fake_pool, monkeypatch, make_inbound_scope
    ):
        """send_outbound with mediator scope calls discord.send_text with bot_id='mediator'."""
        monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
        from app.config import get_settings

        get_settings.cache_clear()

        user = _make_user(fake_pool)
        discord_called = []

        async def fake_send_text(to, body, *, send_typing_indicator=True, bot_id):
            discord_called.append((to, body, bot_id))
            return {"messages": [{"id": "discord-out"}]}

        monkeypatch.setattr("app.services.discord.send_text", fake_send_text)

        try:
            await send_outbound(
                fake_pool,
                user,
                "hello",
                scope=make_inbound_scope(user, bot_id="mediator"),
            )
            assert discord_called == [(user.phone, "hello", "mediator")]
        finally:
            get_settings.cache_clear()

    async def test_send_outbound_without_scope_rejected(self, fake_pool, monkeypatch):
        """send_outbound without scope is no longer a valid public API."""
        monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
        from app.config import get_settings

        get_settings.cache_clear()

        user = _make_user(fake_pool)

        try:
            try:
                await send_outbound(fake_pool, user, "hello")
            except TypeError as exc:
                assert "scope" in str(exc)
            else:
                raise AssertionError("send_outbound accepted missing scope")
        finally:
            get_settings.cache_clear()

    async def test_send_outbound_with_tante_rosi_scope(
        self, fake_pool, monkeypatch, make_inbound_scope
    ):
        """send_outbound with tante_rosi scope forwards correctly."""
        monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
        from app.config import get_settings

        get_settings.cache_clear()

        user = _make_user(fake_pool)
        discord_called = []

        async def fake_send_text(to, body, *, send_typing_indicator=True, bot_id):
            discord_called.append((to, body, bot_id))
            return {"messages": [{"id": "discord-out"}]}

        monkeypatch.setattr("app.services.discord.send_text", fake_send_text)

        try:
            await send_outbound(
                fake_pool,
                user,
                "hello",
                scope=make_inbound_scope(user, bot_id="tante_rosi"),
            )
            assert discord_called == [(user.phone, "hello", "tante_rosi")]
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Non-agentic caller verification
# ---------------------------------------------------------------------------


class TestNonAgenticCallers:
    """Confirm non-agentic callers preserve bot identity."""

    def test_transcription_passes_mediator(self):
        """transcription.py routes media failures by scope when scope exists."""
        import ast

        content = open("app/services/transcription.py").read()
        tree = ast.parse(content)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in getattr(node, "keywords", []):
                    if (
                        getattr(kw, "arg", None) == "scope"
                        and isinstance(kw.value, ast.Name)
                        and kw.value.id == "scope"
                    ):
                        found = True
                        break
        assert found, "transcription.py should route send_outbound through scope"

    def test_vision_passes_mediator(self):
        """vision.py routes media failures by scope when scope exists."""
        import ast

        content = open("app/services/vision.py").read()
        tree = ast.parse(content)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in getattr(node, "keywords", []):
                    if (
                        getattr(kw, "arg", None) == "scope"
                        and isinstance(kw.value, ast.Name)
                        and kw.value.id == "scope"
                    ):
                        found = True
                        break
        assert found, "vision.py should route send_outbound through scope"

    def test_scheduled_job_handlers_has_no_mediator_bot_id_default(self):
        """scheduled_job_handlers.py must not fabricate mediator bot ids."""
        import ast

        content = open("app/services/scheduled_job_handlers.py").read()
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.arg)
                and node.arg == "bot_id"
                and getattr(node, "annotation", None) is not None
            ):
                continue
            if isinstance(node, ast.Call):
                for kw in getattr(node, "keywords", []):
                    if getattr(kw, "arg", None) == "bot_id":
                        assert not (
                            isinstance(kw.value, ast.Constant)
                            and kw.value.value == "mediator"
                        )
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "get" and len(node.args) >= 2:
                    assert not (
                        isinstance(node.args[0], ast.Constant)
                        and node.args[0].value == "bot_id"
                        and isinstance(node.args[1], ast.Constant)
                        and node.args[1].value == "mediator"
                    )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_user(pool):
    """Insert a test user into the FakePool and return a User object."""
    from app.models.user import User

    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": "Test User",
        "phone": "15555550100",
        "timezone": "UTC",
        "onboarding_state": "pending",
        "pacing_preferences": {},
        "pregnancy_edd": None,
        "pregnancy_dating_basis": None,
        "pregnancy_lmp_date": None,
        "pregnancy_scan_date": None,
        "pregnancy_scan_corrected_at": None,
        "pregnancy_started_at": None,
        "pregnancy_ended_at": None,
        "pregnancy_outcome": None,
    }
    return User(id=user_id, name="Test User", phone="15555550100", timezone="UTC")
