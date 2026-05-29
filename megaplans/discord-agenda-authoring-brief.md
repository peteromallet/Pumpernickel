# Discord-Authored Conversation Agendas — Brief

> Status: design, sense-checked, **decisions locked** → runs as a **two-sprint
> epic**. Sprint 1 builds real live-voice identity + user-scoped discovery (the
> dependency below); Sprint 2 builds the agenda-authoring tools. Create strategy:
> **direct-write** (locked). See *Critical dependency* and *Epic structure*.

## Outcome

Let the Discord bot you talk to **build and edit the agenda for a live voice
conversation by chat**, so that when you open the live voice web app the
conversation is already prepped and ready to start — the same end state the web
prep path produces.

Mental model: a **conversation plan = a list of topics** to cover. From Discord
you can **see**, **create**, and **edit** that list in natural conversation. The
bot works in a markdown representation *internally* (model↔DB); the user sees and
edits a short numbered list in chat, never a pasted document.

Assumption (in scope): plans are authored **before** the call. Mid-call editing
is out of scope (it would corrupt live turn state — verified below).

## Critical dependency: discovery & identity (resolve first)

The headline promise — "open the app and the conversation is sitting there" —
**does not work on the current code**, for two reasons:

1. **Identity mismatch.** The web live-voice surface does not authenticate the
   real user; it resolves the owner via `_resolve_test_user_id()` — a fixed env
   UUID / placeholder `0000…0001` (`live_voice.py:134, 285, 354`). A Discord turn
   carries the **real** `mediator.users.id` as `ctx.user.id`
   (`turn_context.py:82`). A bot-created conversation owned by the real user is
   invisible to a web app browsing as the test user.
2. **No discovery endpoint.** Every conversation query is keyed by `id`; there is
   **no user-scoped "list my conversations"** endpoint. The web app only reaches
   a conversation if it already knows the `session_id`. (RLS is not the gate — the
   card/get/WS handlers all read via the service-role pool by `session_id` with
   no ownership check; `live_voice.py:488, 1493`, WS at `:1535` verifies a JWT but
   never compares it to `conversation.user_id`.)

**Implication:** this feature is only meaningful once the live-voice app (a) keys
ownership off the real authenticated user and (b) exposes a user-scoped session
list the UI loads on entry. **Decision (locked): do it properly** — this work
becomes **Sprint 1** of the epic; the agenda tools (Sprint 2) depend on it and
write `user_id = ctx.user.id`.

## Epic structure

| Sprint | Outcome | Hand-off artifact |
|---|---|---|
| **1 — Identity & discovery** | Live-voice ownership keyed off the real authenticated user (retire `_resolve_test_user_id` on the live surface); a user-scoped "list my conversations" endpoint; UI loads it on entry; WS/card/get handlers enforce `conversation.user_id == authed user`. | The auth/ownership convention + the list-sessions endpoint contract that Sprint 2 cites. |
| **2 — Agenda authoring** | The Discord bot tools (read/create/edit plan), markdown↔items converter, hot-context "pending conversations" section, tests. Create writes `user_id = ctx.user.id`. | The shipped feature. |

Sprint 2 must not start until Sprint 1's auth model + endpoint contract are
written down (the chain enforces ordering).

## The existing structure this plugs into

A conversation "waiting" in the app is a `mediator.conversations` row at status
**`ready`** with ≥1 `mediator.conversation_items` row. The web app reaches that
via:

1. `POST /api/live/sessions` (`live_voice.py:322`) — inserts the conversation at
   **`preparing`**, returns immediately. (A `skip_prep` variant, `:391`, goes
   straight to `ready` with no items — proof that "ready with a supplied agenda"
   is a supported end state.)
2. Background agentic prep (`prep.py:253`) runs Opus → `submit_live_brief` with an
   `Agenda` (`schemas.py:60`).
3. `_persist_prep_success` (`prep.py:665`) inserts items + a `live_prep_brief`
   artifact, flips `preparing → ready`, sets `prep_summary` and `current_item_id`.
4. UI polls `GET /sessions/{id}/card` (`live_voice.py:477`); enables **Start**
   when `status === "ready" && items.length > 0`.

