# Xen v2 — Multi-source retrieval + hot-context UX (Plan)

> Extends the shipped Xen v1 message-search spine to **all high-value knowledge
> types** (distillations/insights, memories, observations, conversation
> artifacts) and redesigns the hot-context render so the surfaced knowledge is
> legible, type-labeled, name-resolved, and navigable. Grounded in
> `xen-v2-strategy.md` (6 DeepSeek code/schema profiles, 2026-06-01) — every
> decision below traces to it; this plan resolves them so milestones don't
> re-litigate.

This is the top-level tie-together. Each milestone has a self-contained brief
under `megaplans/`. Build them in order; later milestones depend on earlier ones.

---

## The reframe that changes everything

Memories, observations, and distillations are **already in the hot context
today** via dedicated pre-Xen sections (`hot_context.py`: `## Memories` :1740,
`## High-sig observations` :1752, `## Distillations` :1759). So Xen v2 is not
"put knowledge into hot context" — that's already done. The net-new value is:

- **SEARCH is the real net-new.** None of these knowledge types are semantically
  searchable today; only `messages` got the v1 retriever. v2 makes the whole
  knowledge corpus searchable through the *same* hybrid (pgvector + tsvector +
  RRF) spine, by generalizing the index — not rebuilding it.
- **HOT-CONTEXT v2 work = (a) the readable, type-labeled UX redesign of how this
  knowledge is rendered, and (b) reconciling the new retrieval "previous on this
  topic" / `relevant_prior` path so it does NOT double-surface what the dedicated
  sections already show** (`hot_context.py:1238-1263`).

This reframe is why v2 is a *small* epic that reuses v1 infra rather than a
greenfield build.

---

## Prerequisite decisions resolved here (so milestones don't re-litigate)

These are the strategy's verdicts, locked. Genuinely-human-call items are flagged
at the end.

1. **Extend v1, don't rebuild — LOCKED.** Generalize `message_embeddings` →
   `content_embeddings(source_type TEXT NOT NULL DEFAULT 'message', source_id
   UUID NOT NULL, …)`, PK `(source_type, source_id)`. **Preserve the 1826 live
   message vectors** via in-place backfill (`source_type='message',
   source_id=message_id`) — never a re-embed. Add a `v_searchable_content`
   UNION-ALL view, **one arm per source type**, each arm carrying ITS OWN
   visibility predicate. (`xen-v2-strategy.md:36-41`.)

2. **Job/worker generalization — LOCKED.** `embed_jobs` gains
   `source_type`/`source_id` (default `'message'`); dedupe index updated. One
   worker loop dispatches on `source_type` → a per-type canonical-text builder
   (`canonical_memory_text`, type-aware `payload` extractor, …). Small change.
   (`xen-v2-strategy.md:41-43`.)

3. **First-cut source types — LOCKED.** m1–m3 index: **distillations, memories,
   observations, conversation_artifacts — PLUS existing messages.** Canonical
   text per the strategy table (`xen-v2-strategy.md:16-26`):
   - `distillations.content`
   - `memories.content` (**NOT** `shareable_summary`)
   - `observations.content`
   - `conversation_artifacts.payload` via **type-aware jsonb text extraction**

   Defer **themes** + **conversation_notes** to m4. **NEVER embed:**
   `commitments`, `watch_items` (short labels/enums, no prose, already structured
   in hot context), `out_of_bounds` (`sensitive_core` is ENCRYPTED /
   partner-hidden — OOB stays a **hard exclusion filter**, `retrieval.py:407`),
   and the join tables `artifact_topics` / `artifact_links` (used for
   topic-filtering, never embedded).

4. **Per-type visibility is THE correctness spine — LOCKED.** Each
   `v_searchable_content` arm must replicate the EXACT visibility logic of that
   type's existing dedicated hot-context section, so retrieval can NEVER surface
   what the section hides: memories `hot_context.py:836-862`, observations
   `:913-942`, distillations `:986-1054`. The **`dyad_shareable` memory leak** is
   the sharpest risk (embed `content`, but a partner must only ever see
   `shareable_summary`) → either exclude `dyad_shareable` rows from the index OR
   post-filter+substitute before returning. m1 runs **premium/thorough** because
   of this.

5. **Source-weighted RRF — LOCKED (mechanism), TUNABLE (values).** In
   `_fuse_rrf_results` (`retrieval.py:435`), multiply each RRF term by
   `source_weight_map[source_type]` (default `1.0`). Weights are **CONFIG, not
   hardcoded.** Default lean: distillations slightly-above / messages ≈ baseline /
   memories ≈ messages / observations slightly-below — **final tuning left to the
   m4 eval golden set.** (`xen-v2-strategy.md:28-34, 44-46`.)

6. **Hot-context double-surfacing reconciliation — LOCKED.**
   memories/observations/distillations are ALREADY in dedicated hot-context
   sections; the new `relevant_prior` retrieval merge must EXCLUDE those
   `source_type`s (or dedup by id) so they don't appear twice
   (`hot_context.py:1238-1263, 53-55`).

7. **Hot-context UX redesign — LOCKED (m3).** Type-labeled, name-resolved
   (`sender_id` → `users.name`, never a raw UUID), recency-ordered
   (most-recent-first), each line a snippet + nav handle `[id/cursor]`,
   insights-first, with a `+N more — search()` cue. Applies to the
   `relevant_prior` render block (`hot_context.py:1802-1837`) and improves the
   dedicated sections' legibility. (`xen-v2-strategy.md:56-58`.)

