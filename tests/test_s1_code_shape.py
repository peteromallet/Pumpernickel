"""Sprint 1 code-shape tests — TurnContext defaults, BotSpec defaults, __all__ exports.

Does NOT modify conftest.py or any dirty test file.
"""

from __future__ import annotations

import ast
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.turn_context import TurnContext


# ---------------------------------------------------------------------------
# TurnContext defaults
# ---------------------------------------------------------------------------

def _dummy_user(name: str = "testuser") -> User:
    return User(
        id=uuid4(),
        name=name,
        phone="+15550001111",
        timezone="America/New_York",
    )


def test_turn_context_defaults_all_none():
    """Construct TurnContext with minimal positional args; all new fields default to None."""
    user = _dummy_user("alice")
    partner = _dummy_user("bob")
    turn_id = uuid4()

    ctx = TurnContext(
        turn_id=turn_id,
        pool=None,
        user=user,
        partner=partner,
        triggering_message_ids=[],
    )

    assert ctx.bot_id is None
    assert ctx.transport is None
    assert ctx.user_id is None
    assert ctx.bot_spec is None
    assert ctx.binding_id is None
    assert ctx.participants_shape is None
    assert ctx.primary_topic_id is None
    assert ctx.primary_topic_slug is None
    assert ctx.channel_id is None
    assert ctx.read_scopes is None
    assert ctx.write_scopes is None
    assert ctx.cross_topic_policy is None


def test_turn_context_partner_can_be_none():
    """TurnContext.partner accepts None (type was widened to User | None)."""
    user = _dummy_user("alice")
    turn_id = uuid4()

    ctx = TurnContext(
        turn_id=turn_id,
        pool=None,
        user=user,
        partner=None,
        triggering_message_ids=[],
    )

    assert ctx.partner is None


def test_turn_context_partner_accepts_user():
    """TurnContext.partner still accepts a User (backward compatible)."""
    user = _dummy_user("alice")
    partner = _dummy_user("bob")
    turn_id = uuid4()

    ctx = TurnContext(
        turn_id=turn_id,
        pool=None,
        user=user,
        partner=partner,
        triggering_message_ids=[],
    )

    assert ctx.partner is partner
    assert ctx.partner.name == "bob"


def test_turn_context_new_fields_positional_preserved():
    """Positional construction still works — new fields don't shift positions 1-5."""
    user = _dummy_user("alice")
    partner = _dummy_user("bob")
    turn_id = uuid4()
    msg_ids = [uuid4(), uuid4()]

    ctx = TurnContext(turn_id, None, user, partner, msg_ids)

    assert ctx.turn_id == turn_id
    assert ctx.user is user
    assert ctx.partner is partner
    assert ctx.triggering_message_ids == msg_ids
    # New fields default to None
    assert ctx.bot_id is None


def test_turn_context_field_count():
    """TurnContext has the correct number of fields (existing + identity fields)."""
    from dataclasses import fields

    field_names = {f.name for f in fields(TurnContext)}
    # Pre-S1: turn_id, pool, user, partner, triggering_message_ids,
    #          current_step, turn_plan, tool_call_log, trigger_charge,
    #          explicit_partner_alert_requested, turn_started_at,
    #          incremental_sending_enabled, protected_owner_ids,
    #          send_typing_indicator, before_paced_send,
    #          sent_message_parts, hot_context_rendered, trigger_metadata = 18
    # S1 adds: bot_id, bot_spec, binding_id, participants_shape,
    #           primary_topic_id, primary_topic_slug, channel_id,
    #           read_scopes, write_scopes, cross_topic_policy = 10
    # S2a adds: dyad_id = 1
    # InboundScope refactor adds: transport, user_id = 2
    # Later agentic plumbing adds: flat_allowed_tools, hot_context_window_edge = 2
    # S2 live prep adds: extras = 1
    # Total: 34
    assert len(field_names) == 34
    assert "bot_id" in field_names
    assert "transport" in field_names
    assert "user_id" in field_names
    assert "cross_topic_policy" in field_names
    assert "flat_allowed_tools" in field_names
    assert "hot_context_window_edge" in field_names


