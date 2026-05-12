"""Tante Rosi persona prompt — phase 1 placeholder.

Phase 2 will fill in the full persona content per the sprint brief §3:
voice register, medical-defer guardrails, loss/grief stance,
complication handling, onboarding stance, multilingual stance, boundaries.

For now the renderer delegates to the shared solo system prompt with
topic_display_name='pregnancy'.  The persona body is a single-line placeholder
so the bot can be registered and allowlist-tested without producing real
user-facing content yet.
"""

from __future__ import annotations

from typing import Any

from app.services.prompts_solo import render_solo_system_prompt

# ── Phase 1 placeholder ───────────────────────────────────────────────────
# Replaced with the full persona text in Phase 2.
_PLACEHOLDER_PERSONA = "Tante Rosi prompt (phase 2 content pending)."


def render_system_prompt(**kwargs: Any) -> str:
    """Render the Tante Rosi system prompt.

    Delegates to the shared solo system prompt with topic_display_name
    set to 'pregnancy'.  All keyword arguments are forwarded to
    render_solo_system_prompt, which accepts assistant_name, user_name,
    prompt_version, onboarding_state, sharing_default, topic_display_name,
    and extra **kwargs.
    """
    return render_solo_system_prompt(
        topic_display_name="pregnancy",
        **kwargs,
    )