from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.partner_sharing import (
    DyadPartner,
    bot_display_name,
    get_partner_share,
    get_partner_share_states,
    normalize_partner_share,
    provenance_prefix,
    resolve_dyad_partner,
    set_partner_share,
)


class PartnerSharingPool:
    def __init__(self) -> None:
        self.states: dict[tuple[object, str], str | None] = {}
        self.bot_names: dict[str, str] = {}
        self.dyad_partner: DyadPartner | None = None
        self.executed: list[tuple[str, tuple[object, ...]]] = []

    async def fetchval(self, sql: str, *args):
        assert "FROM user_bot_state" in sql
        return self.states.get((args[0], args[1]))

    async def fetch(self, sql: str, *args):
        assert "unnest($1::uuid[], $2::text[])" in sql
        user_ids, bot_ids = args
        return [
            {
                "user_id": user_id,
                "bot_id": bot_id,
                "partner_share": self.states.get((user_id, bot_id)),
            }
            for user_id, bot_id in zip(user_ids, bot_ids, strict=True)
        ]

    async def fetchrow(self, sql: str, *args):
        if "FROM dyad_members" in sql:
            if self.dyad_partner is None:
                return None
            return {
                "dyad_id": self.dyad_partner.dyad_id,
                "partner_user_id": self.dyad_partner.partner_user_id,
            }
        if "FROM bots" in sql:
            bot_id = args[0]
            name = self.bot_names.get(bot_id)
            if name is None:
                return None
            return {"display_name": name}
        raise AssertionError(f"unexpected fetchrow SQL: {sql}")

    async def execute(self, sql: str, *args):
        assert "INSERT INTO user_bot_state" in sql
        user_id, bot_id, partner_share = args
        self.states[(user_id, bot_id)] = partner_share
        self.executed.append((sql, args))
        return "INSERT 0 1"


def test_normalize_partner_share_accepts_only_known_values():
    assert normalize_partner_share(None) is None
    assert normalize_partner_share(" opt_in ") == "opt_in"
    assert normalize_partner_share("OPT_OUT") == "opt_out"
    with pytest.raises(ValueError):
        normalize_partner_share("maybe")


async def test_partner_share_fetch_batch_and_scoped_upsert():
    pool = PartnerSharingPool()
    user_id = uuid4()
    other_user_id = uuid4()
    pool.states[(user_id, "mediator")] = "opt_in"

    assert await get_partner_share(pool, user_id=user_id, bot_id="mediator") == "opt_in"

    states = await get_partner_share_states(
        pool,
        [
            (user_id, "mediator"),
            (user_id, "tante_rosi"),
            (other_user_id, "mediator"),
        ],
    )
    assert states[(user_id, "mediator")] == "opt_in"
    assert states[(user_id, "tante_rosi")] is None
    assert states[(other_user_id, "mediator")] is None

    result = await set_partner_share(
        pool, user_id=user_id, bot_id="tante_rosi", opt_in=False
    )
    assert result == "opt_out"
    assert pool.states[(user_id, "tante_rosi")] == "opt_out"
    assert "INSERT INTO user_bot_state" in pool.executed[-1][0]


async def test_resolve_dyad_partner_uses_dyad_tables():
    pool = PartnerSharingPool()
    dyad_id = uuid4()
    partner_user_id = uuid4()
    pool.dyad_partner = DyadPartner(dyad_id=dyad_id, partner_user_id=partner_user_id)

    partner = await resolve_dyad_partner(pool, uuid4())
    assert partner == DyadPartner(dyad_id=dyad_id, partner_user_id=partner_user_id)

    pool.dyad_partner = None
    assert await resolve_dyad_partner(pool, uuid4()) is None


async def test_provenance_uses_registry_then_db_then_id_fallback():
    pool = PartnerSharingPool()
    assert await provenance_prefix(pool, "mediator") == "from Mediator:"

    pool.bot_names["custom_bot"] = "Custom Bot"
    assert await bot_display_name(pool, "custom_bot") == "Custom Bot"
    assert await provenance_prefix(pool, "custom_bot") == "from Custom Bot:"

    assert await bot_display_name(pool, "missing_bot") == "missing_bot"
