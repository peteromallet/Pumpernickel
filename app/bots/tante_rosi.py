"""Solo 'tante_rosi' bot profile (pregnancy topic).

Phase 1 placeholder: BotSpec wired with tool allowlist + ReadScopes per the
sprint brief §2.1.  The prompt renderer delegates to the phase-1 placeholder
in app.bots.prompts.tante_rosi — real persona content lands in Phase 2.

Registered lazily in _maybe_register_staging_bots (STAGING=1 gate), mirroring
the coach registration pattern.  Prod path (bots-table row-existence gate)
lands in T11.
"""

from __future__ import annotations

from app.bots.base import BotSpec, ReadScopes, WriteScopes
from app.bots.prompts.tante_rosi import render_system_prompt as _persona_render


def _tante_rosi_prompt_renderer(
    assistant_name: str,
    user_name: str,
    partner_name: str | None = None,
    *,
    prompt_version: str = "v1",
    onboarding_state: str | None = None,
    current_user_sharing_default: str | None = None,
    partner_sharing_default: str | None = None,
    **kwargs: object,
) -> str:
    """Tante Rosi prompt renderer — delegates to the persona module.

    Accepts partner_name, partner_sharing_default, and partner (via
    **kwargs) from BotSpec.render_system_prompt but ignores them.  The
    solo renderer has no dyadic concepts.
    """
    return _persona_render(
        assistant_name=assistant_name,
        user_name=user_name,
        prompt_version=prompt_version,
        onboarding_state=onboarding_state,
        sharing_default=current_user_sharing_default,
    )


_MIN_STEP_INSTRUCTIONS = {
    "read": "Read step (Rosi phase 1 stub).",
    "consult": "Consult step (Rosi phase 1 stub).",
    "respond": "Respond step (Rosi phase 1 stub).",
    "record": "Record step (Rosi phase 1 stub).",
    "schedule": "Schedule step (Rosi phase 1 stub).",
    "done": "Done step (Rosi phase 1 stub).",
}

# ── Tool allowlist ─────────────────────────────────────────────────────────
# §4.1 no-auto-bridging: the bridge/escalate exclusions below are load-bearing.
# Tante Rosi MUST NOT be able to auto-bridge pregnancy content to the mediator.
_COACH_EXCLUSIONS = frozenset(
    {
        "set_topic_status",
        # Bridge/escalate (load-bearing for §4.1 no-auto-bridging):
        "create_bridge_candidate",
        "update_bridge_candidate",
        "send_bridge_candidate",
        "list_bridge_candidates",
        "escalate_to_partner",
        # Dyad-only read tools:
        "search_messages",
        "recent_activity",
    }
)

_TANTE_ROSI_ADDITIONS = frozenset(
    {
        "set_pregnancy_edd",
        "correct_pregnancy_edd",
        "end_pregnancy",
    }
)


def build_tante_rosi_spec() -> BotSpec:
    """Build the Tante Rosi BotSpec.

    Lazy import of TOOL_DISPATCH avoids a circular import (registry imports
    write_tools, which doesn't import tante_rosi).  The tool_allowlist is the
    full dispatch table minus dyad-only/bridge tools plus the three pregnancy
    write tools.
    """
    from app.services.tools.registry import TOOL_DISPATCH

    return BotSpec(
        bot_id="tante_rosi",
        prompt_renderer=_tante_rosi_prompt_renderer,
        step_instructions=_MIN_STEP_INSTRUCTIONS,
        display_name="Tante Rosi",
        primary_topic_slug="pregnancy",
        participants_shape="solo",
        read_scopes=ReadScopes(
            topics=frozenset({"own"}),
            allow_cross_topic_peek=True,
            allow_cross_topic_status_injection=False,
        ),
        write_scopes=WriteScopes(topics=frozenset({"own"})),
        cross_topic_policy="peek",
        tool_allowlist=(frozenset(TOOL_DISPATCH.keys()) - _COACH_EXCLUSIONS)
        | _TANTE_ROSI_ADDITIONS,
        bot_spec_version="1.0.0",
    )