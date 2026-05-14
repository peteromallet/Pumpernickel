"""PARTNER_NUDGE_PROMPT_SLOT (S3) — slot quality + mounting + inert draft.

Invariant 6: the autonomous-judgment guidance ships INERT — present in
the file as ``_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT`` and mounted
by NO renderer in this megaplan.
"""

from __future__ import annotations

from uuid import uuid4

from app.bots.prompts.partner_nudge import (
    PARTNER_NUDGE_PROMPT_SLOT,
    _AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT,
)


def test_slot_is_under_two_hundred_active_words() -> None:
    assert len(PARTNER_NUDGE_PROMPT_SLOT.split()) <= 200


def test_slot_names_required_tools() -> None:
    assert "schedule_partner_checkin" in PARTNER_NUDGE_PROMPT_SLOT
    assert "cancel_partner_nudge" in PARTNER_NUDGE_PROMPT_SLOT


def test_slot_contains_acceptable_and_unacceptable_examples() -> None:
    # SD-005 containment rule examples. Normalize whitespace so a
    # wrapped line inside the slot still matches the conceptual
    # sentence boundary.
    normalized = " ".join(PARTNER_NUDGE_PROMPT_SLOT.split())
    assert "Pom asked me to see how you're doing today." in normalized
    assert "Pom says you've been distant." in normalized


def test_slot_enumerates_three_rejection_reasons() -> None:
    assert "no_dyad_partner" in PARTNER_NUDGE_PROMPT_SLOT
    assert "opt_out" in PARTNER_NUDGE_PROMPT_SLOT
    assert "pending" in PARTNER_NUDGE_PROMPT_SLOT


def test_autonomous_draft_is_inert_and_marked() -> None:
    """Invariant 6 sanity: the draft constant exists, contains the
    DRAFT marker, and is NOT included in either rendered prompt.
    """
    assert "DRAFT" in _AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT
    assert "not mounted" in _AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT.lower()


def test_mediator_rendered_prompt_contains_partner_nudge_slot() -> None:
    from app.services.prompts import render_system_prompt

    rendered = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share="opt_in",
        current_user_partner_sharing_state="opt_in",
    )
    assert PARTNER_NUDGE_PROMPT_SLOT in rendered
    # Autonomous draft must NOT leak into the rendered prompt.
    assert _AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT not in rendered
    # Spot-check a sentinel from the draft body.
    assert "asymmetric care load" not in rendered


def test_solo_rendered_prompt_contains_partner_nudge_slot() -> None:
    from app.services.prompts_solo import render_solo_system_prompt

    rendered = render_solo_system_prompt("Coach", "Maya")
    assert PARTNER_NUDGE_PROMPT_SLOT in rendered
    assert _AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT not in rendered


def test_tante_rosi_rendered_prompt_contains_partner_nudge_slot() -> None:
    from app.bots.prompts.tante_rosi import render_system_prompt

    rendered = render_system_prompt(
        assistant_name="Tante Rosi", user_name="Anna"
    )
    assert PARTNER_NUDGE_PROMPT_SLOT in rendered
    assert _AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT not in rendered


def test_tante_rosi_botspec_render_contains_partner_nudge_slot() -> None:
    from app.bots.tante_rosi import build_tante_rosi_spec
    from app.models.user import User

    spec = build_tante_rosi_spec()
    user = User(uuid4(), "Anna", "15555550100", "UTC")
    rendered = spec.render_system_prompt(
        assistant_name="Tante Rosi",
        user=user,
        partner=None,
        prompt_version="v1",
    )
    assert PARTNER_NUDGE_PROMPT_SLOT in rendered


def test_mount_order_scheduling_then_partner_nudge() -> None:
    """SD-013: rendered order must be scheduling → partner-nudge."""
    from app.bots.prompts.scheduling import SCHEDULING_CAPABILITY_PROMPT_SLOT
    from app.services.prompts import render_system_prompt

    rendered = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share=None,
        current_user_partner_sharing_state="pending",
    )
    scheduling_index = rendered.find(SCHEDULING_CAPABILITY_PROMPT_SLOT)
    partner_nudge_index = rendered.find(PARTNER_NUDGE_PROMPT_SLOT)
    assert scheduling_index >= 0
    assert partner_nudge_index > scheduling_index
    assert "Partner sharing is undecided" not in rendered


def test_solo_mount_order_scheduling_then_partner_nudge() -> None:
    from app.bots.prompts.scheduling import SCHEDULING_CAPABILITY_PROMPT_SLOT
    from app.services.prompts_solo import render_solo_system_prompt

    rendered = render_solo_system_prompt(
        "Coach", "Maya", partner_sharing_state="pending"
    )
    scheduling_index = rendered.find(SCHEDULING_CAPABILITY_PROMPT_SLOT)
    partner_nudge_index = rendered.find(PARTNER_NUDGE_PROMPT_SLOT)
    assert scheduling_index >= 0
    assert partner_nudge_index > scheduling_index
    assert "Partner sharing is undecided" not in rendered
