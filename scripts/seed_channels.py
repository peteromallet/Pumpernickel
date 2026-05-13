#!/usr/bin/env python3
"""Seed channels from environment variables — idempotent, credentials-optional.

Post-migration script. Run after 0020_topics_bots_bindings.sql is applied.
Each transport block independently reads its env var; if absent, logs INFO and skips.
WhatsApp is optional (WHATSAPP_PHONE_NUMBER_ID may not be set).

Usage:
    python scripts/seed_channels.py

Requires:
    DISCORD_BOT_TOKEN_<BOT_ID> (per-bot tokens; required for each discord channel)
    DISCORD_BOT_USER_ID_<BOT_ID> (optional per-bot override; derived from token if unset)
    DISCORD_BOT_TOKEN               (legacy fallback for mediator)
    WHATSAPP_PHONE_NUMBER_ID        (optional — skipped if absent)
    DATABASE_URL or PG* env vars for asyncpg connection
"""

from __future__ import annotations

import asyncio
import logging
import os

import asyncpg

from app.services.discord_id import _decode_discord_user_id

logger = logging.getLogger(__name__)


def _env(key: str) -> str | None:
    value = os.getenv(key)
    return value.strip() if value else None


async def _get_pool() -> asyncpg.Pool:
    # statement_cache_size=0 is required for Supabase's transaction-mode pooler
    # (port 6543). Safe to set unconditionally — only disables a local cache.
    database_url = _env("DATABASE_URL")
    if database_url:
        return await asyncpg.create_pool(
            dsn=database_url, min_size=1, max_size=2, statement_cache_size=0
        )
    return await asyncpg.create_pool(
        host=_env("PGHOST") or "localhost",
        port=int(_env("PGPORT") or "5432"),
        user=_env("PGUSER") or "postgres",
        password=_env("PGPASSWORD") or "",
        database=_env("PGDATABASE") or "postgres",
        min_size=1,
        max_size=2,
        statement_cache_size=0,
    )


async def seed_discord(pool: asyncpg.Pool) -> bool:
    """Seed discord channels from per-bot tokens. Returns True if any seeded, False if skipped."""
    from app.config import get_settings

    settings = get_settings()
    per_bot_tokens = settings.discord_bot_tokens

    # Build a unified dict of (bot_id, token_str) pairs
    tokens: dict[str, str] = {
        bot_id: s.get_secret_value() for bot_id, s in per_bot_tokens.items()
    }

    # Legacy fallback: DISCORD_BOT_TOKEN → mediator (only when no per-bot tokens)
    if not tokens:
        legacy_token = _env("DISCORD_BOT_TOKEN")
        if legacy_token:
            tokens["mediator"] = legacy_token

    if not tokens:
        logger.info("No Discord bot tokens configured — skipping discord channel seed")
        return False

    seeded_any = False
    for bot_id, token in tokens.items():
        # Per-bot user-id override: DISCORD_BOT_USER_ID_<BOT_ID_UPPER>
        override_key = f"DISCORD_BOT_USER_ID_{bot_id.upper()}"
        bot_user_id = _env(override_key)
        if not bot_user_id or not bot_user_id.isdigit():
            bot_user_id = _decode_discord_user_id(token)

        if not bot_user_id or not bot_user_id.isdigit():
            logger.warning(
                "Could not resolve bot user id for bot_id=%s; "
                "set DISCORD_BOT_USER_ID_%s explicitly or check token format",
                bot_id,
                bot_id.upper(),
            )
            continue

        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                INSERT INTO channels (bot_id, transport, address, guild_id, channel_id)
                VALUES ($1, 'discord', $2, NULL, NULL)
                ON CONFLICT (transport, address, COALESCE(guild_id, ''), COALESCE(channel_id, ''))
                DO NOTHING
                """,
                bot_id,
                bot_user_id,
            )
        inserted = result != "INSERT 0 0"
        if inserted:
            logger.info("Seeded discord channel: bot_id=%s address=%s", bot_id, bot_user_id)
        else:
            logger.info("Discord channel already exists: bot_id=%s address=%s", bot_id, bot_user_id)
        seeded_any = True

    return seeded_any


async def seed_whatsapp(pool: asyncpg.Pool) -> bool:
    """Seed whatsapp channel. Returns True if seeded, False if skipped."""
    phone_number_id = _env("WHATSAPP_PHONE_NUMBER_ID")
    if not phone_number_id:
        logger.info("WHATSAPP_PHONE_NUMBER_ID not set — skipping whatsapp channel seed")
        return False

    async with pool.acquire() as conn:
        result = await conn.execute(
            """
            INSERT INTO channels (bot_id, transport, address, guild_id, channel_id)
            VALUES ('mediator', 'whatsapp', $1, NULL, NULL)
            ON CONFLICT (transport, address, COALESCE(guild_id, ''), COALESCE(channel_id, ''))
            DO NOTHING
            """,
            phone_number_id,
        )
    inserted = result != "INSERT 0 0"
    if inserted:
        logger.info("Seeded whatsapp channel: address=%s", phone_number_id)
    else:
        logger.info("Whatsapp channel already exists: address=%s", phone_number_id)
    return True


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    pool = await _get_pool()
    try:
        discord_ok = await seed_discord(pool)
        whatsapp_ok = await seed_whatsapp(pool)

        if not discord_ok and not whatsapp_ok:
            logger.warning(
                "No channels seeded — set DISCORD_BOT_TOKEN_<BOT_ID> or WHATSAPP_PHONE_NUMBER_ID"
            )
        else:
            logger.info("Channel seeding complete")
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())