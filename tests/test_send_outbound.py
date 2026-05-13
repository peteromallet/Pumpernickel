from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import hooks, system_state
from app.services.pacer import DiscordPacer
from app.services.messaging import send_outbound, send_outbound_part
from app.services.scope import InboundScope
from app.services.templates import TemplateCall, render_template
from app.services.tools.read_tools import send_message_part
from app.services.turn_context import TurnContext
from app.services import whatsapp
from tool_schemas import SendMessagePartInput


pytestmark = pytest.mark.anyio


@pytest.fixture(autouse=True)
def reset_hooks():
    hooks.check_oob = None
    yield
    hooks.check_oob = None


def _user(fake_pool) -> User:
    row = {"id": uuid4(), "name": "Maya", "phone": "15555550100", "timezone": "UTC"}
    fake_pool.users[row["id"]] = row
    return User(**row)


def _scope(user: User, *, bot_id: str = "mediator") -> InboundScope:
    return InboundScope(
        bot_id=bot_id,
        transport="discord",
        user_id=user.id,
        topic_id=uuid4(),
        channel_id=None,
        binding_id=uuid4(),
        dyad_id=uuid4(),
    )


def _inbound(fake_pool, user: User, sent_at: datetime) -> None:
    message_id = uuid4()
    fake_pool.messages[message_id] = {
        "id": message_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "hi",
        "processing_state": "raw",
        "sent_at": sent_at,
        "charge": None,
        "whatsapp_message_id": f"wa-{message_id}",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }


async def test_free_form_path_sends_text_and_updates_row(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []

    async def send_text(to, body):
        sent.append((to, body))
        return {"messages": [{"id": "wamid.out"}]}

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)

    row_id = await send_outbound(fake_pool, user, "hello", scope=_scope(user))

    assert sent == [(user.phone, "hello")]
    assert fake_pool.messages[row_id]["whatsapp_message_id"] == "wamid.out"
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    assert fake_pool.users[user.id]["onboarding_state"] == "welcomed"


async def test_free_form_path_records_bot_turn_id(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    turn_id = uuid4()
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))

    async def send_text(to, body):
        return {"messages": [{"id": "wamid.out"}]}

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)

    row_id = await send_outbound(fake_pool, user, "hello", bot_turn_id=turn_id, scope=_scope(user))

    assert fake_pool.messages[row_id]["bot_turn_id"] == turn_id
    assert fake_pool.messages[row_id]["whatsapp_message_id"] == "wamid.out"


async def test_successful_bot_initiated_outbound_marks_onboarding_welcomed(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)

    async def send_template(to, payload):
        return {"messages": [{"id": "wamid.template"}]}

    monkeypatch.setattr("app.services.whatsapp.send_template", send_template)

    row_id = await send_outbound(
        fake_pool,
        user,
        "first contact",
        template_fallback=TemplateCall("media_failure", [user.name, "voice"]),
        scope=_scope(user),
    )

    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    assert fake_pool.users[user.id]["onboarding_state"] == "welcomed"


async def test_template_path_and_param_validation(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(hours=25))
    sent = []

    async def send_template(to, payload):
        sent.append((to, payload))
        return {"messages": [{"id": "wamid.template"}]}

    monkeypatch.setattr("app.services.whatsapp.send_template", send_template)
    await send_outbound(fake_pool, user, "nudge", template_fallback=TemplateCall("media_failure", [user.name, "voice"]), scope=_scope(user))

    assert sent == [(user.phone, render_template(TemplateCall("media_failure", [user.name, "voice"])))]
    with pytest.raises(ValueError):
        await send_outbound(fake_pool, user, "bad", template_fallback=TemplateCall("media_failure", []), scope=_scope(user))
    assert len([row for row in fake_pool.messages.values() if row["direction"] == "outbound"]) == 1
    assert sent == [(user.phone, render_template(TemplateCall("media_failure", [user.name, "voice"])))]


