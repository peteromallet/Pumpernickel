"""T17 — SuperPOM reflection routing and prompt integration tests.

Verifies:
- Reflection tools excluded from SuperPOM allowlist
- Prompt profile hides internals, avoids proactive invitations
- pick_default_skeleton routes likely reflections to "standard" skeleton
- Non-reflection SuperPOM messages keep original skeleton selection
- Other bots are unaffected by reflection routing
"""

from __future__ import annotations

import pytest

from app.bots.superpom import build_superpom_spec
from app.bots.ids import SUPERPOM_BOT_ID
from app.services.turn_plan import pick_default_skeleton


# ── Tool allowlist tests ──────────────────────────────────────────────────


def test_superpom_spec_excludes_reflection_tools():
    """Reflection tools must not be in SuperPOM's allowlist."""
    spec = build_superpom_spec()
    assert spec.tool_allowlist is not None
    excluded = {
        "list_reflections",
        "get_reflection",
        "finalize_reflection",
        "correct_reflection",
    }
    for tool_name in excluded:
        assert tool_name not in spec.tool_allowlist, (
            f"Reflection tool {tool_name!r} should be excluded from "
            f"SuperPOM allowlist but was found"
        )


def test_superpom_spec_keeps_orientation_tools():
    """Orientation tools must remain in SuperPOM's allowlist."""
    spec = build_superpom_spec()
    orient_tools = {
        "list_orientation_items",
        "get_orientation_item",
        "create_orientation_item",
        "update_orientation_item",
        "review_orientation_item",
        "close_orientation_item",
        "link_orientation_evidence",
    }
    for tool_name in orient_tools:
        assert tool_name in spec.tool_allowlist, (
            f"Orientation tool {tool_name!r} must be in SuperPOM allowlist"
        )


def test_superpom_spec_keeps_memory_observation_tools():
    """Memory and observation tools must remain so the bot can write durable state."""
    spec = build_superpom_spec()
    durable_tools = {
        "add_memory",
        "update_memory",
        "log_observation",
        "update_observation",
    }
    for tool_name in durable_tools:
        assert tool_name in spec.tool_allowlist, (
            f"Durable-state tool {tool_name!r} must be in SuperPOM allowlist"
        )


# ── Prompt profile tests ──────────────────────────────────────────────────


def _rendered_prompt(assistant_name: str = "SuperPOM", user_name: str = "TestUser") -> str:
    from app.bots.prompts.profiles.superpom import PROFILE
    from app.bots.prompts.profile import render_profile

    return render_profile(PROFILE, assistant_name=assistant_name, user_name=user_name)


def test_prompt_does_not_mention_reflection_tools():
    """The prompt must not mention reflection tool names — internals stay hidden."""
    rendered = _rendered_prompt()
    forbidden = [
        "list_reflections",
        "get_reflection",
        "finalize_reflection",
        "correct_reflection",
    ]
    for tool in forbidden:
        assert tool not in rendered, (
            f"Reflection tool {tool!r} must not appear in the SuperPOM prompt"
        )


def test_prompt_does_not_proactively_invite_reflections():
    """No proactive reflection invitation language in the prompt.

    The prompt may mention reflection capture or negation contexts
    (e.g. 'Do not invite the user to start a reflection'), but it
    must never proactively invite the user to reflect.
    """
    rendered = _rendered_prompt().lower()
    # These phrases in an *invitation* context would be problematic.
    # Check that they only appear in negation/rejection contexts.
    _check_not_proactive(rendered, "start a reflection")
    _check_not_proactive(rendered, "begin a reflection")
    _check_not_proactive(rendered, "let's reflect")
    _check_not_proactive(rendered, "shall we reflect")
    _check_not_proactive(rendered, "schedule a reflection")
    _check_not_proactive(rendered, "set up a reflection")
    _check_not_proactive(rendered, "daily reflection")
    _check_not_proactive(rendered, "weekly reflection")