The card endpoint and turn loop read **only** `conversations` + `conversation_items`
— no artifact join — so direct-written rows are consumed unchanged.

### `conversation_items` columns to populate (`prep.py:729`, `turn_loop.py:244`)

| Column | Req | Notes / default |
|---|---|---|
| `id` | yes | generated UUID |
| `conversation_id` | yes | the new conversation |
| `kind` | yes | `planned` |
| `title` | yes | the bullet (1–200 chars) |
| `priority` | yes | `must`/`should`/`optional` — default `should` |
| `speaker_scope` | yes | `primary`/`partner`/`both` — default `both` |
| `coverage_evidence_required` | yes | `explicit_answer` (default) / `emotional_shift` / `concrete_decision` / `blocker_named` |
| `order_hint` | yes | 0,1,2,… |
| `next_item_ids` | yes | `uuid[]`, may be `{}` |
| `intent`, `ask`, `done_when` | no | recommended |
| `theme_id` | no | nullable |

`Agenda` validation (reuse it) requires: ≥1 item, unique ids, resolvable
`first_item_id` + `next_item_ids`, and **≥1 item with `priority='must'`**. The
create tool promotes the first item to `must` so a conversational user never has
to think about it.

## Design

Tools added to the existing Discord agent registry; no new auth, API, or queue.
Each handler gets `TurnContext` (`ctx.pool`, `ctx.user.id`, `ctx.bot_id`) and
follows the `add_memory` write-tool template (UUID parse, ownership `EXISTS`
guard, `_log_tool_call`).

| Capability | Tool | Behaviour |
|---|---|---|
| See | `read_conversation_plan` / `list_conversation_plans` | Read items / list the user's pending plans. Returns are **spoken summaries**, not raw markdown. |
| Create | `create_conversation_plan` | Parse plan → INSERT conversation (`ready`) + items + set `current_item_id`. |
| Edit | `update_conversation_plan` | Reconcile revised plan into items (markdown = source of truth). Guarded to `preparing`/`ready` only. |

### Tool placement & confirmation (wiring correction)

Write tools normally run in the **`record`** step, which is **forbidden from
emitting user-facing text** (`mediator.py:75`) — registering the plan tools there
would make the bot write **silently**, with no "done" to the user. Fix:

- Place the plan tools where a spoken confirmation can be paired with the write —
  the **`read` step** allows `send_message_part`; add a narrow, plan-scoped write
  carve-out there (or an equivalent step) so the bot writes *and then confirms*
  reflecting the real tool result. Do **not** drop them into `record`.
- `bot_id` and `topic_id` are derived from `ctx` (the persona the user is DMing)
  via `primary_topic_id_for` — no bot/topic prompting needed (`registry.py:336`,
  matching `live_voice.py:363`).

### Markdown ⇄ agenda mapping (internal representation)

Internal tool payload only — **not** the conversational surface:

```
Goal: <what I want from this conversation>   → steering_text + derived prep_summary
- First thing to cover                        → item.title (order_hint 0, promoted to `must`)
  - what I want out of it                       → item.intent
- Second thing                                 → item.title (order_hint 1)
```

- Top-level bullet → one `conversation_item` (`kind='planned'`); sub-line →
  `intent`. `order_hint` from position; `next_item_ids` chained or left empty.
- Defaults: `priority='should'` (first → `must`), `speaker_scope='both'`,
  `coverage_evidence_required='explicit_answer'`.

### Create — what `create_conversation_plan` writes (parity-correct)

1. `INSERT mediator.conversations (id, user_id, bot_id, mode, steering_text,
   status='ready', prep_summary, topic_id)`.
   - `user_id` per *Critical dependency* (A: `ctx.user.id`; B: test-user value).
   - **`topic_id`: resolve via `primary_topic_id_for` with the router's try/except**
     — a NULL topic makes the turn loop skip hot-context load entirely
     (`turn_loop.py:313`), degrading the call. Fail loudly rather than silently
     ship a context-less conversation.
   - **`mode`: derive from `prep_summary` presence** (`steered` if non-empty else
     `open`), matching `prep.py:708` — not from a goal heading.
