"""Discord bot user-id helpers — single source of truth.

Do NOT add a field to `app/config.py` Settings; use these helpers instead.
"""

from __future__ import annotations

import base64
import logging
import os
import re

logger = logging.getLogger(__name__)

_DISCORD_BOT_USER_ID_RE = re.compile(r"^\d+$")


def _decode_discord_user_id(token: str) -> str | None:
    """Decode the user-id prefix of a Discord bot token.

    Discord bot tokens are "<base64url(user_id)>.<timestamp>.<hmac>". The first
    segment is the user id encoded as urlsafe base64 (no padding). Decode it to
    the canonical decimal user id (e.g. '1245222614276898866') which is what
    inbound webhooks use and what `routing.resolve_bot` will need.
    """
    prefix = token.split(".", 1)[0]
    if not prefix:
        return None
    # base64 needs len-multiple-of-4 input; add the missing padding.
    padding = "=" * (-len(prefix) % 4)
    try:
        decoded = base64.urlsafe_b64decode(prefix + padding).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return None
    if _DISCORD_BOT_USER_ID_RE.match(decoded):
        return decoded
    return None


def discord_bot_user_id(bot_id: str) -> str | None:
    """Return the Discord bot's numeric user id for *bot_id*.

    Resolution order (first match wins):

    1. Per-bot override  — DISCORD_BOT_USER_ID_<BOT_ID_UPPER> (digit-only).
    2. Per-bot token      — DISCORD_BOT_TOKEN_<BOT_ID_UPPER>, decoded via
                            _decode_discord_user_id.
    3. Mediator fallback  — (only when *bot_id* == 'mediator'): try
                            DISCORD_BOT_USER_ID, then DISCORD_BOT_TOKEN
                            decode (same logic as the pre-multi-gateway era).

    Returns None when no source is available for the requested bot.
    """
    from app.config import get_settings

    settings = get_settings()

    # (a) Per-bot user-id override — DISCORD_BOT_USER_ID_<BOT_ID_UPPER>
    overrides = settings.discord_bot_user_id_overrides
    if bot_id in overrides:
        return overrides[bot_id]

    # (b) Decode from per-bot token — DISCORD_BOT_TOKEN_<BOT_ID_UPPER>
    tokens = settings.discord_bot_tokens
    if bot_id in tokens:
        return _decode_discord_user_id(tokens[bot_id].get_secret_value())

    # (c) Mediator legacy fallback — DISCORD_BOT_USER_ID / DISCORD_BOT_TOKEN
    if bot_id == "mediator":
        from_env = os.environ.get("DISCORD_BOT_USER_ID")
        if from_env and _DISCORD_BOT_USER_ID_RE.match(from_env):
            return from_env
        token = os.environ.get("DISCORD_BOT_TOKEN")
        if token:
            return _decode_discord_user_id(token)

    return None