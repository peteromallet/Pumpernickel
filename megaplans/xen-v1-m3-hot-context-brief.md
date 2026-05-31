# Xen v1 — M3: Hot-context integration for all agents

> Part of the Xen v1 epic (`xen-v1-epic-plan.md`). Depends on M1 (retriever) and
> M2 (nav cursor contract). **The heart of the goal** — "into the hot context of
> all the agents." Plugs search/navigation into `app/services/hot_context.py` so
> that, for every agent, the relevant *previous-on-this-topic* messages beyond
> the last-20 window are surfaced, bounded sanely, and the SIMPLE
> "before-current / before-ID" navigation is anchored to the window cleanly.

## Outcome

`build_hot_context` (`hot_context.py:535`) gains a new bounded section that
surfaces previous-on-topic messages a good system would want — topic-scoped
recent that fell out of the last-20 window **plus** semantically-relevant prior
messages tied to the current conversational state — without ballooning the token
budget, available to every bot. The hot context also publishes the **window edge
anchor** so M2's `messages_before(anchor="current")` has a defined meaning.

## Scope

IN:
- **New `HotContext` field + section: `relevant_prior` (name TBD).**
  - Added to the `HotContext` dataclass (`hot_context.py:37-61`) and rendered by
    `_render_with_counts` (`hot_context.py:1280+`) as a new `## Previous on this
    topic` block, after `recent_messages`, before synthesis blocks.
  - **Two query sources, merged + capped:**
    1. **Topic-scoped recent beyond the window**: the most-recent `K` messages in
       `(bot_id, topic_id)` that are *older* than the last-20 window edge (so the
       agent sees the immediately-preceding context that eviction dropped). Cheap
       SQL, mirrors the existing `message_rows` query (`hot_context.py:758-770`)
       with an extra `sent_at < window_edge` and a small `K` (e.g. 5).
    2. **Semantically-relevant prior**: call M1's `hybrid_search` with the
       *current conversational state* as the query (derived from the triggering
       message content, `trigger_rows`/`triggering_message_ids`) scoped to
       `(bot_id, topic_id)`, `mode=semantic`, excluding anything already in the
       last-20 window or in source (1). Top `J` (e.g. 5). This is "previous
       messages on this topic" by *meaning*, reaching past the window.
  - **Merge + bound:** union the two sources, dedup, cap to a single budget
    (e.g. ≤ 8 total). De-rank within budget by RRF score then recency. This cap
    is the M0 hot-context-inclusion budget — keep them equal.
- **Window-edge anchor published to `TurnContext`.**
  - Compute the oldest `message_id`+`sent_at` in `recent_messages` (the last-20
    window — `hot_context.py:758-770`, `reversed(message_rows)`).
  - Publish it on the turn context (e.g. `ctx.hot_context_window_edge` /
    `trigger_metadata`) so M2's `messages_before(anchor="current")` resolves to
    "older than this edge" deterministically. This is the concrete link between
    the hot-context window and the SIMPLE navigation verbs the user named.
- **Eviction / bounds discipline.** The new section is small and fixed-cap; it
  participates in the existing truncation accounting (`truncations` dict,
  `_render_with_counts`) so it can't starve other blocks. It surfaces *gist-ish*
  prior context (snippet + speaker + date + a cursor handle), not full
  transcripts — opening the full thread is M2's `open_thread`, a tool call
  (`xen-retrieval-brief.md:56-62`, "present before precise"; the section is a
  *handle to scroll*, not the scrollback itself).
- **Invites deeper retrieval (the "push to search" nudge).** The block is framed
  so the agent treats it as a *bounded gist, not the whole story*: a one-line
  lead-in states these are partial prior-on-topic hits, and each row renders an
  **actionable handle** (the cursor + a human label) plus an explicit cue that the
  full exchange is one tool call away (`open_thread`/`scroll`/`search`). Intent:
  when the surfaced gist is insufficient the agent *proactively* fetches more via
  M2 rather than answering from the slice. (The matching "use me when the gist
  isn't enough" framing on the tool **descriptions** is an M2 deliverable; M4
  grades whether the agent actually does it.)
- **Visibility.** Every row passes `raw_message_visibility` + `partner_share`
  exactly as `recent_messages` does (`hot_context.py:876-902`), and reads M1's
  searchable view (suppress/deleted excluded by construction).
- **All agents.** The section is built for every `bot_id` (the function is
  already bot-parameterized, `hot_context.py:546`). No per-bot special-casing.

