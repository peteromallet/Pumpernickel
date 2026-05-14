def test_partner_sharing_prompt_module_no_longer_exports_pending_slot() -> None:
    import app.bots.prompts.partner_sharing as partner_sharing

    assert not hasattr(partner_sharing, "PENDING_PARTNER_SHARING_PROMPT_SLOT")


def test_mediator_keeps_settled_partner_sharing_guidance_only() -> None:
    from app.services.prompts import render_system_prompt

    pending = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share=None,
        current_user_partner_sharing_state="pending",
    )
    assert "Partner sharing is undecided" not in pending
    assert "set_partner_sharing" not in pending

    opted_out = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share="opt_out",
        current_user_partner_sharing_state="opt_out",
    )
    assert "do not pressure or repeat the opt-in question" in opted_out

    opted_in = render_system_prompt(
        "Veas",
        "Maya",
        "Ben",
        current_user_partner_share="opt_in",
        current_user_partner_sharing_state="opt_in",
    )
    assert "partner_share` is `opt_in`" in opted_in


def test_generic_solo_and_coach_do_not_render_pending_slot() -> None:
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
    assert "Partner sharing is undecided" not in pending
    assert "set_partner_sharing" not in pending

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
    assert "Partner sharing is undecided" not in rendered


def test_tante_rosi_keeps_opt_in_guidance_only() -> None:
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
    assert "Partner sharing is undecided" not in pending
    assert "Partner Sharing For Pregnancy Facts" not in pending

    opted_in = render_system_prompt(
        assistant_name="Tante Rosi",
        user_name="Anna",
        partner_share="opt_in",
        partner_sharing_state="opt_in",
    )
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
    assert "Partner sharing is undecided" not in rendered