async def test_twilio_send_text_and_template(app_env, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "twilio-token")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+14155238886")
    from app.config import get_settings

    get_settings.cache_clear()
    whatsapp._client = None
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sid": "SMtwilio"}

    class Client:
        async def post(self, path, auth=None, data=None, json=None, headers=None):
            calls.append((path, auth, data))
            return Response()

    async def get_client():
        return Client()

    monkeypatch.setattr(whatsapp, "_get_client", get_client)

    result = await whatsapp.send_text("+15555550100", "hello")
    template_result = await whatsapp.send_template(
        "+15555550100",
        render_template(TemplateCall("media_failure", ["Maya", "voice"])),
    )

    assert result == {"messages": [{"id": "SMtwilio"}]}
    assert template_result == {"messages": [{"id": "SMtwilio"}]}
    assert calls[0][0] == "/2010-04-01/Accounts/AC123/Messages.json"
    assert calls[0][1] == ("AC123", "twilio-token")
    assert calls[0][2] == {"From": "whatsapp:+14155238886", "To": "whatsapp:+15555550100", "Body": "hello"}
    assert "couldn't process" in calls[1][2]["Body"]
    get_settings.cache_clear()
    whatsapp._client = None


async def test_twilio_api_key_auth_uses_account_sid_for_url(app_env, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "twilio")
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC123")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "account-token")
    monkeypatch.setenv("TWILIO_API_KEY_SID", "SK123")
    monkeypatch.setenv("TWILIO_API_KEY_SECRET", "api-secret")
    monkeypatch.setenv("TWILIO_WHATSAPP_FROM", "+14155238886")
    from app.config import get_settings

    get_settings.cache_clear()
    whatsapp._client = None
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"sid": "SMtwilio"}

    class Client:
        async def post(self, path, auth=None, data=None, json=None, headers=None):
            calls.append((path, auth, data))
            return Response()

    async def get_client():
        return Client()

    monkeypatch.setattr(whatsapp, "_get_client", get_client)

    await whatsapp.send_text("+15555550100", "hello")

    assert calls[0][0] == "/2010-04-01/Accounts/AC123/Messages.json"
    assert calls[0][1] == ("SK123", "api-secret")
    get_settings.cache_clear()
    whatsapp._client = None


