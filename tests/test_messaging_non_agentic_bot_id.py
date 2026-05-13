"""Verify bot_id threading through send_outbound.

Non-agentic callers (transcription, vision, scheduled_job_handlers) pass
bot_id='mediator' as a string literal.  Agentic callers source from ctx.bot_id.
"""

from __future__ import annotations

from uuid import uuid4

from app.services.messaging import send_outbound


# ---------------------------------------------------------------------------
# send_outbound bot_id threading
# ---------------------------------------------------------------------------

class TestSendOutboundBotIDThreading:
    """Verify send_outbound accepts and forwards bot_id."""

    async def test_send_outbound_with_bot_id_mediator(self, fake_pool, monkeypatch):
        """send_outbound with bot_id='mediator' calls discord.send_text with bot_id='mediator'."""
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
            await send_outbound(fake_pool, user, "hello", bot_id="mediator")
            assert discord_called == [(user.phone, "hello", "mediator")]
        finally:
            get_settings.cache_clear()

    async def test_send_outbound_without_bot_id_defaults_none(self, fake_pool, monkeypatch):
        """send_outbound without bot_id passes None (legacy compat)."""
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
            await send_outbound(fake_pool, user, "hello")
            # bot_id defaults to None; note that in production callers always pass bot_id='mediator'
            assert discord_called == [(user.phone, "hello", None)]
        finally:
            get_settings.cache_clear()

    async def test_send_outbound_bot_id_tante_rosi(self, fake_pool, monkeypatch):
        """send_outbound with bot_id='tante_rosi' forwards correctly."""
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
            await send_outbound(fake_pool, user, "hello", bot_id="tante_rosi")
            assert discord_called == [(user.phone, "hello", "tante_rosi")]
        finally:
            get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Non-agentic caller verification
# ---------------------------------------------------------------------------

class TestNonAgenticCallers:
    """Confirm non-agentic callers pass bot_id='mediator' as a string literal."""

    def test_transcription_passes_mediator(self):
        """transcription.py passes bot_id='mediator' to send_outbound."""
        import ast
        content = open("app/services/transcription.py").read()
        tree = ast.parse(content)
        # Find send_outbound calls with bot_id='mediator'
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in getattr(node, 'keywords', []):
                    if (getattr(kw, 'arg', None) == 'bot_id'
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value == 'mediator'):
                        found = True
                        break
        assert found, "transcription.py should have at least one send_outbound(..., bot_id='mediator')"

    def test_vision_passes_mediator(self):
        """vision.py passes bot_id='mediator' to send_outbound."""
        import ast
        content = open("app/services/vision.py").read()
        tree = ast.parse(content)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in getattr(node, 'keywords', []):
                    if (getattr(kw, 'arg', None) == 'bot_id'
                            and isinstance(kw.value, ast.Constant)
                            and kw.value.value == 'mediator'):
                        found = True
                        break
        assert found, "vision.py should have at least one send_outbound(..., bot_id='mediator')"

    def test_scheduled_job_handlers_passes_mediator(self):
        """scheduled_job_handlers.py defaults bot_id to 'mediator'."""
        import ast
        content = open("app/services/scheduled_job_handlers.py").read()
        tree = ast.parse(content)
        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in getattr(node, 'keywords', []):
                    if getattr(kw, 'arg', None) == 'bot_id':
                        # Accept either literal 'mediator' or job.get('bot_id','mediator')
                        if (isinstance(kw.value, ast.Constant) and kw.value.value == 'mediator'):
                            found = True
                            break
                        # Also accept Call expressions that default to 'mediator'
                        elif isinstance(kw.value, ast.Call):
                            # Look for 'mediator' anywhere in the call as a default
                            for subnode in ast.walk(kw.value):
                                if (isinstance(subnode, ast.Constant)
                                        and subnode.value == 'mediator'):
                                    found = True
                                    break
        assert found, "scheduled_job_handlers.py should have at least one bot_id defaulting to 'mediator'"


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
    return User(id=user_id, name="Test User", phone="15555550100", timezone="UTC")