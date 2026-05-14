from app.bots.prompts.partner_sharing import PENDING_PARTNER_SHARING_PROMPT_SLOT


def test_pending_partner_sharing_prompt_slot_is_canonical_and_domain_agnostic() -> None:
    text = PENDING_PARTNER_SHARING_PROMPT_SLOT
    lower = text.lower()
    compact = " ".join(lower.split())

    assert "undecided for this user and this bot" in lower
    assert "crisis or time-critical" in lower
    assert "raise the choice naturally this turn" in lower
    assert "do not share this bot's memories or distillations" in compact
    assert "until the user explicitly opts in" in lower
    assert "`set_partner_sharing(opt_in=true)`" in text
    assert "`set_partner_sharing(opt_in=false)`" in text

    assert "pregnancy" not in lower
    assert "relationship" not in lower
    assert "rosi" not in lower
    assert "mediator" not in lower


def test_mediator_renders_canonical_slot_only_when_pending() -> None:
    from app.services.prompts import render_system_prompt

    pending = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share=None,
        current_user_partner_sharing_state="pending",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT in pending
    assert "update_cross_thread_sharing_default" not in pending

    opted_out = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share="opt_out",
        current_user_partner_sharing_state="opt_out",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT not in opted_out
    assert "do not pressure or repeat the opt-in question" in opted_out
    assert "gently surface the value sharing could unlock" not in opted_out

    unavailable = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share=None,
        current_user_partner_sharing_state="unavailable",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT not in unavailable


def test_generic_solo_and_coach_render_canonical_pending_slot() -> None:
    from uuid import uuid4

    from app.bots.coach import build_coach_spec
    from app.models.user import User
    from app.services.prompts_solo import render_solo_system_prompt

    pending = render_solo_system_prompt(
        "Coach",
        "Maya",
        partner_share=None,
        partner_sharing_state="pending",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT in pending

    unavailable = render_solo_system_prompt(
        "Coach",
        "Maya",
        partner_share=None,
        partner_sharing_state="unavailable",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT not in unavailable

    spec = build_coach_spec()
    user = User(uuid4(), "Maya", "15555550100", "UTC")
    rendered = spec.render_system_prompt(
        assistant_name="Coach",
        user=user,
        partner=None,
        prompt_version="v1",
        current_user_partner_share=None,
        current_user_partner_sharing_state="pending",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT in rendered


def test_tante_rosi_renders_canonical_slot_and_opt_in_guidance() -> None:
    from uuid import uuid4

    from app.bots.prompts.tante_rosi import render_system_prompt
    from app.bots.tante_rosi import build_tante_rosi_spec
    from app.models.user import User

    pending = render_system_prompt(
        assistant_name="Tante Rosi",
        user_name="Anna",
        partner_share=None,
        partner_sharing_state="pending",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT in pending

    opted_in = render_system_prompt(
        assistant_name="Tante Rosi",
        user_name="Anna",
        partner_share="opt_in",
        partner_sharing_state="opt_in",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT not in opted_in
    assert "Partner Sharing For Pregnancy Facts" in opted_in
    assert "`dyad_shareable` memories or distillations" in opted_in
    assert "`shareable_summary`" in opted_in
    assert "When unsure, keep it private." in opted_in

    opted_out = render_system_prompt(
        assistant_name="Tante Rosi",
        user_name="Anna",
        partner_share="opt_out",
        partner_sharing_state="opt_out",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT not in opted_out
    assert "Partner Sharing For Pregnancy Facts" not in opted_out

    spec = build_tante_rosi_spec()
    user = User(uuid4(), "Anna", "15555550100", "UTC")
    rendered = spec.render_system_prompt(
        assistant_name="Tante Rosi",
        user=user,
        partner=None,
        prompt_version="v1",
        current_user_partner_share=None,
        current_user_partner_sharing_state="pending",
    )
    assert PENDING_PARTNER_SHARING_PROMPT_SLOT in rendered
