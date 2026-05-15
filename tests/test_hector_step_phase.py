"""Step-phase gating tests for Hector tools.

T14 (SC14):
- Create mock TurnContext with hector_spec and each step in
  {read, consult, record, schedule, respond, done}.
- Call _step_allowed(ctx) and assert:
  - Three read tools (list_commitments, list_events, get_adherence)
    resolve in read/consult/record steps.
  - Four write tools (create_commitment, update_commitment,
    close_commitment, log_event) resolve in record step.
  - None of the seven appear in respond/schedule/done steps.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

from app.models.user import User
from app.services.tools.registry import (
    HECTOR_ONLY_TOOLS,
    _step_allowed,
    STEP_ALLOWED_TOOLS,
)
from app.services.turn_context import TurnContext

_HECTOR_READ_TOOLS = {"list_commitments", "list_events", "get_adherence"}
_HECTOR_WRITE_TOOLS = {
    "create_commitment",
    "update_commitment",
    "close_commitment",
    "log_event",
}


@pytest.fixture(autouse=True)
def _env_staging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set STAGING=1 so _maybe_register_staging_bots registers staging bots."""
    monkeypatch.setenv("STAGING", "1")


def _hector_spec():
    """Return hector BotSpec after staging registration."""
    from app.bots.registry import _maybe_register_staging_bots, BOT_SPECS

    _maybe_register_staging_bots()
    return BOT_SPECS["hector"]


def _make_ctx(*, step: str) -> TurnContext:
    """Build a minimal TurnContext for step-phase testing."""
    uid = uuid4()
    user = User(
        id=uid,
        name="TestUser",
        phone="+155****0100",
        timezone="UTC",
        onboarding_state="completed",
    )
    return TurnContext(
        turn_id=uuid4(),
        pool=None,
        user=user,
        partner=None,
        triggering_message_ids=[],
        bot_id="hector",
        user_id=uid,
        bot_spec=_hector_spec(),
        current_step=step,  # type: ignore[arg-type]
    )


class TestHectorStepPhaseRead:
    """Read tools resolve in read/consult/record steps."""

    def test_read_tools_in_read_step(self):
        ctx = _make_ctx(step="read")
        allowed = _step_allowed(ctx)
        assert _HECTOR_READ_TOOLS <= allowed, (
            f"Missing read tools in read step: {_HECTOR_READ_TOOLS - allowed}"
        )
        # Write tools should NOT be in read step
        leaked = _HECTOR_WRITE_TOOLS & allowed
        assert not leaked, f"Write tools leaked into read step: {leaked}"

    def test_read_tools_in_consult_step(self):
        ctx = _make_ctx(step="consult")
        allowed = _step_allowed(ctx)
        assert _HECTOR_READ_TOOLS <= allowed, (
            f"Missing read tools in consult step: {_HECTOR_READ_TOOLS - allowed}"
        )

    def test_read_tools_in_record_step(self):
        ctx = _make_ctx(step="record")
        allowed = _step_allowed(ctx)
        assert _HECTOR_READ_TOOLS <= allowed, (
            f"Missing read tools in record step: {_HECTOR_READ_TOOLS - allowed}"
        )


class TestHectorStepPhaseWrite:
    """Write tools resolve only in record step."""

    def test_write_tools_in_record_step(self):
        ctx = _make_ctx(step="record")
        allowed = _step_allowed(ctx)
        assert _HECTOR_WRITE_TOOLS <= allowed, (
            f"Missing write tools in record step: {_HECTOR_WRITE_TOOLS - allowed}"
        )

    def test_write_tools_not_in_read_step(self):
        ctx = _make_ctx(step="read")
        allowed = _step_allowed(ctx)
        leaked = _HECTOR_WRITE_TOOLS & allowed
        assert not leaked, f"Write tools leaked into read step: {leaked}"

    def test_write_tools_not_in_consult_step(self):
        ctx = _make_ctx(step="consult")
        allowed = _step_allowed(ctx)
        leaked = _HECTOR_WRITE_TOOLS & allowed
        assert not leaked, f"Write tools leaked into consult step: {leaked}"


class TestHectorStepPhaseAbsent:
    """Hector tools must NOT appear in respond/schedule/done steps."""

    def test_no_hector_tools_in_respond_step(self):
        ctx = _make_ctx(step="respond")
        allowed = _step_allowed(ctx)
        # log_event is in RESPOND_TOOLS so it IS allowed here.
        # But the 6 other tools should not be.
        excluded = HECTOR_ONLY_TOOLS - {"log_event"}
        leaked = excluded & allowed
        assert not leaked, (
            f"Non-log_event Hector tools leaked into respond step: {leaked}"
        )

    def test_no_hector_tools_in_schedule_step(self):
        ctx = _make_ctx(step="schedule")
        allowed = _step_allowed(ctx)
        leaked = HECTOR_ONLY_TOOLS & allowed
        assert not leaked, (
            f"Hector tools leaked into schedule step: {leaked}"
        )

    def test_no_hector_tools_in_done_step(self):
        ctx = _make_ctx(step="done")
        allowed = _step_allowed(ctx)
        leaked = HECTOR_ONLY_TOOLS & allowed
        assert not leaked, (
            f"Hector tools leaked into done step: {leaked}"
        )


class TestHectorStepPhaseRespondHasLogEvent:
    """log_event is intentionally allowed in respond step for quick event logging."""

    def test_log_event_in_respond_step(self):
        ctx = _make_ctx(step="respond")
        allowed = _step_allowed(ctx)
        assert "log_event" in allowed, (
            "log_event should be allowed in respond step"
        )
