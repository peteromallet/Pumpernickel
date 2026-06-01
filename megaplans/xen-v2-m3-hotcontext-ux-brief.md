# Xen v2 — M3: Hot-context UX (dedup reconciliation + readable redesign)

> Part of the Xen v2 epic (`xen-v2-epic-plan.md`). Depends on M2's
> `source_type`-carrying retriever. **The reframe payoff** — memories/
> observations/distillations are ALREADY in dedicated hot-context sections, so
> this milestone (a) reconciles the new retrieval `relevant_prior` path so it
> doesn't DOUBLE-surface them, and (b) redesigns the render so surfaced knowledge
> is legible: type-labeled, name-resolved, recency-ordered, snippet + nav handle,
> insights-first, with a `+N more — search()` cue. Runs **partnered / full /
> medium**. Grounded in `xen-v2-strategy.md:53-58`.

## Outcome

`build_hot_context` (`hot_context.py`) no longer surfaces a knowledge item twice:
the new retrieval `relevant_prior` merge EXCLUDES the source types that already
have dedicated sections (memories/observations/distillations), and the render of
both the `relevant_prior` block and the dedicated sections is redesigned to be
readable at a glance — each line clearly labeled by type, names resolved (never a
raw UUID), most-recent-first, a snippet + a navigable handle, insights surfaced
first, and a `+N more — search()` cue when there's more behind the bound.

## Scope

IN:
- **Double-surfacing reconciliation.** The new retrieval-backed `relevant_prior`
  merge (`hot_context.py:1238-1263`) must EXCLUDE
  `source_type IN (memory, observation, distillation)` — those are already shown
  by the dedicated `## Memories` / `## High-sig observations` / `## Distillations`
  sections (`:1740`, `:1752`, `:1759`). Either filter those source types out of
  the merge, or dedup by id. `relevant_prior` keeps surfacing messages (and, when
  M4 adds them, artifacts/notes) — the types that have NO dedicated section.
- **Render redesign — `relevant_prior` block** (`hot_context.py:1802-1837`). Each
  line:
  - **Type-labeled** — clearly marked by type (memory / insight / message /
    artifact), so the agent knows what kind of knowledge it is reading.
  - **Name-resolved** — `sender_id` → `users.name` (NEVER a raw UUID).
  - **Recency-ordered** — most-recent-first.
  - **Snippet + nav handle** — a clipped snippet plus a navigable `[id/cursor]`
    handle (reuse M2's `source_id`/cursor) so the agent can open/search the full
    item.
  - **Insights-first** — insights/distillations ordered ahead of rawer types
    within the block.
  - **`+N more — search()` cue** — when the bound hides additional hits, a single
    line cues the agent that more is one `search()` away.
- **Dedicated-section legibility pass.** Apply the same readability improvements
  (type label clarity, name resolution, recency order, snippet + handle) to the
  existing `## Memories` / `## High-sig observations` / `## Distillations` render
  so they're consistent with the new block and equally navigable. (Presentation
  only — do NOT change what those sections *select*; their visibility logic is the
  M1 spine and stays put.)
- **Truncation discipline.** The redesigned blocks participate in the existing
  truncation accounting (the `truncations` dict / `_render_with_counts`), stay
  fixed-cap and clipped (`_clip`), and can't starve other blocks.

OUT (anti-scope):
- No retriever/index changes (M1/M2). No source-weight changes.
- No change to WHAT the dedicated sections select (their M1 visibility predicates
  are authoritative) — m3 only changes how they RENDER.
- No new source types in `relevant_prior` (artifacts/notes arrive in M4).
- No removal/shrinking of existing blocks beyond the dedup; no relationship-card,
  no aggregation/trend blocks, no cross-topic-peek changes.

## Locked decisions (from the epic plan; do not re-litigate)

- `relevant_prior` EXCLUDES `source_type IN (memory, observation, distillation)`
  (or dedups by id) — they're already in dedicated sections; double-surfacing
  otherwise (`hot_context.py:1238-1263`).
- The render is **type-labeled, name-resolved (sender_id→users.name, never raw
  UUID), recency-ordered (most-recent-first), snippet + nav handle [id/cursor],
  insights-first, with a `+N more — search()` cue** — applied to the
  `relevant_prior` block (`:1802-1837`) and the dedicated sections' legibility.
- Each line is **clearly labeled by type** (memory / insight / message / artifact).
- Presentation redesign only for the dedicated sections; their selection /
  visibility logic (M1) is unchanged.

## Open questions

1. Exact insights-first ordering rule when mixing types within `relevant_prior`
   (recommend: type-priority key [insight > artifact > message] then recency,
   under the single budget).
2. `+N more — search()` wording — keep it a single terse cue line; final phrasing
   is a presentation detail, not a re-litigation.

## Constraints

- Token budget: blocks stay fixed-cap and clipped (`_clip`, ~240 chars/line like
  other blocks); the redesign must not grow the budget.
- Must not regress existing hot-context behavior or block the build on retrieval
  failure (graceful degrade, like the existing optional fetches) — this fires on
  every turn for every agent, including live voice.
- Name resolution must be cheap (batch-resolve ids → names; no per-line query).
- The dedup must be exact: a memory/observation/distillation must appear in its
  dedicated section and NOT also in `relevant_prior`.

## Done criteria

- A knowledge item of type memory/observation/distillation appears in its
  dedicated section and NEVER also in `relevant_prior` (test).
- `relevant_prior` renders each line type-labeled, name-resolved (no raw UUID),
  most-recent-first, snippet + handle, insights-first, with a `+N more — search()`
  cue when the bound hides hits (render test asserts each property).
- The dedicated `## Memories` / `## High-sig observations` / `## Distillations`
  blocks render with the same legibility (type label, resolved names, recency,
  handle) and their *selection* is unchanged vs before (test: same rows selected).
- Retrieval failure degrades gracefully without failing the build (test).
- Blocks participate in truncation accounting and stay within the fixed cap
  (test).

## Touchpoints

- Modify: `app/services/hot_context.py` — the `relevant_prior` merge
  (`:1238-1263`, exclude the dedicated source types / dedup); the `relevant_prior`
  render block (`:1802-1837`, the redesign); the dedicated-section renders
  (`## Memories` `:1740`, `## High-sig observations` `:1752`, `## Distillations`
  `:1759`, legibility pass); `_render_with_counts` / `truncations` accounting;
  reuse `_clip`.
- Reuse: M2's `RetrievalResult.source_type` + `source_id`/cursor handle; a
  batch `sender_id`→`users.name` resolver.
- Tests: extend `tests/test_hot_context.py`.

## Anti-scope (explicit)

No retriever/index/weight changes, no change to dedicated-section *selection*, no
new source types in the block, no block removal beyond the dedup, no
relationship-card / aggregation / cross-topic changes, no token-budget growth.
