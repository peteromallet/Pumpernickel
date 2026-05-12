"""Solo 'coach' bot profile (career topic).

S4 pre-flight only: BotSpec wired but the prompt renderer + hot context code
live in S5. The spec is registered in BOT_SPECS only when STAGING env truthy.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.bots.base import BotSpec, ReadScopes, WriteScopes


def _coach_prompt_renderer(*args: Any, **kwargs: Any) -> str:
    raise NotImplementedError("coach prompt renderer lands in S5")


_MIN_STEP_INSTRUCTIONS = {
    "read": "Read step (coach S4 stub).",
    "consult": "Consult step (coach S4 stub).",
    "respond": "Respond step (coach S4 stub).",
    "record": "Record step (coach S4 stub).",
    "schedule": "Schedule step (coach S4 stub).",
    "done": "Done step (coach S4 stub).",
}


def build_coach_spec() -> BotSpec:
    """Build the coach BotSpec.

    Lazy import of TOOL_DISPATCH avoids a circular import (registry imports
    write_tools, which doesn't import coach). The tool_allowlist starting set
    is the full dispatch table minus set_topic_status (per §16.5 locked
    decision: coach does not write topic_status in S4).
    """
    from app.services.tools.registry import TOOL_DISPATCH

    return BotSpec(
        bot_id="coach",
        prompt_renderer=_coach_prompt_renderer,
        step_instructions=_MIN_STEP_INSTRUCTIONS,
        display_name="Coach",
        primary_topic_slug="career",
        participants_shape="solo",
        read_scopes=ReadScopes(topics=frozenset({"career"})),
        write_scopes=WriteScopes(topics=frozenset({"career"})),
        cross_topic_policy=None,
        tool_allowlist=frozenset(TOOL_DISPATCH) - frozenset({"set_topic_status"}),
        bot_spec_version="1.1.0",
    )