async def test_discord_provider_sends_without_whatsapp_window(fake_pool, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    sent = []

    async def send_text(to, body, *, send_typing_indicator=True, bot_id="mediator"):
        sent.append((to, body, send_typing_indicator))
        return {"messages": [{"id": "discord-message"}]}

    monkeypatch.setattr("app.services.discord.send_text", send_text)

    row_id = await send_outbound(fake_pool, user, "hello discord", scope=_scope(user))

    assert sent == [(user.phone, "hello discord", True)]
    assert fake_pool.messages[row_id]["whatsapp_message_id"] == "discord-message"
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    get_settings.cache_clear()


async def test_discord_provider_can_suppress_low_level_typing(fake_pool, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    sent = []

    async def send_text(to, body, *, send_typing_indicator=True, bot_id="mediator"):
        sent.append((to, body, send_typing_indicator))
        return {"messages": [{"id": "discord-message"}]}

    monkeypatch.setattr("app.services.discord.send_text", send_text)

    row_id = await send_outbound(fake_pool, user, "hello discord", send_typing_indicator=False, scope=_scope(user))

    assert sent == [(user.phone, "hello discord", False)]
    assert fake_pool.messages[row_id]["whatsapp_message_id"] == "discord-message"
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    get_settings.cache_clear()


async def test_null_window_uses_template_no_none_arithmetic(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    sent = []

    async def send_template(to, payload):
        sent.append((to, payload))
        return {"messages": [{"id": "wamid.template"}]}

    monkeypatch.setattr("app.services.whatsapp.send_template", send_template)
    await send_outbound(
        fake_pool,
        user,
        "pause",
        template_fallback=TemplateCall("pause_confirmation", [user.name, "Sam"]),
        scope=_scope(user),
    )

    assert sent


async def test_defer_without_template_appends_reasoning(fake_pool) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(hours=25))
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
        "triggering_message_ids": [],
    }

    row_id = await send_outbound(fake_pool, user, "too specific", bot_turn_id=turn_id, scope=_scope(user))

    assert fake_pool.messages[row_id]["processing_state"] == "withheld"
    assert "outside WhatsApp 24h window" in fake_pool.bot_turns[turn_id]["reasoning"]
    assert fake_pool.users[user.id].get("onboarding_state", "pending") == "pending"


async def test_retry_success_and_exhaustion(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    attempts = 0
    sleeps = []

    async def send_text(to, body):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("temporary")
        return {"messages": [{"id": "wamid.retry"}]}

    async def no_sleep(seconds):
        sleeps.append(seconds)
        return None

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    monkeypatch.setattr("app.services.messaging.asyncio.sleep", no_sleep)
    row_id = await send_outbound(fake_pool, user, "hello", scope=_scope(user))
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    assert sleeps == [1, 2]

    attempts = 0
    sleeps.clear()
    fake_pool.users[user.id]["onboarding_state"] = "pending"

    async def always_fails(to, body):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("down")

    monkeypatch.setattr("app.services.whatsapp.send_text", always_fails)
    row_id = await send_outbound(fake_pool, user, "hello", scope=_scope(user))
    assert attempts == 3
    assert sleeps == [1, 2]
    assert fake_pool.messages[row_id]["processing_state"] == "expired"
    assert fake_pool.users[user.id]["onboarding_state"] == "pending"


async def test_pause_and_oob_hooks(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []

    async def send_text(to, body):
        sent.append(body)
        return {"messages": [{"id": "wamid.oob"}]}

    async def paused(user_id, *, bot_id=None):
        return True

    monkeypatch.setattr("app.services.hooks.paused_for_user", paused)
    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    row_id = await send_outbound(fake_pool, user, "hidden", scope=_scope(user))
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"
    assert sent == []

    row_id = await send_outbound(fake_pool, user, "control", ignore_pause=True, scope=_scope(user))
    assert fake_pool.messages[row_id]["processing_state"] == "processed"
    assert sent == ["control"]
    sent.clear()

    async def not_paused(user_id, *, bot_id=None):
        return False

    monkeypatch.setattr("app.services.hooks.paused_for_user", not_paused)

    async def rewrite(pool, content, recipient, protected_owner_ids=None, *, bot_id, topic_id):
        assert bot_id == "mediator"
        assert topic_id is not None
        return {"verdict": "rewrite", "reason": "too specific", "suggested_rewrite": "rewritten"}

    hooks.check_oob = rewrite
    row_id = await send_outbound(fake_pool, user, "rough", scope=_scope(user))
    assert sent == []
    assert fake_pool.messages[row_id]["content"] == "rough"
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"
    review = next(iter(fake_pool.withheld_outbound_reviews.values()))
    assert review["original_content"] == "rough"
    assert review["suggested_rewrite"] == "rewritten"
    assert review["verdict"] == "rewrite"

    async def block(pool, content, recipient, protected_owner_ids=None, *, bot_id, topic_id):
        assert bot_id == "mediator"
        assert topic_id is not None
        return {"verdict": "block", "reason": "blocked", "suggested_rewrite": None}

    hooks.check_oob = block
    row_id = await send_outbound(fake_pool, user, "blocked", scope=_scope(user))
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"


async def test_global_pause_default_withholds_and_ignore_pause_bypasses(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []

    async def send_text(to, body):
        sent.append(body)
        return {"messages": [{"id": f"wamid.{len(sent)}"}]}

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    await system_state.pause(fake_pool, user.id)

    withheld_id = await send_outbound(fake_pool, user, "ordinary", scope=_scope(user))
    sent_id = await send_outbound(fake_pool, user, "control", ignore_pause=True, scope=_scope(user))

    assert fake_pool.messages[withheld_id]["processing_state"] == "withheld"
    assert fake_pool.messages[sent_id]["processing_state"] == "processed"
    assert sent == ["control"]


async def test_send_outbound_passes_protected_owner_ids_and_withholds_current_user_leak(fake_pool, monkeypatch) -> None:
    user = _user(fake_pool)
    current_user_id = uuid4()
    protected_owner_ids = [current_user_id, user.id]
    _inbound(fake_pool, user, datetime.now(UTC) - timedelta(minutes=5))
    sent = []
    oob_calls = []

    async def send_text(to, body):
        sent.append(body)
        return {"messages": [{"id": "wamid.should-not-send"}]}

    async def block_current_user_leak(pool, content, recipient_id, protected_owner_ids=None, *, bot_id, topic_id):
        oob_calls.append((pool, content, recipient_id, protected_owner_ids, bot_id, topic_id))
        return {
            "verdict": "block",
            "reason": "current-user hard OOB",
            "suggested_rewrite": None,
            "checker_failed": False,
        }

    monkeypatch.setattr("app.services.whatsapp.send_text", send_text)
    hooks.check_oob = block_current_user_leak

    row_id = await send_outbound(fake_pool, user, "current-user protected detail", protected_owner_ids=protected_owner_ids, scope=_scope(user))

    assert sent == []
    assert len(oob_calls) == 1
    assert oob_calls[0][:5] == (fake_pool, "current-user protected detail", user.id, protected_owner_ids, "mediator")
    assert oob_calls[0][5] is not None
    assert fake_pool.messages[row_id]["processing_state"] == "withheld"


async def test_send_outbound_part_uses_runtime_part_key_for_idempotency(fake_pool, app_env, monkeypatch) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    fake_pool.users[user.id]["onboarding_state"] = "pending"
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
        "triggering_message_ids": [],
        "final_output_message_id": None,
    }
    sent = []

    async def send_text(to, body, *, send_typing_indicator=True, bot_id="mediator"):
        sent.append(body)
        return {"messages": [{"id": f"discord-{len(sent)}"}]}

    monkeypatch.setattr("app.services.discord.send_text", send_text)

    first = await send_outbound_part(
        fake_pool,
        user,
        "first part",
        bot_turn_id=turn_id,
        part_key=f"{turn_id}:1",
        part_index=1,
        scope=_scope(user),
    )
    second = await send_outbound_part(
        fake_pool,
        user,
        "first part",
        bot_turn_id=turn_id,
        part_key=f"{turn_id}:1",
        part_index=1,
        scope=_scope(user),
    )

    assert first["status"] == "sent"
    assert second["status"] == "duplicate"
    assert first["message_id"] == second["message_id"]
    assert sent == ["first part"]
    assert second["sent_so_far"] == ["first part"]
    assert fake_pool.users[user.id]["onboarding_state"] == "welcomed"
    get_settings.cache_clear()


async def test_send_message_part_paced_followup_uses_composition_and_rhythm(
    fake_pool,
    app_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    monkeypatch.setenv("DISCORD_MULTI_MESSAGE_DELAY_S", "1.1")
    monkeypatch.setenv("DISCORD_PACING_COMPOSITION_JITTER_RATIO", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _user(fake_pool)
    partner = _user(fake_pool)
    fake_pool.users[user.id]["pacing_preferences"] = {
        "answer_typing_min_s": 0.4,
        "answer_typing_max_s": 10,
        "answer_chars_per_s": 10,
        "max_typing_wait_s": 10,
    }
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
        "triggering_message_ids": [],
        "final_output_message_id": None,
    }
    sent = []
    typing_sent_at = []
    sleeps = []
    paced_calls = []

    def current_time() -> datetime:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += timedelta(seconds=seconds)

    async def send_typing(channel_id: str) -> None:
        typing_sent_at.append((channel_id, now))

    async def send_text(to, body, *, send_typing_indicator=True, bot_id="mediator"):
        sent.append((to, body, send_typing_indicator, list(sleeps)))
        return {"messages": [{"id": f"discord-{len(sent)}"}]}

    pacer = DiscordPacer(fake_pool, send_typing=send_typing, sleep=sleep, now=current_time)

    async def before_paced_send(answer_text: str, *, send_kind: str, part_index: int | None) -> None:
        paced_calls.append((answer_text, send_kind, part_index))
        await pacer.perform_send_typing(user, "channel-1", answer_text, send_kind=send_kind, part_index=part_index)

    ctx = TurnContext(
        turn_id=turn_id,
        pool=fake_pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        turn_started_at=now,
        incremental_sending_enabled=True,
        send_typing_indicator=False,
        before_paced_send=before_paced_send,
        sent_message_parts=[],
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=uuid4(),
    )
    monkeypatch.setattr("app.services.discord.send_text", send_text)

    first = await send_message_part(ctx, SendMessagePartInput(content="Six."))
    second = await send_message_part(ctx, SendMessagePartInput(content="x" * 30))

    assert first.status == "sent"
    assert second.status == "sent"
    assert paced_calls == [("Six.", "incremental_first", 1), ("x" * 30, "incremental_next", 2)]
    assert sleeps == pytest.approx([0.4, 1.1, 3.0])
    assert typing_sent_at[1][1] - typing_sent_at[0][1] == timedelta(seconds=1.5)
    assert sent == [
        (user.phone, "Six.", False, [0.4]),
        (user.phone, "x" * 30, False, [0.4, 1.1, 3.0]),
    ]
    get_settings.cache_clear()


async def test_send_message_part_interrupts_after_paced_wait_before_provider_send(
    fake_pool,
    app_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    started_at = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    user = _user(fake_pool)
    partner = _user(fake_pool)
    trigger_id = uuid4()
    fake_pool.messages[trigger_id] = {
        "id": trigger_id,
        "direction": "inbound",
        "sender_id": user.id,
        "recipient_id": None,
        "content": "first",
        "processing_state": "raw",
        "sent_at": started_at,
        "charge": "routine",
        "whatsapp_message_id": "discord-in",
        "media_type": None,
        "media_url": None,
        "media_duration_seconds": None,
        "media_analysis": None,
        "edit_history": None,
        "edited_at": None,
        "deleted_at": None,
    }
    sent = []
    paced_calls = []

    async def send_text(to, body, *, send_typing_indicator=True, bot_id="mediator"):
        sent.append(body)
        return {"messages": [{"id": "discord-out"}]}

    async def before_paced_send(answer_text: str, *, send_kind: str, part_index: int | None) -> None:
        paced_calls.append((answer_text, send_kind, part_index))
        newer_id = uuid4()
        fake_pool.messages[newer_id] = {
            "id": newer_id,
            "direction": "inbound",
            "sender_id": user.id,
            "recipient_id": None,
            "content": "wait one more thing",
            "processing_state": "raw",
            "sent_at": started_at + timedelta(milliseconds=1),
            "charge": "routine",
            "whatsapp_message_id": "discord-in-2",
            "media_type": None,
            "media_url": None,
            "media_duration_seconds": None,
            "media_analysis": None,
            "edit_history": None,
            "edited_at": None,
            "deleted_at": None,
        }

    ctx = TurnContext(
        turn_id=uuid4(),
        pool=fake_pool,
        user=user,
        partner=partner,
        triggering_message_ids=[trigger_id],
        turn_started_at=started_at,
        incremental_sending_enabled=True,
        send_typing_indicator=False,
        before_paced_send=before_paced_send,
        sent_message_parts=[],
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=uuid4(),
    )
    monkeypatch.setattr("app.services.discord.send_text", send_text)

    result = await send_message_part(ctx, SendMessagePartInput(content="stale part"))

    assert result.status == "interrupted"
    assert paced_calls == [("stale part", "incremental_first", 1)]
    assert sent == []
    assert [row for row in fake_pool.messages.values() if row.get("direction") == "outbound"] == []
    get_settings.cache_clear()


async def test_send_message_part_withholds_internal_process_narration(
    fake_pool,
    app_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    partner = _user(fake_pool)
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
        "triggering_message_ids": [],
        "final_output_message_id": None,
    }
    sent: list[str] = []

    async def send_text(to, body, *, send_typing_indicator=True, bot_id="mediator"):
        sent.append(body)
        return {"messages": [{"id": "discord-out"}]}

    monkeypatch.setattr("app.services.discord.send_text", send_text)

    ctx = TurnContext(
        turn_id=turn_id,
        pool=fake_pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        turn_started_at=datetime.now(UTC),
        incremental_sending_enabled=True,
        send_typing_indicator=False,
        before_paced_send=None,
        sent_message_parts=[],
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=uuid4(),
    )

    result = await send_message_part(
        ctx,
        SendMessagePartInput(
            content="**Memory `61ddbfdb`** — needs updating to include Hannah's agreement.\n\nLet me do those writes now."
        ),
    )

    assert result.status == "withheld"
    assert result.visible_to_user is False
    assert result.reason and "internal process" in result.reason
    assert sent == []
    get_settings.cache_clear()


async def test_send_message_part_withholds_step_transition_narration(
    fake_pool,
    app_env,
    monkeypatch,
) -> None:
    monkeypatch.setenv("MESSAGING_PROVIDER", "discord")
    from app.config import get_settings

    get_settings.cache_clear()
    user = _user(fake_pool)
    partner = _user(fake_pool)
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
        "triggering_message_ids": [],
        "final_output_message_id": None,
    }
    sent: list[str] = []

    async def send_text(to, body, *, send_typing_indicator=True, bot_id="mediator"):
        sent.append(body)
        return {"messages": [{"id": "discord-out"}]}

    monkeypatch.setattr("app.services.discord.send_text", send_text)

    ctx = TurnContext(
        turn_id=turn_id,
        pool=fake_pool,
        user=user,
        partner=partner,
        triggering_message_ids=[],
        turn_started_at=datetime.now(UTC),
        incremental_sending_enabled=True,
        send_typing_indicator=False,
        before_paced_send=None,
        sent_message_parts=[],
        bot_id="mediator",
        user_id=user.id,
        primary_topic_id=uuid4(),
    )

    result = await send_message_part(ctx, SendMessagePartInput(content="Now at the schedule step."))

    assert result.status == "withheld"
    assert result.visible_to_user is False
    assert result.reason and "internal process" in result.reason
    assert sent == []
    get_settings.cache_clear()
