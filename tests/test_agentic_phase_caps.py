"""Project A2: phase-aware tool-loop cap tests for ``run_step``.

Covers (per plan_v4 Step 9):
- read / consult cap → empty return, no exception, step advances
- respond cap with prior sent_message_parts → success early-stop
- respond cap with no prior output → in-run_step Anthropic emergency hop;
  on its success returns text; on its failure raises RespondCapNoOutput
  whose failure_reason maps to retryable_pre_send
- record / schedule cap → raises ``_PostSendPhaseCapExceeded`` (a private
  control marker) which the outer ``_run_agentic`` catches and writes a
  single ``turn_audit_events`` row.  ``bot_turns.failure_reason`` MUST stay
  NULL on those turns.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.models.user import User
from app.services import agentic
from app.services.agentic import (
    BoundedLoopExceeded,
    LLMPhaseError,
    RespondCapNoOutput,
    _PostSendPhaseCapExceeded,
    run_step,
)
from app.services.inbound_queue import FAILURE_REASON_TO_CLASS
from app.services.tools.registry import READ_PHASE_TOOLS, WRITE_PHASE_TOOLS
from app.services.turn_context import TurnContext
from tests.conftest import FakePool


pytestmark = pytest.mark.anyio


USAGE = {
    "input_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 2,
}


# ── fakes ───────────────────────────────────────────────────────────────────

def _tool_use(name: str = "update_turn_plan") -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(
                type="tool_use",
                id=f"toolu_{uuid4().hex[:6]}",
                name=name,
                input={},
            )
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(**USAGE),
    )


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(**USAGE),
    )


class _ScriptedMessages:
    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outcomes:
            raise AssertionError("unexpected provider call")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _ScriptedClient:
    def __init__(self, outcomes: list) -> None:
        self.messages = _ScriptedMessages(outcomes)


def _ctx(*, step: str = "read", sent_parts=None) -> TurnContext:
    pool = FakePool()
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    return TurnContext(
        turn_id=uuid4(),
        pool=pool,
        user=user,
        partner=None,
        triggering_message_ids=[uuid4()],
        bot_id="mediator",
        current_step=step,
        hot_context_rendered="ctx",
        sent_message_parts=sent_parts or [],
    )


@pytest.fixture(autouse=True)
def _reset_breaker():
    agentic._FALLBACK_BREAKER.reset()
    yield
    agentic._FALLBACK_BREAKER.reset()


@pytest.fixture
def _no_call_tool(monkeypatch):
    async def fake(name, args, ctx):  # noqa: ARG001
        return {"ok": True}

    monkeypatch.setattr(agentic, "call_tool", fake)


# ── read / consult caps ─────────────────────────────────────────────────────

@pytest.mark.parametrize("step", ["read", "consult"])
async def test_pre_send_caps_advance_silently(app_env, monkeypatch, _no_call_tool, step):
    """read and consult caps return empty text without raising."""
    ctx = _ctx(step=step)
    # Repeatedly return tool_use to force the cap path.
    client = _ScriptedClient([_tool_use(), _tool_use(), _tool_use()])
    text, _msgs, _count = await run_step(
        client,
        ctx,
        "system",
        "context",
        READ_PHASE_TOOLS | {"update_turn_plan"},
        [{"role": "user", "content": "hi"}],
        max_tool_iterations=1,
    )
    assert text == ""


# ── respond cap ─────────────────────────────────────────────────────────────

async def test_respond_cap_with_prior_send_returns_empty(
    app_env, monkeypatch, _no_call_tool
):
    """If sent_message_parts already has user-visible output, respond cap is
    treated as a successful early-stop: empty return, no raise.
    """
    ctx = _ctx(step="respond", sent_parts=[{"message_id": uuid4(), "content": "hi"}])
    client = _ScriptedClient([_tool_use(), _tool_use(), _tool_use()])
    text, _msgs, _count = await run_step(
        client,
        ctx,
        "system",
        "context",
        WRITE_PHASE_TOOLS | {"update_turn_plan"},
        [{"role": "user", "content": "respond"}],
        max_tool_iterations=1,
    )
    assert text == ""


async def test_respond_cap_no_prior_output_emergency_hop_succeeds(
    app_env, monkeypatch, _no_call_tool
):
    """Cap hit with NO sent_message_parts → emergency Anthropic-only hop;
    on its success the text it returns flows back to the caller."""
    ctx = _ctx(step="respond")
    # Primary returns tool_use enough times to overshoot the cap.
    primary = _ScriptedClient([_tool_use(), _tool_use(), _tool_use()])
    # The emergency hop builds a fresh client via the patched anthropic factory.
    emergency = _ScriptedClient([_text("emergency reply")])
    monkeypatch.setattr(
        agentic.anthropic, "AsyncAnthropic", lambda api_key=None: emergency
    )
    # Also patch DeepSeekClient just in case the chain is misread.
    monkeypatch.setattr(agentic, "DeepSeekClient", lambda: emergency)

    text, _msgs, _count = await run_step(
        primary,
        ctx,
        "system",
        "context",
        WRITE_PHASE_TOOLS | {"update_turn_plan"},
        [{"role": "user", "content": "respond"}],
        max_tool_iterations=1,
    )
    assert text == "emergency reply"


async def test_respond_cap_no_prior_output_emergency_hop_fails_raises_respond_cap_no_output(
    app_env, monkeypatch, _no_call_tool
):
    ctx = _ctx(step="respond")
    primary = _ScriptedClient([_tool_use(), _tool_use(), _tool_use()])

    # Emergency hop fails on Anthropic too.
    class _FakeErr(Exception):
        status_code = 500

        def __init__(self) -> None:
            super().__init__("boom")
            self.response = SimpleNamespace(status_code=500, headers={})

    failing = _ScriptedClient([_FakeErr(), _FakeErr()])
    monkeypatch.setattr(
        agentic.anthropic, "AsyncAnthropic", lambda api_key=None: failing
    )

    with pytest.raises(RespondCapNoOutput) as excinfo:
        await run_step(
            primary,
            ctx,
            "system",
            "context",
            WRITE_PHASE_TOOLS | {"update_turn_plan"},
            [{"role": "user", "content": "respond"}],
            max_tool_iterations=1,
        )
    assert excinfo.value.failure_reason == "respond_cap_no_output"
    assert FAILURE_REASON_TO_CLASS["respond_cap_no_output"] == "retryable_pre_send"


# ── record / schedule caps ──────────────────────────────────────────────────

@pytest.mark.parametrize("step", ["record", "schedule"])
async def test_post_send_caps_raise_internal_marker(
    app_env, monkeypatch, _no_call_tool, step
):
    """record / schedule cap raises ``_PostSendPhaseCapExceeded`` (private
    marker); it is NOT in FAILURE_REASON_TO_CLASS and the outer handler
    catches it locally."""
    ctx = _ctx(step=step)
    client = _ScriptedClient([_tool_use(), _tool_use(), _tool_use()])

    with pytest.raises(_PostSendPhaseCapExceeded) as excinfo:
        await run_step(
            client,
            ctx,
            "system",
            "context",
            WRITE_PHASE_TOOLS | {"update_turn_plan"},
            [{"role": "user", "content": step}],
            max_tool_iterations=1,
        )
    assert excinfo.value.step == step
    assert excinfo.value.cap == 1
    # Confirm the marker is NOT registered as a public failure_reason.
    assert (
        "post_send_phase_cap_exceeded" not in FAILURE_REASON_TO_CLASS
    )
    # The marker subclasses BoundedLoopExceeded but is treated locally.
    assert isinstance(excinfo.value, BoundedLoopExceeded)


# ── failure_reason mapping sanity (catches drift) ───────────────────────────

def test_a2_failure_reasons_all_map_to_retryable_pre_send(app_env):
    for reason in (
        "provider_fallback_killed",
        "same_provider_fallback_noop",
        "fallback_breaker_open",
        "respond_cap_no_output",
    ):
        assert FAILURE_REASON_TO_CLASS[reason] == "retryable_pre_send"


def test_unsupported_chain_maps_to_infra_bug(app_env):
    assert (
        FAILURE_REASON_TO_CLASS["unsupported_chain_anthropic_to_deepseek"]
        == "infra_bug"
    )


# ── max_tool_calls cap tests ─────────────────────────────────────────────────


class TestMaxToolCallsCap:
    """Verify that MaxToolCallsExceeded is importable and carries expected
    attributes.  Full cap-integration tests go through run_agentic_nonchat_job
    (see test_nonchat_agentic.py)."""

    def test_max_tool_calls_importable_and_has_attributes(self) -> None:
        """MaxToolCallsExceeded is importable and has expected attributes."""
        from app.services.agentic import MaxToolCallsExceeded

        exc = MaxToolCallsExceeded(
            "cap exceeded",
            tool_call_count=5,
            max_calls=500,
        )
        assert exc.tool_call_count == 5
        assert exc.max_calls == 500
        assert "cap exceeded" in str(exc)

    def test_max_tool_calls_zero_default(self) -> None:
        """Default tool_call_count is 0 when not provided."""
        from app.services.agentic import MaxToolCallsExceeded

        exc = MaxToolCallsExceeded("test")
        assert exc.tool_call_count == 0
        assert exc.max_calls == 0

    def test_max_tool_calls_instance_is_exception(self) -> None:
        """MaxToolCallsExceeded is a proper Exception subclass."""
        from app.services.agentic import MaxToolCallsExceeded

        exc = MaxToolCallsExceeded("test")
        assert isinstance(exc, Exception)


async def test_max_tool_calls_cap_builds_error_result_before_raising(
    app_env, monkeypatch, _no_call_tool
) -> None:
    """The cap path must raise MaxToolCallsExceeded, not NameError."""
    from app.services.agentic import MaxToolCallsExceeded

    ctx = _ctx(step="live_debrief")
    client = _ScriptedClient([_tool_use("log_observation")])

    with pytest.raises(MaxToolCallsExceeded) as exc_info:
        await run_step(
            client,
            ctx,
            "system",
            "context",
            {"log_observation", "update_turn_plan"},
            [{"role": "user", "content": "hi"}],
            max_tool_calls=0,
        )

    exc = exc_info.value
    assert exc.tool_call_count == 0
    assert exc.max_calls == 0
    assert "tool_result" in json.dumps(exc.messages, default=str)
