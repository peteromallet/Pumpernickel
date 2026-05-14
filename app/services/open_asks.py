from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class OpenAsk:
    key: str
    open_if: Callable[[Mapping[str, Any]], bool]
    example: str
    resolves_with: str


class _SafeFormatDict(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def render_open_asks(asks: Sequence[OpenAsk], state: Mapping[str, Any]) -> str:
    open_items = [ask for ask in asks if ask.open_if(state)]
    if not open_items:
        return ""
    lines = [
        "## Open asks",
        (
            "Things you don't know yet that you need to find out from the user. "
            "Work one in when there's a place to. One per turn. "
            "If they deflect or change subject, drop it for this turn."
        ),
        "",
    ]
    format_state = _SafeFormatDict(state)
    for ask in open_items:
        lines.append(f"- `{ask.key}` is not set.")
        lines.append(f'  Example: "{ask.example.format_map(format_state)}"')
        lines.append(f"  Resolves with: `{ask.resolves_with}`")
        lines.append("")
    return "\n".join(lines).rstrip()


def _get_bot_asks(bot_id: str) -> Sequence[OpenAsk]:
    if bot_id == "tante_rosi":
        from app.bots.prompts.tante_rosi import ASKS

        return ASKS
    if bot_id == "mediator":
        from app.services.prompts import VEAS_ASKS

        return VEAS_ASKS
    from app.services.prompts_solo import ASKS as SOLO_ASKS

    return SOLO_ASKS
