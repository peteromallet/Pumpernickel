# Mediated Follow-Through — Brief

## Outcome

Make Vea reliably turn meaningful relationship grievances into a mediated follow-through loop using existing primitives where possible: bridge candidates, partner nudges, topic status, and follow-up tracking.

The intended product behavior is:

- When one partner brings a meaningful grievance or unresolved relational issue, Vea can create a partner-facing bridge candidate.
- That bridge can trigger or strongly guide a scheduled check-in turn for the other partner, with safe neutral context derived from the bridge candidate.
- Both partners' hot context should show high-level unresolved mediated issues until they are addressed, declined, expired, or otherwise resolved.
- The loop should push toward understanding, direct repair, and closure, not become a grievance relay system.

## Scope

In scope:

- Inspect the existing mediator prompt, bridge candidate tools, partner nudge scheduling, hot context, topic status, and tests.
- Prefer composing existing abstractions over adding a new "episode" table.
- Treat `bridge_candidates` as the likely spine of mediated follow-through unless the plan proves that is insufficient.
- Add prompt instructions that explain when and how Vea should use mediated follow-through.
- Ensure partner-side scheduled nudges can carry enough bridge-linked context for the receiving agent to understand what it is checking in about, without leaking private raw grievance text.
- Make unresolved mediated issues visible at a high level in hot context for both source and target sides.
- Preserve existing target-facing behavior: ready `message_partner` bridge candidates stay visible until resolved.
- Define and implement lifecycle guidance for marking a bridge addressed, declined, blocked, expired, or left open.
- Add tests/evals for the behavior and privacy boundaries.

Out of scope unless the plan proves it is necessary:

- A new first-class `mediation_episodes` table.
- A broad refactor of the agentic turn loop.
- A new external UI.
- Changing unrelated live voice, pregnancy, coach, Hector, or habits behavior.
- Relaxing OOB/privacy rules.

## Locked Decisions

- Do not expose private raw grievance text to the other partner.
- `bridge_candidate.shareable_summary` is the safe partner-facing text surface.
- `internal_note` may remain source-side/private and must not appear in target hot context.
- `schedule_partner_checkin.nudge_note` must remain short, neutral, and recipient-visible.
- `schedule_partner_checkin.reason` remains audit-only and must not be rendered to the recipient.
- `topic_status(scope="dyad")` is the shared high-level status surface for the relationship topic.
- Existing resolution states on bridge candidates are preferred over inventing new status enums.
- If multiple unresolved bridge candidates exist, hot context must remain bounded and deterministic.

## Open Questions For The Planner

- Is it enough to link partner nudges to bridge candidates via `scheduled_jobs.context.bridge_candidate_id`, or is another lightweight field needed?
- Should source-side hot context show outgoing unresolved bridge candidates directly, or should it rely on `topic_status` plus `list_bridge_candidates`?
- Should creating a `ready/message_partner` bridge automatically schedule a partner check-in in the tool implementation, or should this remain model-orchestrated through prompt instructions and separate tool calls?
- What cap/order should source-side and target-side unresolved issue rendering use when several are open?
- What exact prompt language keeps Vea from over-contacting the partner or becoming a messenger app?

## Constraints

- Maintain privacy and OOB guarantees.
- Keep implementation narrowly scoped to mediator relationship behavior.
- Preserve existing tests unless their expectations are intentionally updated for the new behavior.
- Avoid new schema unless there is a clear, documented reason existing bridge/status/scheduled primitives cannot support the loop.
- Do not carry unrelated dirty work from the source checkout into the implementation worktree.

## Done Criteria

- Mediator prompt has a clear "mediated follow-through" decision tree and lifecycle instructions.
- A meaningful partner-facing bridge can be tied to a partner check-in turn.
- Partner check-in hot context includes safe bridge-linked context when present.
- Both sides can see high-level unresolved mediated issue state until resolution.
- Multiple unresolved issues are capped and ordered predictably.
- Tests cover:
  - target-side ready bridge remains in hot context until resolved;
  - source-side unresolved mediated issue visibility;
  - partner nudge linked to bridge candidate renders safe context but not private/audit fields;
  - multiple open bridges cap/order;
  - addressed/declined/sent/expired bridges drop out of automatic unresolved hot-context rendering as intended;
  - OOB/private/internal notes are not leaked.

## Touchpoints

- `app/bots/prompts/profiles/mediator.py`
- `app/bots/prompts/slots/partner_nudge.py`
- `app/services/tools/write_tools.py`
- `app/services/tools/read_tools.py`
- `app/services/tools/registry.py`
- `app/services/hot_context.py`
- `app/services/cross_thread_privacy.py`
- `tool_schemas.py`
- `tests/test_hot_context.py`
- `tests/test_partner_nudge_hot_context.py`
- `tests/test_tools.py`
- `evals/scenarios/` if prompt-level eval coverage is appropriate

## Anti-Scope

- Do not add autonomous partner outreach broadly across all bots.
- Do not make Vea send raw complaints to the partner.
- Do not make every grievance into a mediated loop; private reflection and direct-conversation coaching remain valid paths.
- Do not remove bridge candidates or replace them with a new abstraction unless the plan demonstrates a hard blocker.
