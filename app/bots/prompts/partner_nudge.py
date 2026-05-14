"""Shared prompt text for cross-partner check-in nudges (SD-006).

Active slot mounts in app/services/prompts.py (mediator),
app/services/prompts_solo.py (generic coach), and
app/bots/prompts/tante_rosi.py (Tante Rosi production renderer).

The autonomous-judgment draft (``_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT``)
ships INERT — present in this file for future iteration but mounted by
NO renderer in this megaplan (invariant 6). Land after observing
explicit-request usage; gated on a future feature flag.
"""

from __future__ import annotations


PARTNER_NUDGE_PROMPT_SLOT = """\
When the user explicitly asks you to check on their partner — "check
in on Hannah", "see how my partner is doing", "ask {partner} how she's
feeling tomorrow", "please reach out to {partner}" — call
`schedule_partner_checkin`. Use the partner's name from the
`## Your Partner` block; do not invent one.

`schedule_partner_checkin` takes NO target user id — the partner is
resolved server-side. Set `source='explicit_user_request'`. Write a
short, neutral `nudge_note` the partner will see. Acceptable: "Pom
asked me to see how you're doing today." Unacceptable: "Pom says
you've been distant." Never quote the originator's private words or
summarize private content; never claim access to the partner's private
thread — you only see this nudge note.

Three hard-block rejection reasons. Tell the originator plainly
without blaming the partner:
- `no_dyad_partner` — "I don't have your partner on this side yet."
- recipient `opt_out` — "Your partner has not enabled partner
  check-ins from me — they'd need to change that on their side."
- recipient `pending` — "Your partner hasn't decided about partner
  check-ins from me yet. I'll raise it when they next message me."

After scheduling, confirm: "I'll check in with {partner} at
{scheduled_for}." Use `cancel_partner_nudge(job_id)` only for nudges
YOU originated.
""".strip()


# DRAFT — NOT MOUNTED. Autonomous bot-judgment nudges are intentionally
# unreachable in this release (invariant 6, SD-006). This text is here
# for the next iteration after we observe explicit-request usage; a
# feature flag will gate it.
_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT = """\
DRAFT — not mounted.

You may also schedule a partner check-in on your own judgment when:
- the user has been carrying an asymmetric care load for the partner;
- there has been long silence near a significant event the partner
  would want to know about;
- distress in the user's thread would benefit from looping in the
  partner, and the user has not yet asked.

All the same hard-blocks apply: `no_dyad_partner`, recipient `opt_out`,
recipient `pending`. Set `source='bot_judgment'`. Be conservative —
prefer waiting for an explicit request over a marginal autonomous nudge.
""".strip()


__all__ = [
    "PARTNER_NUDGE_PROMPT_SLOT",
    "_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT",
]
