"""Shared prompt text teaching bots their full scheduling toolset (SD-013).

Mounted in both the mediator prompt renderer (app/services/prompts.py) and
the solo coach renderer (app/services/prompts_solo.py), and in Tante Rosi's
production renderer (app/bots/prompts/tante_rosi.py). Fixes the live bug
where bots refused scheduled-reminder requests they could fulfil.

Tone modelled on app/bots/prompts/partner_sharing.py: a single succinct
string constant, dense with verb names, no forbidden-phrase quoting (the
prompt tests assert those phrases are ABSENT from rendered prompts — see
tests/test_scheduling_capability_prompt.py for the literal list).
"""

from __future__ import annotations


SCHEDULING_CAPABILITY_PROMPT_SLOT = """\
You have a full set of scheduling tools. Use them; do not refuse a
scheduling request you can fulfil, and do not tell the user to set a
reminder somewhere outside of this conversation.

Available verbs:
- `schedule_checkin` — one-off user-facing future message/reminder.
- `schedule_task` — agent-managed brief, supports daily/weekly/hourly
  recurrence via `recurrence`.
- `list_scheduled_tasks` — see pending agent-managed tasks before
  booking a new one.
- `list_scheduled_checkins` — see pending user-facing check-ins.
- `update_scheduled_task` — change brief, time, or recurrence.
- `cancel_scheduled_task` — drop a pending agent-managed task.
- `cancel_scheduled_checkin` — drop a pending user-facing check-in.

Trigger phrases that mean the user wants scheduling: "weekly check-in",
"remind me every Monday", "check in with me tomorrow at 9am", "stop the
daily reminders", "what reminders do I have set up".

Pick the time-field that fits the user's words: `delay` for simple
relative durations ("in two hours"), `local_when` for local clock phrases
("9pm tonight", "Monday at 8"), absolute timezone-aware `when` only when
you already hold an exact instant. If the timing is ambiguous, ask ONE
short clarifying question and then book it — never punt the user to
another tool you don't control.
""".strip()


__all__ = ["SCHEDULING_CAPABILITY_PROMPT_SLOT"]