8. **Provenance navigation — LOCKED (m2).** `distillations.supporting_message_ids`
   + `observations.supporting_message_ids` (`uuid[]`) → a "show me the source
   messages this insight is based on" tool/affordance. **memories have NO source
   link** — note the gap. (`xen-v2-strategy.md:59-61`.)

---

## Milestone order & dependencies

```
M1  Content embeddings — unified index + per-type visibility + backfill
        │  (generalizes v1 message_embeddings; preserves 1826 live vectors)
        ▼
M2  Multi-source retriever — source-weighted RRF + provenance nav tool
        │  (depends on M1's content_embeddings + v_searchable_content)
        ▼
M3  Hot-context UX — dedup reconciliation + type-labeled readable redesign
        │  (depends on M2's source_type-carrying results)
        ▼
M4  Extend + eval — artifacts/notes/themes + golden-set + weight tuning   (optional)
        │  (depends on M1-M3; tunes the source_weight_map)
```

| # | Milestone | Brief file | One-line |
|---|-----------|-----------|----------|
| M1 | Content embeddings | `xen-v2-m1-content-embeddings-brief.md` | Generalize `message_embeddings`→`content_embeddings(source_type,source_id)`; preserve the 1826 live message vectors by backfill; add `v_searchable_content` UNION-ALL with one per-type visibility arm; generalize `embed_jobs`/worker + per-type canonical-text builders for distillations/memories/observations/artifacts. |
| M2 | Multi-source retriever | `xen-v2-m2-multisource-retriever-brief.md` | Query `content_embeddings ⋈ v_searchable_content`; add `source_type` to `RetrievalResult`; source-weighted RRF (config); search/nav tool surface carries `source_type` + a provenance "source messages this insight is based on" affordance via `supporting_message_ids`. |
| M3 | Hot-context UX | `xen-v2-m3-hotcontext-ux-brief.md` | Dedup the `relevant_prior` merge against the dedicated memory/observation/distillation sections; redesign the render to be type-labeled, name-resolved, recency-ordered, snippet+handle, insights-first, with a `+N more — search()` cue. |
| M4 | Extend + eval | `xen-v2-m4-extend-and-eval-brief.md` | Add conversation_artifacts/notes + themes source arms (the deferred types), extend the eval golden set with knowledge-type queries, the real-data #2 gate, and tune `source_weight_map` on the golden set. |

---

## GOAL → milestone mapping (so nothing is dropped)

| Goal clause (from strategy) | Milestone(s) |
|---|---|
| "SEARCH is the real net-new" — knowledge types semantically searchable | M1 (index) + M2 (retriever/tool) |
| Generalize `message_embeddings`, preserve 1826 vectors, `v_searchable_content` | M1 |
| `embed_jobs`/worker generalization + per-type canonical-text builders | M1 |
| Per-type visibility = correctness spine (the `dyad_shareable` leak) | M1 (arms) + M2 (post-filter/substitute on return) |
| Source-weighted RRF (config) | M2 (mechanism) + M4 (tune values on golden set) |
| `source_type` on `RetrievalResult` + search tool | M2 |
| Provenance nav (`supporting_message_ids` → source messages) | M2 |
| Hot-context double-surfacing reconciliation | M3 |
| Type-labeled / name-resolved / recency / handle / insights-first / `+N more` | M3 |
| Extend to artifacts/notes/themes (deferred types) | M4 |
| Eval golden-set extension + real-data #2 gate + weight tuning | M4 |

---

## Recommended megaplan dials (per milestone)

Dials per `megaplan-prep`: **profile** (intelligence tier), **robustness**
(review/critique depth), **depth** (thinking).

| # | Profile | Robustness | Depth | Why |
|---|---------|-----------|-------|-----|
| M1 | premium | thorough | high | Highest risk: live schema change (generalize the table holding 1826 prod vectors), per-type visibility arms (the correctness spine), the `dyad_shareable` leak, and a live backfill that must not lose existing vectors. Wants the strongest profile + adversarial gate. |
| M2 | premium | thorough | high | Visibility *composition* across arms + source-weight correctness; the search tool can leak if an arm's predicate is wrong; the `dyad_shareable` post-filter/substitute lives on this return path. Correctness-sensitive. |
| M3 | partnered | full | medium | Edits the heart (`hot_context.py`); dedup reconciliation + a presentation redesign over M2's results. Token-budget sensitive but bounded; no new invariant. |
| M4 | partnered | full | medium | Adds the deferred-type arms over M1's now-proven pattern + offline eval/tuning. Bounded; reuses the established mechanism. |

---

## The one genuine human call: weighting philosophy (FLAGGED)

Everything else above is resolved. The single decision that is a genuine human
judgment — not a defensible default the planner can pick — is the **weighting
philosophy**:

- **boost insights ABOVE messages** (the user's instinct: distilled syntheses,
  with provenance, are the most valuable thing to surface) vs
- **messages primary, insights below** (the subagent caution: summaries are
  "lossy, one layer removed" from ground truth).

**Resolution chosen by the user:** ship **tunable per-source weights**
(`source_weight_map`, config), default insights ≈ / slightly-above messages,
memories ≈ messages, observations slightly-below — and **let the m4 real-data
golden set decide the final values.** The user explicitly chose
**tunable-default over hard-boosting** insights. The mechanism is LOCKED in m2
(decision 5); only the *numbers* are open, and they are resolved by the m4 eval,
not by re-litigation in any milestone. Likely query-type-dependent: "what do I
know about X" favors insights; "what exactly did she say" favors messages — which
is itself an argument for tunable weights over a hardcoded boost.