# ---------------------------------------------------------------------------
# BotSpec + scope dataclasses
# ---------------------------------------------------------------------------

def test_bot_spec_defaults():
    """BotSpec defaults match mediator shape."""
    from app.bots.base import BotSpec, ReadScopes, WriteScopes

    # Minimal BotSpec (just required fields)
    def dummy_renderer(**kwargs):
        return ""

    spec = BotSpec(
        bot_id="test-bot",
        prompt_renderer=dummy_renderer,
        step_instructions={},
    )

    assert spec.display_name == "Mediator"
    assert spec.primary_topic_slug == "relationship"
    assert spec.participants_shape == "dyad"
    assert isinstance(spec.read_scopes, ReadScopes)
    assert isinstance(spec.write_scopes, WriteScopes)
    assert spec.bot_spec_version == "1.1.0"
    assert spec.hot_context_builder_version == "1.0.0"
    assert spec.tool_schema_version == "1.0.0"


def test_read_scopes_default():
    """ReadScopes default: allow_cross_topic_status_injection=False."""
    from app.bots.base import ReadScopes

    scopes = ReadScopes()
    assert scopes.allow_cross_topic_status_injection is False


def test_read_scopes_custom():
    """ReadScopes can be constructed with custom values."""
    from app.bots.base import ReadScopes

    scopes = ReadScopes(allow_cross_topic_status_injection=True)
    assert scopes.allow_cross_topic_status_injection is True


def test_read_scopes_frozen():
    """ReadScopes is frozen — cannot mutate after construction."""
    from app.bots.base import ReadScopes

    scopes = ReadScopes()
    with pytest.raises(Exception):
        scopes.allow_cross_topic_status_injection = True  # type: ignore[misc]


def test_write_scopes_default():
    """WriteScopes constructs without error."""
    from app.bots.base import WriteScopes

    scopes = WriteScopes()
    assert scopes is not None


def test_write_scopes_frozen():
    """WriteScopes is frozen."""
    from app.bots.base import WriteScopes

    scopes = WriteScopes()
    with pytest.raises(Exception):
        scopes.some_field = True  # type: ignore[attr-defined]


def test_bot_spec_custom_scopes():
    """BotSpec accepts custom scopes."""
    from app.bots.base import BotSpec, ReadScopes, WriteScopes

    def dummy_renderer(**kwargs):
        return ""

    custom_read = ReadScopes(allow_cross_topic_status_injection=True)
    spec = BotSpec(
        bot_id="custom-bot",
        prompt_renderer=dummy_renderer,
        step_instructions={},
        read_scopes=custom_read,
    )

    assert spec.read_scopes.allow_cross_topic_status_injection is True


# ---------------------------------------------------------------------------
# __all__ exports
# ---------------------------------------------------------------------------

def test_read_scopes_in_all():
    """ReadScopes is exported from app.bots.__init__.__all__."""
    import app.bots

    assert "ReadScopes" in app.bots.__all__


def test_write_scopes_in_all():
    """WriteScopes is exported from app.bots.__init__.__all__."""
    import app.bots

    assert "WriteScopes" in app.bots.__all__


def test_bot_spec_in_all():
    """BotSpec is exported from app.bots.__init__.__all__."""
    import app.bots

    assert "BotSpec" in app.bots.__all__


def test_inbound_scope_import_topology_has_single_canonical_module():
    """InboundScope must not be re-exported from inbound.py.

    Regression guard: downstream imports should depend on app.services.scope,
    otherwise the old inbound-owned ResolvedScope shape can creep back in and
    create import cycles.
    """
    root = Path(__file__).resolve().parents[1]
    offenders: list[str] = []
    for path in [*root.joinpath("app").rglob("*.py"), *root.joinpath("tests").rglob("*.py")]:
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.module == "app.services.inbound" and any(alias.name == "InboundScope" for alias in node.names):
                offenders.append(str(path.relative_to(root)))

    assert offenders == []