def _check_not_proactive(rendered: str, phrase: str) -> None:
    """Ensure a phrase only appears in negation context, not as invitation."""
    idx = rendered.find(phrase)
    if idx == -1:
        return  # not present at all — fine
    # Check nearby context for negation markers
    nearby = rendered[max(0, idx - 80) : idx + len(phrase) + 20]
    negation_markers = ["do not", "don't", "not", "never", "without", "avoid"]
    is_negated = any(marker in nearby for marker in negation_markers)
    assert is_negated, (
        f"Phrase {phrase!r} appears without negation context. "
        f"Nearby text: {nearby!r}"
    )


def test_prompt_mentions_reflection_capture_is_automatic():
    """The prompt should note that reflection capture is automatic and silent."""
    rendered = _rendered_prompt()
    assert "Reflection capture is automatic" in rendered, (
        "Prompt should state that reflection capture is automatic"
    )
    assert "do not need to trigger" in rendered.lower() or (
        "Do not invite" in rendered
    )


def test_prompt_still_contains_forward_motion_language():
    """Core SuperPOM identity language unchanged."""
    rendered = _rendered_prompt()
    assert "forward motion" in rendered.lower()
    assert "action catalyst" in rendered.lower()


def test_prompt_does_not_mention_internals():
    """No internal reflection mechanics in the prompt."""
    rendered = _rendered_prompt().lower()
    internals = [
        "classification",
        "ledger",
        "derivation",
        "normalization",
        "structured payload",
        "include_internals",
    ]
    for internal_term in internals:
        assert internal_term not in rendered, (
            f"Internal term {internal_term!r} must not appear in SuperPOM prompt"
        )


# ── Skeleton routing tests ────────────────────────────────────────────────


def _trigger_meta(text: str, *, kind: str = "inbound") -> dict:
    return {"kind": kind, "messages": [{"content": text}]}


def _signals(*, bot_id: str = "superpom") -> dict:
    return {"bot_id": bot_id}


def test_likely_reflection_text_routes_to_standard():
    """Text classified as a likely reflection → standard skeleton for SuperPOM."""
    # Explicit reflection language should route to standard.
    # (Avoid "to do" substring which triggers the task-negative classifier.)
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta(
            "Time for my weekly reflection — let's look back at this week"
        ),
        charge=None,
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "standard", (
        f"Expected 'standard' skeleton for reflection text, got {result!r}"
    )


def test_introspective_text_routes_to_standard():
    """Introspective content → standard skeleton for SuperPOM."""
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta(
            "I feel like I've noticed a pattern in how I approach decisions"
        ),
        charge=None,
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "standard", (
        f"Expected 'standard' skeleton for introspective text, got {result!r}"
    )


def test_compass_review_text_routes_to_standard():
    """Compass-review/checkpoint language → standard skeleton for SuperPOM."""
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta(
            "Let's do a checkpoint on my goals this month"
        ),
        charge=None,
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "standard", (
        f"Expected 'standard' skeleton for checkpoint text, got {result!r}"
    )


def test_non_reflection_text_stays_quick_reply():
    """Ordinary non-reflection SuperPOM text keeps quick_reply skeleton."""
    # Simple acknowledgement should stay quick_reply
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("ok, thanks"),
        charge=None,
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result in ("quick_reply", "silence_or_react"), (
        f"Expected quick_reply/silence_or_react for ack text, got {result!r}"
    )


def test_greeting_text_stays_quick_reply():
    """Greetings keep quick_reply skeleton."""
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("hello"),
        charge=None,
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "quick_reply", (
        f"Expected quick_reply for greeting, got {result!r}"
    )


def test_task_text_stays_quick_reply():
    """Non-reflection logistics text stays quick_reply (not a reflection).

    Uses logistics text that doesn't match CHECKIN_CONFIRM_RE or other
    patterns that independently force 'standard'.
    """
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("what's the weather like today"),
        charge=None,
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "quick_reply", (
        f"Expected quick_reply for non-reflection logistics text, got {result!r}"
    )


def test_joke_text_stays_quick_reply():
    """Joke text stays quick_reply."""
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("haha just kidding about the reflection thing"),
        charge=None,
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "quick_reply", (
        f"Expected quick_reply for joke text, got {result!r}"
    )


def test_charged_override_still_works():
    """Charged override takes precedence over reflection routing."""
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("I want to do a weekly reflection"),
        charge="charged",
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "charged", (
        f"Expected 'charged' skeleton (charge overrides reflection), got {result!r}"
    )