OUT (anti-scope):
- No removal/shrinking of existing hot-context blocks in this milestone. (The v3
  doc's "shrink hot context" idea — `xen-retrieval-brief.md:154-159` — is a
  *separate* tuning slice gated on measuring first-reply quality, open-Q #3
  there. M3 *adds* the relevant-prior section; it does not yet cut the last-20.)
- No relationship-card / open-loops surfacing (that's a different v3 surface, not
  in this epic).
- No cross-topic peek changes (existing `cross_topic_peek` stays as-is).
- No aggregation/trend blocks.

## Locked decisions

- The new section merges (topic-recent-beyond-window) + (semantic-prior) and is
  capped at a single budget equal to the M0 inclusion budget.
- It surfaces handles (snippet + cursor), not full transcripts; full read is via
  M2 tools ("present before precise").
- The window-edge anchor is computed in `build_hot_context` and published to
  `TurnContext` for M2's `anchor="current"`.
- Built for all bots, no per-bot branching.
- The section is rendered to *invite follow-up retrieval* (gist + actionable
  handle + an explicit "open/search for the full exchange" cue), not as a
  self-sufficient answer — the "push to search for full context" behavior.

## Open questions

1. Exact budget split between the two sources (recommend 5 topic-recent + up to
   3 net-new semantic, total ≤ 8; let M0's precision/recall thresholds tune it).
2. Whether the semantic-prior query should also fire on *silent* / scheduled-task
   turns (no fresh triggering message). Recommend: only when there is a
   triggering message with content; otherwise topic-recent-only.
3. Latency — **RESOLVED (see Constraints, "Latency (live voice)")**: the
   semantic half adds one ANN query + (cache-miss) one query-embed round-trip to
   every hot-context build. Run it concurrently with the existing fetches, reuse
   M1's query-embed cache + budget, and degrade to topic-recent-only on
   timeout/error (mirror `_fetch_upcoming`, `hot_context.py:1113-1124`).

## Constraints

- Token budget: the section is fixed-cap and clipped (`_clip`, 240 chars/line
  like other blocks).
- Must not regress existing hot-context behavior or block the build on retrieval
  failure (graceful degrade, like the existing optional fetches).
- 6543 pooler: the semantic query uses M1's `SET LOCAL ef_search` in-txn pattern.
- **Latency (live voice) — hard requirement.** The semantic-prior query reuses
  M1's query-embed cache and runs under M1's query-embed latency budget; it
  executes concurrently with the other hot-context fetches and degrades to
  topic-recent-only on timeout/error (mirror the `_fetch_upcoming` try/except,
  `hot_context.py:1113-1124`). The hot-context build must NEVER block on the
  embedding vendor — this fires on every turn for every agent.

## Done criteria

- `HotContext` carries `relevant_prior` and `build_hot_context` populates it
  from both sources, deduped against the window, capped at budget, visibility-
  filtered.
- `_render_with_counts` renders a `## Previous on this topic` block with
  snippet + speaker label + date + cursor handle per row, participating in
  truncation accounting.
- The window-edge anchor is published to `TurnContext` and M2's
  `messages_before(anchor="current")` consumes it (cross-milestone test).
- Retrieval failure degrades to topic-recent-only without failing the build
  (test).
- The rendered block carries a lead-in framing it as a partial gist and renders
  each row with an actionable handle + a follow-up cue (so the agent is nudged to
  open/search for full context); asserted in the render test.
- The section passes M0's hot-context-inclusion thresholds (set-recall ≥ 0.8,
  set-precision ≥ 0.6 @ budget) on the fixture suite (via M0's DB-backed
  candidate path).
- A suppressed/deleted/partner-private prior message never appears in the
  section (test).

## Touchpoints

- Modify: `app/services/hot_context.py` — `HotContext` dataclass
  (`:37-61`), `build_hot_context` (`:535`, add the two queries + window-edge),
  `_render_with_counts` (`:1280+`, new block); reuse `raw_message_visibility`,
  `partner_share_by_user`, `_message_thread_owner_id`, `_time_context`, `_clip`.
- Reuse: M1 `hybrid_search`; M1 `v_searchable_messages`.
- Modify: `app/services/turn_context.py` (publish window-edge anchor) so M2 can
  read it.
- Tests: extend `tests/test_hot_context.py`; M0 hot-context-inclusion suite is
  the spec.

## Anti-scope (explicit)

No shrinking of existing blocks, no relationship-card, no aggregation, no
cross-topic changes, no full-transcript dumping (handles only).
