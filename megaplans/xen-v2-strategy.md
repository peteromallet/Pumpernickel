# Xen v2 — multi-source retrieval + hot-context UX (grounded strategy)

> Grounded in 6 DeepSeek code/schema profiles (2026-06-01), not assumptions. Each
> claim traces to file:line in those profiles. Companion to xen-v1 docs.

## The reframe that changes everything
Memories, observations, and distillations are **already in hot context** today via
dedicated pre-Xen sections (`hot_context.py`: `## Memories` :1740, `## High-sig
observations` :1752, `## Distillations` :1759). So:
- **SEARCH is the real net-new** — none of these are semantically searchable today.
- **HOT-CONTEXT v2 work = (a) the readable/type-labeled UX redesign, (b) reconcile
  the new retrieval "previous on this topic" section so it doesn't DOUBLE-surface
  what the dedicated sections already show.**

## Per-type verdict (what to index, weight, gate)
| Type | Embed? | Canonical text | Weight* | Visibility gate | Provenance → source msgs |
|---|---|---|---|---|---|
| **distillations** (insights) | ✅ | `content` | **highest** (0.75+) | `visibility`/`source_user_ids`/`partner_share` (hot_context :986-1054) | **`supporting_message_ids uuid[]`** ✅ |
| **memories** | ✅ | `content` (NOT shareable_summary) | high | `status='active'` + `visibility` (dyad_shareable risk) | ❌ none (no link) |
| **observations** | ✅ | `content` | med | `status='active' AND significance>=3` + topic scope + `about_user_id` | `supporting_message_ids` ✅ |
| **conversation_artifacts** (agenda/prep/debrief) | ✅ | `payload` jsonb (type-aware text extract) | high (prep briefs ~1.0) | parent `conversation` (user/partner) + `deleted_at` | `artifact_links` ✅ |
| **conversation_notes** | ✅ | `text` | med (0.6-0.7) | parent conversation | `evidence_turn_id` |
| **themes** | ⏸ defer | title+desc thin; enums dominate | low | topic scope | reverse via related_theme_ids |
| **commitments / watch_items** | ❌ | short labels/enums, no prose | — | already structured hot-context | — |
| **out_of_bounds** | ❌ **never** | `sensitive_core` is ENCRYPTED/partner-hidden | — | already a **hard exclusion filter** (retrieval.py:407) | — |
| **artifact_topics / artifact_links** | ❌ join tables | use for topic-filtering, don't embed | — | — | — |

\* **Weight is the one genuine design tension.** The subagents leaned messages=1.0 /
knowledge below (summaries are "lossy, one layer removed"). The user's instinct is
the opposite: distilled insights ABOVE raw messages. Resolution: **per-source weights
are config, validated by the eval golden set** — default to boosting distillations
(deliberate syntheses w/ provenance) at/above messages, memories≈messages,
observations slightly below; tune on real queries (likely query-type-dependent:
"what do I know about X" favors insights; "what exactly did she say" favors messages).

## Architecture (extend v1, don't rebuild)
1. **Schema**: generalize `message_embeddings` → `content_embeddings(source_type,
   source_id, …)` PK, default `'message'`; backfill keeps the 1826 live vectors.
   Add `v_searchable_content` = UNION ALL, **one arm per type, each carrying its own
   visibility predicate**.
2. **embed_jobs**: add `source_type`/`source_id` (default message); dedupe index updated.
3. **Worker**: one loop, dispatch on `source_type` → per-type canonical-text builder
   (`canonical_memory_text`, type-aware artifact `payload` extractor, …). Small change.
4. **Retriever**: query `content_embeddings ⋈ v_searchable_content`; add `source_type`
   to `RetrievalResult`; in `_fuse_rrf_results` (retrieval.py:435) multiply each RRF
   term by `source_weight_map[source_type]` (default 1.0). Source weights = config.
5. **Per-type visibility = THE correctness spine.** Each UNION arm must replicate the
   EXACT visibility logic of its dedicated hot-context section, or retrieval leaks what
   the section hides. memories=:836-862, observations=:913-942, distillations=:986-1054.
   The `dyad_shareable` memory case (embed `content` but partner must see
   `shareable_summary`) is the sharpest leak risk → exclude dyad_shareable from the
   index OR post-filter+substitute before returning.
6. **Hot-context dedup**: exclude `source_type IN (memory,observation,distillation)`
   from the `relevant_prior` retrieval merge (hot_context.py:1238-1263) — they're
   already in dedicated sections; double-surfacing otherwise.
7. **Hot-context render (UX redesign)**: type-labeled, name-resolved, recency-ordered,
   nav-handle lines (`💡 insight · …`, `🧠 memory · …`, `💬 Sarah, 3d ago · "…"`),
   insights-first, "+N more — search()" cue. Applies to the dedicated sections too.
8. **Provenance navigation** (high-value UX): `distillations.supporting_message_ids` +
   `observations.supporting_message_ids` → one-query "show me the messages this insight
   is based on". Wire as a tool/affordance. (memories have no such link — note the gap.)

## Proposed megaplan (small epic, codex, reuses v1 infra)
- **m1** unified `content_embeddings` + `v_searchable_content` (per-type visibility) +
  embed_jobs/worker generalization + canonical-text builders + per-type backfill.
  *premium/thorough* (visibility correctness + live-schema change).
- **m2** multi-source retriever + source-weighted RRF + `search`/nav tool surface
  carries source_type + provenance (`supporting_message_ids`) nav. *premium*.
- **m3** hot-context: dedup reconciliation + the type-labeled readable UX redesign
  (incl. v1 UX gaps: names, recency, more-cue). *partnered*.
- **m4** *(optional)* extend to conversation_artifacts/notes + eval golden-set
  extension (knowledge-type queries) + the real-data #2 gate. *partnered*.

## First-cut scope recommendation
messages (have) + **distillations + memories + observations** (highest-value prose,
all already in hot context so visibility logic exists to copy) + **conversation_artifacts**
(core to the agenda feature). Defer themes/notes; skip commitments/watch_items/OOB
(structured, already surfaced; OOB stays an exclusion filter).

## Open decision for the user
Weighting philosophy: **boost insights above messages** (user instinct) vs **messages
primary, insights below** (subagent caution). Recommendation: ship tunable weights,
default insights≈/slightly-above messages, and let the #2 real-data golden set decide.
