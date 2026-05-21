"""Live-voice bot profile helpers."""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.bots.registry import BOT_SPECS, get_bot_spec
from app.models.user import User

logger = logging.getLogger(__name__)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row[key]
    except Exception:
        if isinstance(row, dict):
            return row.get(key, default)
        return default


def user_from_live_row(user_id: UUID, row: Any | None) -> User:
    """Build a minimal User for persona prompt rendering."""
    if row is None:
        return User(id=user_id, name="the user", phone="", timezone="UTC")
    return User(
        id=user_id,
        name=_row_value(row, "name") or "the user",
        phone=_row_value(row, "phone") or "",
        timezone=_row_value(row, "timezone") or "UTC",
        onboarding_state=_row_value(row, "onboarding_state") or "pending",
        pacing_preferences=_row_value(row, "pacing_preferences") or {},
        pregnancy_edd=_row_value(row, "pregnancy_edd"),
        pregnancy_dating_basis=_row_value(row, "pregnancy_dating_basis"),
        pregnancy_lmp_date=_row_value(row, "pregnancy_lmp_date"),
        pregnancy_scan_date=_row_value(row, "pregnancy_scan_date"),
        pregnancy_scan_corrected_at=_row_value(row, "pregnancy_scan_corrected_at"),
        pregnancy_started_at=_row_value(row, "pregnancy_started_at"),
        pregnancy_ended_at=_row_value(row, "pregnancy_ended_at"),
        pregnancy_outcome=_row_value(row, "pregnancy_outcome"),
    )


def live_bot_profile_context(bot_id: str, *, user: User | None = None) -> dict[str, Any]:
    """Return selected-bot context for live prep and live turn prompts."""
    try:
        spec = get_bot_spec(bot_id)
    except Exception:
        spec = BOT_SPECS.get(bot_id)
    if spec is None:
        return {"bot_id": bot_id}

    profile: dict[str, Any] = {
        "bot_id": spec.bot_id,
        "display_name": spec.display_name,
        "primary_topic_slug": spec.primary_topic_slug,
        "participants_shape": spec.participants_shape,
        "bot_spec_version": spec.bot_spec_version,
    }
    if user is None:
        return profile

    try:
        profile["system_prompt"] = spec.render_system_prompt(
            assistant_name=spec.display_name,
            user=user,
            partner=None,
            prompt_version=spec.bot_spec_version,
        )
    except Exception:
        logger.warning(
            "live bot profile: failed to render %s prompt",
            bot_id,
            exc_info=True,
        )
    return profile


def format_live_bot_profile(profile: dict[str, Any]) -> str:
    """Format selected-bot context for LLM prompts."""
    lines = [
        f"- bot_id: {profile.get('bot_id') or '(unknown)'}",
        f"- display_name: {profile.get('display_name') or '(unknown)'}",
        f"- primary_topic_slug: {profile.get('primary_topic_slug') or '(unknown)'}",
        f"- participants_shape: {profile.get('participants_shape') or '(unknown)'}",
    ]
    prompt = (profile.get("system_prompt") or "").strip()
    if prompt:
        lines.append("\nSELECTED BOT SYSTEM PROMPT:\n" + prompt)
    return "\n".join(lines)
