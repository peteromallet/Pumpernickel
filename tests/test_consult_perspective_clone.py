"""S4 T13 — consult_perspective clone enumeration test.

The consult_perspective tool forks the per-turn context. Historically it did
this by reconstructing a TurnContext field-by-field, which silently dropped
any newly added field — the easiest authorization leak path in the codebase.
T12 rewrote it to use replace_ctx (dataclasses.replace). This test enforces:

1. For a fully-populated source TurnContext (including non-None
   read_scopes/write_scopes/cross_topic_policy), the cloned ctx matches the
   source on every dataclasses.fields(TurnContext) entry EXCEPT the eight
   documented overrides.
2. Scanning app/services/tools/consult_perspective.py source: zero
   `TurnContext(` constructor tokens remain.
"""

from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path
from uuid import uuid4

import pytest

from app.bots.base import ReadScopes, WriteScopes
from app.models.user import User
from app.services.turn_context import TurnContext, replace_ctx
from app.services.turn_plan import make_turn_plan


_DOCUMENTED_OVERRIDES = {
    "current_step",
    "incremental_sending_enabled",
    "send_typing_indicator",
    "before_paced_send",
    "sent_message_parts",
    "triggering_message_ids",
    "protected_owner_ids",
    "trigger_metadata",
}


def _user(name: str) -> User:
    return User(id=uuid4(), name=name, phone="+1", timezone="UTC")


def _full_ctx() -> TurnContext:
    user_a = _user("A")
    user_b = _user("B")
    return TurnContext(
        turn_id=uuid4(),
        pool=object(),
        user=user_a,
        partner=user_b,
        triggering_message_ids=[uuid4()],
        bot_id="mediator",
        bot_spec=None,
        binding_id=uuid4(),
        participants_shape="dyad",
        primary_topic_id=uuid4(),
        primary_topic_slug="relationship",
        channel_id="discord/123",
        read_scopes=ReadScopes(
            topics=frozenset({"own"}),
            allow_cross_topic_peek=True,
            allow_cross_topic_status_injection=True,
        ),
        write_scopes=WriteScopes(
            topics=frozenset({"relationship"}),
            require_reason_for_cross_topic=True,
        ),
        cross_topic_policy="peek",
        dyad_id=uuid4(),
        current_step="respond",
        turn_plan=make_turn_plan("quick_reply"),
        tool_call_log=["search_messages"],
        trigger_charge="routine",
        explicit_partner_alert_requested=False,
        turn_started_at=None,
        incremental_sending_enabled=True,
        protected_owner_ids=[uuid4(), uuid4()],
        send_typing_indicator=True,
        before_paced_send=None,
        sent_message_parts=[{"some": "thing"}],
        hot_context_rendered="hot context body",
        trigger_metadata={"kind": "inbound", "foo": "bar"},
    )


def test_replace_ctx_preserves_every_field_except_documented_overrides() -> None:
    src = _full_ctx()
    cloned = replace_ctx(
        src,
        current_step="consult",
        incremental_sending_enabled=False,
        send_typing_indicator=False,
        before_paced_send=None,
        sent_message_parts=[],
        triggering_message_ids=list(src.triggering_message_ids),
        protected_owner_ids=list(src.protected_owner_ids or []),
        trigger_metadata={**dict(src.trigger_metadata), "_inside_consult": True},
    )

    fields = {f.name for f in dataclasses.fields(TurnContext)}
    assert fields, "TurnContext must expose dataclass fields"
    for name in fields:
        if name in _DOCUMENTED_OVERRIDES:
            continue
        src_val = getattr(src, name)
        clone_val = getattr(cloned, name)
        assert clone_val is src_val or clone_val == src_val, (
            f"clone dropped/mutated field {name!r}: src={src_val!r} clone={clone_val!r}"
        )

    # Sanity-check the override semantics.
    assert cloned.current_step == "consult"
    assert cloned.incremental_sending_enabled is False
    assert cloned.send_typing_indicator is False
    assert cloned.before_paced_send is None
    assert cloned.sent_message_parts == []
    assert cloned.trigger_metadata.get("_inside_consult") is True


def test_consult_perspective_source_contains_no_turncontext_constructor() -> None:
    path = Path(__file__).resolve().parents[1] / "app" / "services" / "tools" / "consult_perspective.py"
    src = path.read_text()
    assert "TurnContext(" not in src, (
        "consult_perspective.py must use replace_ctx; found a TurnContext( constructor"
    )
