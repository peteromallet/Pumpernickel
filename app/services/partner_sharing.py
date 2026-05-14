"""Shared helpers for per-bot partner sharing state."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

PartnerShare = Literal["opt_in", "opt_out"]

VALID_PARTNER_SHARE_VALUES: frozenset[str] = frozenset({"opt_in", "opt_out"})


@dataclass(frozen=True)
class DyadPartner:
    dyad_id: UUID
    partner_user_id: UUID


def normalize_partner_share(value: str | None) -> PartnerShare | None:
    """Normalize stored partner-share state.

    ``None`` means pending/unset. Anything other than opt_in/opt_out is a
    schema or caller bug and is rejected rather than treated as a default.
    """
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_PARTNER_SHARE_VALUES:
        raise ValueError(f"invalid partner_share value: {value!r}")
    return normalized  # type: ignore[return-value]


def partner_share_from_opt_in(opt_in: bool) -> PartnerShare:
    return "opt_in" if opt_in else "opt_out"


async def get_partner_share(
    pool: Any,
    *,
    user_id: UUID,
    bot_id: str,
) -> PartnerShare | None:
    value = await pool.fetchval(
        """
        SELECT partner_share
        FROM user_bot_state
        WHERE user_id = $1 AND bot_id = $2
        """,
        user_id,
        bot_id,
    )
    return normalize_partner_share(value)


async def get_partner_share_states(
    pool: Any,
    keys: Iterable[tuple[UUID, str]],
) -> dict[tuple[UUID, str], PartnerShare | None]:
    """Fetch partner-share states for many ``(user_id, bot_id)`` keys.

    Missing rows are returned as ``None`` so callers can treat absent
    user_bot_state rows the same as an explicit pending value.
    """
    unique_keys = list(dict.fromkeys(keys))
    if not unique_keys:
        return {}

    user_ids = [user_id for user_id, _ in unique_keys]
    bot_ids = [bot_id for _, bot_id in unique_keys]
    rows = await pool.fetch(
        """
        WITH requested AS (
            SELECT *
            FROM unnest($1::uuid[], $2::text[]) AS r(user_id, bot_id)
        )
        SELECT r.user_id, r.bot_id, ubs.partner_share
        FROM requested r
        LEFT JOIN user_bot_state ubs
          ON ubs.user_id = r.user_id
         AND ubs.bot_id = r.bot_id
        """,
        user_ids,
        bot_ids,
    )
    found: dict[tuple[UUID, str], PartnerShare | None] = {}
    for row in rows:
        found[(row["user_id"], row["bot_id"])] = normalize_partner_share(
            row["partner_share"]
        )
    for key in unique_keys:
        found.setdefault(key, None)
    return found


async def set_partner_share(
    pool: Any,
    *,
    user_id: UUID,
    bot_id: str,
    opt_in: bool,
) -> PartnerShare:
    """Upsert partner sharing for one scoped ``(user_id, bot_id)`` pair."""
    partner_share = partner_share_from_opt_in(opt_in)
    await pool.execute(
        """
        INSERT INTO user_bot_state (user_id, bot_id, partner_share, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (user_id, bot_id) DO UPDATE
        SET partner_share = EXCLUDED.partner_share,
            updated_at = now()
        """,
        user_id,
        bot_id,
        partner_share,
    )
    return partner_share


async def resolve_dyad_partner(pool: Any, user_id: UUID) -> DyadPartner | None:
    """Return the user's dyad partner using dyads/dyad_members only."""
    row = await pool.fetchrow(
        """
        SELECT dm_other.dyad_id, dm_other.user_id AS partner_user_id
        FROM dyad_members dm_self
        JOIN dyads d ON d.id = dm_self.dyad_id
        JOIN dyad_members dm_other
          ON dm_other.dyad_id = dm_self.dyad_id
         AND dm_other.user_id <> dm_self.user_id
        WHERE dm_self.user_id = $1
        ORDER BY d.created_at DESC, dm_other.joined_at DESC
        LIMIT 1
        """,
        user_id,
    )
    if row is None:
        return None
    return DyadPartner(dyad_id=row["dyad_id"], partner_user_id=row["partner_user_id"])


async def has_dyad_partner(pool: Any, user_id: UUID) -> bool:
    return await resolve_dyad_partner(pool, user_id) is not None


async def bot_display_name(pool: Any, bot_id: str) -> str:
    """Resolve a bot display name from registry, then DB, then id fallback."""
    try:
        from app.bots.registry import UnknownBotSpec, get_bot_spec

        return get_bot_spec(bot_id).display_name
    except UnknownBotSpec:
        pass
    except ImportError:
        pass

    row = await pool.fetchrow(
        """
        SELECT display_name
        FROM bots
        WHERE id = $1
        """,
        bot_id,
    )
    if row is not None and row["display_name"]:
        return row["display_name"]
    return bot_id


async def provenance_prefix(pool: Any, bot_id: str) -> str:
    return f"from {await bot_display_name(pool, bot_id)}:"
