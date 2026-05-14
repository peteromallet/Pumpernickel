from app.services.open_asks import OpenAsk, _get_bot_asks, render_open_asks


def test_both_edd_and_partner_share_open_render_with_examples() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": None,
            "partner_share": None,
            "has_partner": True,
            "partner_name": "Hannah",
        },
    )

    assert "## Open asks" in rendered
    assert "`pregnancy_edd` is not set." in rendered
    assert "Glückwunsch. Damit ich dich gut begleiten kann" in rendered
    assert "Resolves with: `set_pregnancy_edd`" in rendered
    assert "`partner_share` is not set." in rendered
    assert "Willst du, dass ich Hannah ab und zu sage" in rendered
    assert "Resolves with: `set_partner_sharing`" in rendered


def test_edd_set_partner_share_null_renders_only_partner_share() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": "2026-12-01",
            "partner_share": None,
            "has_partner": True,
            "partner_name": "Hannah",
        },
    )

    assert "`pregnancy_edd` is not set." not in rendered
    assert "`partner_share` is not set." in rendered


def test_both_set_returns_empty_string() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": "2026-12-01",
            "partner_share": "opt_in",
            "has_partner": True,
        },
    )

    assert rendered == ""


def test_has_partner_false_suppresses_partner_share() -> None:
    from app.bots.prompts.tante_rosi import ASKS

    rendered = render_open_asks(
        ASKS,
        {
            "pregnancy_edd": "2026-12-01",
            "partner_share": None,
            "has_partner": False,
        },
    )

    assert rendered == ""


def test_empty_asks_list_returns_empty_string() -> None:
    assert render_open_asks([], {}) == ""


def test_partner_name_substitution() -> None:
    ask = OpenAsk(
        key="partner_share",
        open_if=lambda state: True,
        example="Can I share context with {partner_name}?",
        resolves_with="set_partner_sharing",
    )

    rendered = render_open_asks([ask], {"partner_name": "Hannah"})

    assert "Hannah" in rendered
    assert "{partner_name}" not in rendered


def test_registry_resolves_tante_rosi_mediator_and_unknown() -> None:
    from app.bots.prompts.tante_rosi import ASKS as ROSI_ASKS
    from app.services.prompts import VEAS_ASKS
    from app.services.prompts_solo import ASKS as SOLO_ASKS

    assert _get_bot_asks("tante_rosi") is ROSI_ASKS
    assert _get_bot_asks("mediator") is VEAS_ASKS
    assert _get_bot_asks("unknown") is SOLO_ASKS