2. `INSERT` each item into `conversation_items` (columns above) — **before** the
   next step (the `current_item_id` FK requires the items to exist).
3. `UPDATE conversations SET current_item_id = <first item>`.

Skipping the `live_prep_brief` artifact is **safe**: debrief loads it in
try/except and only uses it additively (`debrief.py:434`); card/turn loop never
touch it. The only difference is the debrief lacks an original brief to compare
coverage against — accepted, harmless.

### Edit — reconciliation

Delete existing `kind='planned'` items and re-insert from the revised plan, then
re-set `current_item_id`. Safe pre-call (no per-item coverage state exists yet).
**Hard-guard to `status IN ('preparing','ready')`** — verified necessary: the
live loop mutates items by UUID (coverage at `turn_loop.py:478`, dynamic inserts
at `:493`, `current_item_id` re-point at `:544`) with no locking, so a mid-call
delete-and-reinsert would orphan coverage writes and dangle `current_item_id`.

### Ownership / safety

Every tool verifies the conversation belongs to `ctx.user.id` via explicit
`EXISTS` (tools bypass RLS as service role) — same as existing write tools.

## Hot context integration

Today hot context (`hot_context.py:472`) has **zero** references to
`conversations`/`conversation_items` — so right after the bot creates a plan, the
**next** Discord turn won't know it exists (risking duplicates, can't reference or
offer to edit it). Add a bounded proactive surface:

- **New "## Pending live conversations" section**, mirroring the existing
  `upcoming_items` pattern (`hot_context.py:~1317`). One short line per
  conversation: `id`, `status`, title (`prep_summary`→`steering_text`), item count.
  Not the full agenda.
- Query: `WHERE user_id=$1 AND bot_id=$2 AND topic_id=$3 AND status IN
  ('preparing','ready') ORDER BY created_at DESC LIMIT 5`. New small query; add to
  the render-budget eviction list so it sheds first under pressure.
- **Completed conversations: no new section.** `save_review` writes kept debrief
  notes through to `observations` (`synthesis.py:242`), which hot context already
  loads. *Caveat:* that write-through does **not** stamp `artifact_topics`, and
  hot-context observations are topic-filtered (`hot_context.py:686`) — so debrief
  outcomes may be invisible to topic-scoped context. Pre-existing bug; flag as a
  dependency, don't assume coverage.

## Lifecycle & related verbs

Status machine (`status.py`): `preparing → ready → active → debriefing →
review_pending → completed`, with `prep_failed`/`debrief_failed` retry branches.
`ready → active` happens **only on WebSocket connect** (`live_voice.py:1584`).

| Verb | Scope | Notes |
|---|---|---|
| read / list / create / edit plan | **now** | The feature. `list` needs a net-new `WHERE user_id` query. |
| **discard** an unwanted plan | **fast-follow** | Today a `ready` plan is **permanently stuck** (only exit is WS start). `'discarded'` is already a legal CHECK value — one guarded `UPDATE … SET status='discarded' WHERE status IN ('ready','preparing','prep_failed')`. Lands with the manage trio (shares listing + ownership scaffolding). |
| read a completed conversation's outcome | **later** | No structured read of debrief/kept-notes exists; partially covered by observations in hot context. Defer. |
| "start it now" from Discord | **never** | Impossible by design — live call needs the web mic/WS. Bot says "open the app, it's ready." |
| "mark done" without a call | **never** | Debrief owns completion; a bot-side close would produce an empty, lying terminal state and capture no outcome. |

Disambiguation: there is **no `title` column** on `conversations`. `list_conversation_plans`
should synthesize a label from `topic` title + `prep_summary`/`steering_text` +
first item title + `created_at`, returning `{id, topic_label, prep_summary,
first_item_title, status, created_at}` so the bot can confirm by id in chat.

## Conversational UX

This bot speaks in short chat bubbles, not documents (`mediator.py:52`). So:

- Surface plans as a **numbered plain-text list in one bubble**, never a fenced
  markdown block or `Goal:` heading. Numbers give stable edit referents.
- **Create requires explicit confirmation** before landing `ready` (it's a real
  user-visible artifact). **Edits confirm-by-echo** (apply, re-read the new list,
  "look right?") — except "delete the whole plan", which gets a yes/no.

