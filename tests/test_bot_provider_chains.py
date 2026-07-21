"""Provider routing invariants for registered chat bots."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.bots.ids import (
    HABITS_BOT_ID,
    HECTOR_BOT_ID,
    MEDIATOR_BOT_ID,
    SUPERPOM_BOT_ID,
    TANTE_ROSI_BOT_ID,
)


SAFE_CHAT_PROVIDER_CHAIN = ("deepseek", "anthropic")
PRODUCTION_CHAT_BOT_IDS = frozenset({
    MEDIATOR_BOT_ID,
    TANTE_ROSI_BOT_ID,
    HECTOR_BOT_ID,
    HABITS_BOT_ID,
    SUPERPOM_BOT_ID,
})


def test_all_registered_production_chat_bots_use_safe_provider_chain(
    monkeypatch,
):
    """Every DB-gated production persona resolves to DeepSeek first."""
    from app.bots import registry
    from app.bots.mediator import MEDIATOR_BOT

    monkeypatch.setenv("STAGING", "1")
    monkeypatch.setattr(
        registry,
        "BOT_SPECS",
        {MEDIATOR_BOT.bot_id: MEDIATOR_BOT},
    )
    monkeypatch.setattr(registry, "_STAGING_BOTS_REGISTERED", False)

    registry._maybe_register_staging_bots()

    assert PRODUCTION_CHAT_BOT_IDS <= registry.BOT_SPECS.keys()
    assert {
        bot_id: registry.BOT_SPECS[bot_id].provider_chain
        for bot_id in PRODUCTION_CHAT_BOT_IDS
    } == {
        bot_id: SAFE_CHAT_PROVIDER_CHAIN
        for bot_id in PRODUCTION_CHAT_BOT_IDS
    }


def test_staging_coach_also_uses_safe_provider_chain():
    """Keep the staging-only coach safe if it later becomes production."""
    from app.bots.coach import build_coach_spec

    assert build_coach_spec().provider_chain == SAFE_CHAT_PROVIDER_CHAIN


@pytest.mark.parametrize(
    ("populate_name", "bot_id", "db_row"),
    (
        (
            "populate_mediator_spec_from_db",
            MEDIATOR_BOT_ID,
            {"display_name": "Mediator"},
        ),
        (
            "populate_tante_rosi_spec_from_db",
            TANTE_ROSI_BOT_ID,
            {"?column?": 1},
        ),
        (
            "populate_hector_spec_from_db",
            HECTOR_BOT_ID,
            {"?column?": 1},
        ),
        (
            "populate_habits_spec_from_db",
            HABITS_BOT_ID,
            {"?column?": 1},
        ),
        (
            "populate_superpom_spec_from_db",
            SUPERPOM_BOT_ID,
            {"?column?": 1},
        ),
    ),
)
async def test_db_gated_registration_preserves_safe_provider_chain(
    monkeypatch,
    populate_name,
    bot_id,
    db_row,
):
    """Every production populate success path installs a safe BotSpec."""
    from app.bots import registry

    pool = AsyncMock()
    pool.fetchrow = AsyncMock(return_value=db_row)
    monkeypatch.setattr(registry, "BOT_SPECS", {})

    await getattr(registry, populate_name)(pool)

    pool.fetchrow.assert_awaited_once()
    assert registry.BOT_SPECS[bot_id].provider_chain == SAFE_CHAT_PROVIDER_CHAIN
