"""Shared prompt text for per-bot partner sharing."""

from __future__ import annotations


PENDING_PARTNER_SHARING_PROMPT_SLOT = """\
Partner sharing is undecided for this user and this bot. Unless the turn
is crisis or time-critical, raise the choice naturally this turn: whether
this bot may share carefully selected, non-sensitive summaries from this
domain with the user's partner. Do not share this bot's memories or
distillations with the partner until the user explicitly opts in. If the
user gives an explicit yes/no choice, record it in the record step by
calling `set_partner_sharing(opt_in=true)` or
`set_partner_sharing(opt_in=false)`.
""".strip()


__all__ = ["PENDING_PARTNER_SHARING_PROMPT_SLOT"]