def test_crisis_override_still_works():
    """Crisis override takes precedence over reflection routing."""
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("I need to reflect on everything"),
        charge="crisis",
        hot_context_signals=_signals(bot_id="superpom"),
    )
    assert result == "crisis", (
        f"Expected 'crisis' skeleton (charge overrides reflection), got {result!r}"
    )


def test_other_bots_are_unaffected():
    """Reflection routing only affects SuperPOM — other bots unchanged."""
    # Hector with reflection-like text should still get quick_reply
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("I want to do a weekly reflection"),
        charge=None,
        hot_context_signals=_signals(bot_id="hector"),
    )
    assert result == "quick_reply", (
        f"Hector should not be affected by reflection routing, got {result!r}"
    )

    # Mediator with reflection-like text should still get quick_reply
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("Let's reflect on our relationship"),
        charge=None,
        hot_context_signals=_signals(bot_id="mediator"),
    )
    assert result == "quick_reply", (
        f"Mediator should not be affected by reflection routing, got {result!r}"
    )


def test_no_bot_id_no_reflection_routing():
    """When bot_id is not in signals, reflection routing does not apply."""
    result = pick_default_skeleton(
        trigger_metadata=_trigger_meta("I want to do a weekly reflection"),
        charge=None,
        hot_context_signals=None,
    )
    # Without bot_id, should fall through to quick_reply
    assert result == "quick_reply", (
        f"Without bot_id, expected quick_reply, got {result!r}"
    )


# ── RECORD step instruction tests ─────────────────────────────────────────


def test_record_step_instruction_warns_against_reflection_tools():
    """The RECORD step instruction tells the bot not to use reflection tools."""
    from app.bots.superpom import SUPERPOM_RECORD_INSTRUCTION

    assert "Do not use reflection tools" in SUPERPOM_RECORD_INSTRUCTION
    assert "list_reflections" in SUPERPOM_RECORD_INSTRUCTION
    assert "capture runs automatically" in SUPERPOM_RECORD_INSTRUCTION
    assert "should not be duplicated" in SUPERPOM_RECORD_INSTRUCTION


def test_respond_step_unchanged():
    """The RESPOND step instruction remains unchanged — no internals leaked."""
    from app.bots.superpom import SUPERPOM_RESPOND_INSTRUCTION

    # Must not mention reflection tools or internals
    assert "list_reflections" not in SUPERPOM_RESPOND_INSTRUCTION
    assert "get_reflection" not in SUPERPOM_RESPOND_INSTRUCTION
    assert "finalize_reflection" not in SUPERPOM_RESPOND_INSTRUCTION
    assert "correct_reflection" not in SUPERPOM_RESPOND_INSTRUCTION
    assert "classification" not in SUPERPOM_RESPOND_INSTRUCTION.lower()

    # Core behavior unchanged
    assert "sharp" in SUPERPOM_RESPOND_INSTRUCTION.lower()
    assert "plain" in SUPERPOM_RESPOND_INSTRUCTION.lower()


def test_read_step_unchanged():
    """The READ step instruction remains unchanged."""
    from app.bots.superpom import SUPERPOM_READ_INSTRUCTION

    assert "Compass" in SUPERPOM_READ_INSTRUCTION
    assert "list_orientation_items" in SUPERPOM_READ_INSTRUCTION


def test_schedule_step_unchanged():
    """The SCHEDULE step instruction remains unchanged — no proactive reflection scheduling.

    The original text mentions 'reflection practice' in a passive context
    ('help the user's reflection practice survive the week') which is
    pre-existing and acceptable — it refers to the user's own practice,
    not a scheduled bot-initiated reflection.
    """
    from app.bots.superpom import SUPERPOM_SCHEDULE_INSTRUCTION

    # The schedule step should NOT contain proactive scheduling language
    # for reflection sessions specifically.
    lowered = SUPERPOM_SCHEDULE_INSTRUCTION.lower()
    assert "schedule a reflection" not in lowered
    assert "set up a reflection" not in lowered
    assert "start a reflection" not in lowered