Happy path:

> **User:** Set up a money conversation with my partner — joint account, holiday
> budget, and whether we start saving for a deposit.
> **Bot:** Here's the plan I'd line up:
> 1. The joint account — how it's working
> 2. Holiday budget this year
> 3. Whether to start saving for a deposit
> Set it as ready, or tweak first?
> **User:** Go for it.
> **Bot:** Done — it's waiting in the app whenever you want to start.

Edits: imperatives against the numbers ("drop #2", "add a point about who pays the
bills", "reword the first one"), bot echoes the new numbered list. Discovery
("what's waiting?") → spoken summary from `list_conversation_plans`. Edge cases:
vague request → ask one focusing question (an empty agenda fails `Agenda`
validation anyway); multiple candidates → list numbered and ask which; mid-call
edit → refuse plainly and offer to note it for next time; write fails after a
spoken "done" → only possible if write+confirm aren't in the same step, which is
why they must be (see *Tool placement*).

## Implementation checklist

1. **Resolve the Critical dependency** (auth + user-scoped session list, or the
   dev-only test-user bridge). Nothing below is user-visible without it.
2. **Schemas** (`tool_schemas.py`): I/O models for the tools.
3. **Markdown↔items converter** (`app/services/live/plan_markdown.py`): reuse
   `AgendaItem`/`Agenda` so invariants hold; apply defaults + promote first→`must`.
4. **Handlers**: `create_/update_/read_/list_conversation_plan` — `add_memory`
   template; resolve `topic_id` + `mode` per parity rules; ownership guard; status
   guard on edit.
5. **Tool registration & placement** (`registry.py`): `TOOL_DISPATCH`,
   `TOOL_DESCRIPTIONS`, and a step that **allows a spoken confirmation alongside
   the write** (not `record`); confirm the mediator's `tool_allowlist` permits them.
6. **Hot context** (`hot_context.py`): "## Pending live conversations" section +
   bounded query + eviction-list entry.
7. **Tests**: markdown round-trip; create lands a `ready`, startable conversation
   (`/card` reports it) with `Agenda` invariants; topic_id/mode parity with prep;
   edit reconciles + blocked while `active`; ownership guard rejects others'.
8. **Fast-follow:** `discard_conversation_plan` (+ surface in `list`).

## Out of scope

- Mid-call (`active`) agenda editing — corrupts live turn state.
- Starting a call / "mark done" from Discord (see Lifecycle — never).
- New external (non-Discord) agent access / API keys / job queue.
- Topic taxonomy (`mediator.topics`) editing.
- Conversation **notes** (`conversation_notes`) — separate from the agenda.

## Decisions (locked)

1. **Dependency path: do it properly (epic).** Sprint 1 builds real identity +
   discovery; Sprint 2 the agenda tools. Not a dev bridge.
2. **Create strategy: direct-write.** The bot's bullets *are* the agenda; the tool
   writes `conversation_items` directly at `ready`. Not prep-handoff.

## Key file references

- `app/routers/live_voice.py:134` (`_resolve_test_user_id`), `:322` (`POST /sessions`),
  `:391` (`skip_prep`), `:477` (`/card`), `:1584` (WS start)
- `app/services/live/prep.py:253`/`:665`/`:708` — prep job, persist, mode derivation
- `app/services/live/schemas.py:60` — `Agenda`/`AgendaItem` invariants
- `app/services/live/turn_loop.py:244`/`:313`/`:478` — item read, NULL-topic skip, coverage mutate
- `app/services/live/synthesis.py:242` — debrief→observations write-through
- `app/services/live/status.py` — status machine + legal `discarded`
- `app/services/hot_context.py:472`/`:1317`/`:686` — assembly, upcoming_items pattern, topic filter
- `app/services/tools/registry.py:336`/`:408`/`:418` — topic derivation, step tool gating
- `app/services/tools/write_tools.py:883` (`add_memory`) — write-tool template
- `app/bots/mediator.py:52`/`:75` — chat-bubble persona; record = no user text
- `app/services/turn_context.py:82` — `ctx.user.id` is the real user
