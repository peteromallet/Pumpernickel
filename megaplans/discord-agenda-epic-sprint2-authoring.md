# Sprint 2 — Discord Agenda Authoring

Part 2 of the Discord-authored-agendas epic. Full design context + sense-check
findings: `megaplans/discord-agenda-authoring-brief.md`. **Depends on Sprint 1**
(`discord-agenda-epic-sprint1-identity.md`): real live-voice identity + a
user-scoped session-list endpoint must exist, and the web user id must equal the
Discord `ctx.user.id`. Do not start until that hand-off contract is written.

## Outcome

Let the Discord bot the user talks to **build and edit the agenda (a list of
topics) for a live voice conversation by chat**. The created conversation lands at
status `ready` and appears in the live voice web app (via Sprint 1's discovery),
ready to start — the same end state the web prep path produces.

## Scope

IN:
- Tools on the existing Discord agent registry: `read_conversation_plan`,
  `list_conversation_plans`, `create_conversation_plan`, `update_conversation_plan`.
- A markdown ⇄ agenda-items converter (internal model↔DB representation).
- A bounded hot-context "Pending live conversations" section so the bot remembers
  plans it just made.
- Tests (see Done criteria).

OUT (anti-scope):
- `discard_conversation_plan` / cancel — **fast-follow, not this sprint** (design
  brief notes it's cheap; keep this sprint focused).
- Conversation **notes** (`conversation_notes`), reading completed-conversation
  outcomes, "mark done", "start from Discord" — out per the design brief.
- **Mid-call (`status='active'`) agenda editing** — forbidden; would corrupt live
  turn state.
- Any auth / identity / discovery work — owned by Sprint 1.

## Locked decisions

- **Direct-write**: the bot's bullets *are* the agenda; `create_conversation_plan`
  writes `conversation_items` directly and lands the conversation at `ready`.
  (Not prep-handoff.)
- **Markdown is the internal representation only** (model↔DB tool payload). The
  user sees/edits a numbered list in chat, never a pasted markdown document.
- `user_id = ctx.user.id`; `bot_id` and `topic_id` derive from `ctx` /
  `primary_topic_id_for` — no bot/topic prompting.
- Tools must be placed where a **spoken confirmation can be paired with the write**
  — NOT the `record` step (which forbids user-facing text, `app/bots/mediator.py:75`).
- Edit reconciliation = delete `kind='planned'` items and re-insert from the
  revised plan; hard-guarded to `status IN ('preparing','ready')`.

## Parity requirements (to match the web-prep end state)

- Resolve `topic_id` via `primary_topic_id_for` exactly as the router does
  (`live_voice.py:363`), with its try/except — a NULL topic makes the turn loop
  skip hot-context load (`turn_loop.py:313`), degrading the call. Fail loudly
  rather than ship a context-less conversation.
- Derive `mode` from `prep_summary` presence (`steered` if non-empty else `open`),
  matching `prep.py:708` — not from a goal heading.
- Insert `conversation_items` **before** setting `conversations.current_item_id`
  (FK ordering).
- Reuse `Agenda`/`AgendaItem` (`app/services/live/schemas.py:60`) so invariants
  hold: ≥1 item, unique ids, resolvable refs, ≥1 `priority='must'` (promote the
  first item). Apply defaults `speaker_scope='both'`,
  `coverage_evidence_required='explicit_answer'`.
- Skipping the `live_prep_brief` artifact is acceptable (debrief degrades
  gracefully, `debrief.py:434`).

## Open questions (resolve in plan; low residual)

1. Exact step/allowlist placement for the plan tools so write+spoken-confirmation
   happen together (the `read` step allows `send_message_part`; a narrow
   plan-scoped write carve-out there is the candidate). Confirm against
   `app/services/tools/registry.py:408,418` and `app/bots/mediator.py:52,75`.

## Constraints

- Every tool enforces an explicit `EXISTS` ownership check against `ctx.user.id`
  (tools run as service role, bypassing RLS) — mirror `write_tools.add_memory`.
- The bot is a chat-bubble persona; surfaced plans are numbered plain-text lists,
  edits are confirmed by echoing the new list. Create requires explicit user
  confirmation before landing `ready`.
- Hot-context section must be bounded (`LIMIT 5`, statuses `('preparing','ready')`,
  scoped `user_id`+`bot_id`+`topic_id`) and added to the render-budget eviction
  list; mirror the `upcoming_items` pattern (`hot_context.py:~1317`).

## Done criteria

- `create_conversation_plan` from a Discord turn lands a `ready` conversation with
  agenda items that `GET /sessions/{id}/card` reports as startable
  (`status==ready && items>0`), satisfying `Agenda` invariants, with `topic_id`
  and `mode` matching the prep path.
- The created conversation is discoverable by the owning user via Sprint 1's list
  endpoint.
- `update_conversation_plan` reconciles edits and is rejected while `active`.
- `read_/list_conversation_plan` return data the bot renders as a spoken summary.
- Ownership guard rejects another user's conversation on every tool.
- New hot-context section shows pending/ready conversations within the bound.
- Markdown↔items round-trip test passes.

## Touchpoints

- `tool_schemas.py` — I/O models
- `app/services/live/plan_markdown.py` (new) — converter, reusing `AgendaItem`
- `app/services/tools/write_tools.py` (`add_memory:883` template), `read_tools.py`
- `app/services/tools/registry.py` — `TOOL_DISPATCH`, `TOOL_DESCRIPTIONS`, step
  placement, mediator `tool_allowlist`
- `app/services/hot_context.py` — `:472` assembly, `:1317` upcoming_items pattern,
  `:686` topic filter, budget eviction list
- `app/services/live/{schemas.py:60, prep.py:708/729, turn_loop.py:313, status.py}`
  — parity references
