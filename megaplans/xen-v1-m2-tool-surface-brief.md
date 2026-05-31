# Xen v1 — M2: Agent tool surface (SIMPLE nav + COMPLEX search)

> Part of the Xen v1 epic (`xen-v1-epic-plan.md`). Depends on M1 (the retriever
> + searchable view). This is where the goal's "implemented fully into the app"
> and "navigable in an intuitive way" land for agents — **both** the user's
> SIMPLE navigation examples and the COMPLEX semantic search, as first-class
> registry tools available to every relevant bot.

## Outcome

Every relevant agent (mediator, tante_rosi, hector, habits, coach) has, in the
tool registry, a small set of **dead-simple navigation verbs** (cheap SQL, no
embeddings) AND a **single rich search verb** (M1's hybrid retriever) — with
stable cursors, resolved speaker labels, and edit/retraction surfacing, rendered
spoken-summary-friendly for live voice.

## Scope

IN — the tools (exact signatures; schemas added to `tool_schemas.py`, dispatch +
descriptions + allowlists in `app/services/tools/registry.py`, handlers in
`app/services/tools/read_tools.py`):

**SIMPLE navigation (cheap SQL over `v_searchable_messages`, NO embeddings).**
These are the user's explicit examples. Make them first-class and trivially
callable; each is one `ORDER BY sent_at` query with scope+visibility filters
identical to today's `search_messages` (`read_tools.py:469-483`).

- `messages_before(anchor, n) -> { messages[], cursor }`
  - `anchor`: a `message_id` **or** the literal `"current"`. `"current"` resolves
    to the **edge of the hot-context window** — i.e. the oldest message currently
    in the last-20 recent window (M3 supplies this anchor; see below) — so
    "the most recent messages before the current ones in hot context" is a
    direct call. Returns the `n` messages immediately *older* than the anchor,
    `sent_at DESC`. Covers "before a certain message ID" and
    "recent-before-current" in one verb.
- `messages_after(anchor, n) -> { messages[], cursor }` — symmetric (scroll
  forward toward the present).
- `open_thread(around, n) -> { messages[], cursor }`
  - `around`: `message_id | iso_date | "latest"`. Lands a window centered on the
    anchor (n/2 before, n/2 after), returning a cursor for continued scroll.
    This is "jump-to".
- `scroll(cursor, direction, n) -> { messages[], cursor }`
  - `direction`: `older | newer`. The scrollback primitive. Cursor is opaque
    `{anchor_sent_at, anchor_id, scope}` (per `xen-retrieval-brief.md:174`);
    stable over message identity (a mid-window edit can't shift the cursor).
- `topic_recent(topic_id?, n) -> { messages[], cursor }`
  - Most-recent `n` messages in the current (or named) topic scope. "Topic-scoped
    recent." Defaults to `ctx.primary_topic_id`.

**COMPLEX search (M1 retriever).**
- `search(query, mode, scope, limit, cursor?) -> { hits[], truncated, next_cursor? }`
  - **Paginated result set.** Returns up to `limit` ranked hits plus a
    `next_cursor` when more exist (`truncated=true`). The agent pages by calling
    `search` again with `cursor=next_cursor` (same query/mode/scope) to get the
    next page — a stable rank-offset cursor over the fused ranking (opaque
    `{query_hash, rank_offset, scope}`), so paging is deterministic for a fixed
    query. This is paging the *result list*; the per-hit `cursor` (below) is the
    separate handle for scrolling the *thread around a hit* via `scroll`. `null`
    `next_cursor` = last page.
  - `mode`: `exact | semantic`. `exact` = keyword-only (verbatim-safe — the only
    mode whose snippets may be presented as quotes,
    `xen-retrieval-brief.md:88-91`). `semantic` = full RRF hybrid.
  - `scope`: `thread | topic` (current dyad+topic). **No `scope=all` cross-dyad
    search in v1** — cross-scope reach is a separate opt-in capability
    (`xen-retrieval-brief.md:46-48`); omit it.
  - Each hit: `{ message_id, snippet, cursor, match_type (exact|semantic|both),
    speaker_label, speaker_user_id, direction, sent_at, charge, edited_at,
    edit_history_original?, retracted (deleted_at flagged not dropped — but note
    v1 search reads the searchable view which excludes deleted; retraction
    surfacing applies to edits and to messages retracted *after* a hit cursor
    was opened), why_matched }`.
  - **Speaker labels resolved server-side** (not raw UUIDs — attribution is the
    highest-stakes field, `xen-retrieval-brief.md:92-93`). Resolve from
    `ctx.user`/`ctx.partner` + direction, reusing `_message_thread_owner_id`
    (`read_tools.py:676-681`).
- `search_messages` (today's ILIKE tool) is **superseded**: re-point its handler
  to `search(mode=exact)` keeping its existing input schema for back-compat, OR
  deprecate in favor of `search`. Recommend: keep the name, swap the
  implementation to the hybrid `exact` path so existing prompts/allowlists keep
  working; add `search`'s richer schema as the forward tool.

**Rendering / result shape.** Results are **spoken-summary friendly** (the live
voice agent must be able to read a hit aloud without UUIDs or markup): each hit
carries a one-line human-readable header (`"You, Tuesday 9:14pm: ..."`) plus the
machine cursor. Cursors are returned so the agent can chain `scroll` without
re-querying.

**Registry wiring (`registry.py`).**
- Add all new tools to `TOOL_DISPATCH`, `TOOL_DESCRIPTIONS`, and
  `READ_PHASE_TOOLS` (they are reads — `registry.py:333-359`). They flow into
  `READ_TOOLS_FOR_STEP`, `CONSULT_PHASE_TOOLS`, `RECORD_READ_TOOLS`, and
  `LIVE_PREP_TOOLS` automatically via the existing set algebra.
- **Not bot-exclusive** — these belong to every bot, so they go in the shared
  read set and are NOT added to `BOT_EXCLUSIVE_TOOLS` (`registry.py:275-278`).
  Per-bot `tool_allowlist` intersection (`_step_allowed`, `registry.py:741-742`)
  still applies; ensure each bot spec's allowlist includes them (or is `None` =
  all).
- Nav tools need no `READ_BEFORE_WRITE` entries (they're pure reads).
- **Descriptions are written to actively invite use (the "push to search" nudge).**
  Each tool's `TOOL_DESCRIPTIONS` entry tells the agent *when to reach for it* —
  explicitly: "when the hot-context 'Previous on this topic' gist is insufficient,
  open the thread / scroll / search to get the full exchange before answering."
  The goal is proactive context-gathering, not passive tool availability. Pairs
  with M3's gist-with-handles rendering; M4 grades the behavior.

OUT (anti-scope):
- No `scope=all` cross-dyad search (deferred opt-in capability).
- No Surface 2 (`fetch_span`/`span_manifest`/`summarize_span`) or Surface 3
  (`count_mentions`/`charge_trend`/…) tools — out of the epic.
- No write/forget tools here (suppress is enforced in M1's view; a user-facing
  forget UI/tool is a later slice — `xen-retrieval-brief.md:199-209`).
- No `verify_quote` tool in v1 (nice-to-have; `mode=exact` already constrains
  quoting). Note as a follow-up.
- No web-UI code — but the cursor/scroll contract defined here is what a future
  web scrollback reuses, so keep it clean and documented.

## Locked decisions

- SIMPLE nav = cheap SQL, no embeddings, identical scope+visibility filters to
  `search_messages` today. COMPLEX = M1 hybrid.
- `mode=exact` is the only quote-safe mode.
- Two distinct cursor kinds: the **nav cursor** (opaque
  `{anchor_sent_at, anchor_id, scope}`, stable over identity — for `scroll`) and
  the **search-page cursor** (opaque `{query_hash, rank_offset, scope}` — for
  paging a search result list). Both are opaque strings the agent passes back
  verbatim; never conflate them.
- `anchor="current"` is resolved against the hot-context window edge that M3
  publishes into `TurnContext` (see M3) — do NOT re-derive "current" inside the
  tool from scratch.
- Tools are universal (all bots), gated only by each bot's existing
  `tool_allowlist`; not added to `BOT_EXCLUSIVE_TOOLS`.

## Open questions

1. Whether to keep `search_messages` as an alias or hard-deprecate. (Recommend
   alias → `search(mode=exact)` for zero prompt churn.)
2. Default `n` for nav verbs (recommend 10) and `limit` for `search` (reuse the
   existing `SearchMessagesInput.limit` default).

## Constraints

- All handlers go through M1's `v_searchable_messages` view — never raw
  `messages` — so suppress/delete/visibility are honored by construction.
- Transaction-per-call (6543 pooler) — each tool is one self-contained query (or
  query + `SET LOCAL ef_search` in the same txn for `search`).
- Cursors must be encodable as JSON in tool output (opaque string the agent
  passes back verbatim).

## Done criteria

- All nav verbs + `search` registered, dispatch wired, descriptions written,
  appear in the read-phase allowlist, and are callable by mediator + at least
  one solo bot (tante_rosi) in tests.
- `messages_before(anchor="current")` returns exactly the messages older than
  the published hot-context window edge (asserted against M0's nav-eval
  exact-match cases).
- `search(mode=semantic)` returns RRF hybrid hits with resolved speaker labels,
  snippets, cursors, and match_type; `mode=exact` returns keyword-only,
  quote-safe hits.
- A suppressed/deleted/partner-private message never appears via any nav or
  search verb (test).
- `scroll` chains via cursor without re-query drift across a mid-window edit
  (test).
- `search` paginates: a `next_cursor` from page 1 fed back as `cursor` returns the
  next, non-overlapping page of ranked hits for the same query; `next_cursor` is
  `null` on the last page (test).
- Tool descriptions explicitly cue proactive use ("when the hot-context gist is
  insufficient, search/open for the full exchange") — not just mechanical
  signatures (review-checked; reflected in M4's proactive-search scenario).
- M0's nav-eval suite passes at 100% exact-match against these handlers (via the
  DB-backed adapter).

## Touchpoints

- Modify: `tool_schemas.py` (new Input/Output models + a shared `MessageNavHit`
  reusing `message_hit`, `common.py`); `app/services/tools/read_tools.py` (new
  handlers; reuse `_partner_share_by_user_for_current_bot`,
  `raw_message_visibility`, `_message_thread_owner_id`, `_ctx_timezone`);
  `app/services/tools/registry.py` (`TOOL_DISPATCH`, `TOOL_DESCRIPTIONS`,
  `READ_PHASE_TOOLS`).
- Reuse: M1 `app/services/retrieval.py::hybrid_search` for `search`.
- Reference: M3 publishes the "current window edge" anchor into `TurnContext`.
- Tests: extend `tests/test_tools.py`; M0 nav-eval is the spec.

## Anti-scope (explicit)

No cross-dyad search, no bulk/aggregation tools, no forget tool, no web UI, no
verify_quote in v1.
