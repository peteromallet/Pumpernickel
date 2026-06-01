# Xen v2 — M2: Multi-source retriever (source-weighted RRF + provenance nav)

> Part of the Xen v2 epic (`xen-v2-epic-plan.md`). Depends on M1's
> `content_embeddings` + `v_searchable_content`. Makes the whole knowledge corpus
> retrievable through the v1 hybrid spine, adds **config-driven source weighting**
> to RRF, carries `source_type` out to callers, and wires the
> **provenance-navigation** affordance. Runs **premium / thorough / high** —
> visibility *composition* across arms + weighting correctness, and the
> `dyad_shareable` post-filter (if M1 chose resolution B) lives on this return
> path. Grounded in `xen-v2-strategy.md:44-46, 59-61`.

## Outcome

The v1 hybrid retriever (`app/services/retrieval.py`) now searches across all
M1 source types: it queries `content_embeddings ⋈ v_searchable_content`, every
`RetrievalResult` carries its `source_type`, RRF fusion applies a
**config-driven per-source weight** so callers can lean the ranking toward
insights or messages without a code change, and a provenance affordance lets an
agent jump from a surfaced insight to the **source messages it is based on** via
`supporting_message_ids`.

## Scope

IN:
- **Query across the unified view.** `hybrid_search` (and the keyword + semantic
  halves) read `content_embeddings` joined to `v_searchable_content` instead of
  the message-only view. Both halves now span all M1 arms. The OOB hard-exclusion
  filter (`retrieval.py:407`) stays in force across all arms.
- **`source_type` on results.** Add `source_type` (and keep `source_id`) to
  `RetrievalResult` (`retrieval.py`) so every hit declares its type all the way
  out to the tool surface and the hot-context caller. `match_type`
  (exact/semantic/both) is preserved per the v1 contract.
- **Source-weighted RRF (config, not hardcoded).** In `_fuse_rrf_results`
  (`retrieval.py:435`), multiply each RRF term by
  `source_weight_map[source_type]` (default `1.0` for any unmapped type). The map
  is **CONFIG** (`app/config.py`), not literals in the fusion code. Default lean
  per strategy: distillations slightly-above / messages ≈ baseline / memories ≈
  messages / observations slightly-below. **Final values are tuned by the m4
  golden set** — m2 ships the mechanism + the documented default, not the tuned
  numbers.
- **`dyad_shareable` post-filter/substitute on the return path (only if M1 chose
  resolution B).** If M1 kept `dyad_shareable` memories in the index, the
  retriever's return path MUST substitute `shareable_summary` for `content` (or
  drop the row) before any partner-visible result is returned. If M1 chose
  resolution A (exclude from index), this is a no-op — assert the exclusion holds.
  Either way, a test proves a partner can never receive a `dyad_shareable`
  memory's raw `content` through `hybrid_search`.
- **Search tool surface carries `source_type`.** The agent-facing `search`
  tool/result includes `source_type` per hit (so the agent and the render can
  label "insight" / "memory" / "message" / "artifact"), plus the `source_id` /
  cursor handle for navigation. Snippets follow the v1 contract.
- **Provenance navigation affordance.** A tool/affordance that, given a
  distillation or observation hit, returns the **source messages it is based on**
  via `supporting_message_ids uuid[]` (one query, visibility-filtered through the
  message arm). Wire it as a navigable affordance off a surfaced insight/
  observation. **memories have NO such link — document the gap** (no
  `supporting_message_ids` on memories; provenance nav is unavailable for them).

OUT (anti-scope):
- No schema/view/worker changes (M1). No hot-context wiring or render (M3).
- No weight *tuning* — m2 ships the config mechanism + a documented default lean
  only; the golden-set-tuned values are M4.
- No new source types (themes/notes are M4). No embedding-model change.
- No double-surfacing reconciliation in the hot-context path (that's M3) —
  though `source_type` on results is what makes M3's dedup possible.

## Locked decisions (from the epic plan; do not re-litigate)

- Retrieve across `content_embeddings ⋈ v_searchable_content`; OOB stays a hard
  exclusion filter.
- `RetrievalResult` carries `source_type`.
- **Source-weighted RRF is the mechanism; weights are CONFIG** with a default
  lean (distillations slightly-above, messages baseline, memories ≈ messages,
  observations slightly-below). **Tuning is deferred to the m4 golden set** — the
  weighting *philosophy* (insights-above-messages vs messages-primary) is the one
  genuine human call; the user chose **tunable-default over hard-boosting**.
- Provenance nav uses `distillations.supporting_message_ids` +
  `observations.supporting_message_ids`; memories have no source link (gap noted).
- The `dyad_shareable` substitution lives here only if M1 chose resolution B;
  otherwise M1's index exclusion is authoritative.

## Open questions

1. Whether `search` should let the caller pass a per-call `source_weight_map`
   override (vs config-only). Recommend config-default + optional override param
   — cheap, and lets M4's eval sweep weights without redeploys.
2. Provenance affordance shape: a distinct tool vs an enriched result field that
   M2's nav verbs already resolve. Recommend a thin tool reusing the message arm.

## Constraints

- Visibility is enforced by the M1 view's arms; m2 must NOT add a parallel
  filter that could diverge — compose, don't reimplement. The ONE return-path
  filter m2 owns is the `dyad_shareable` substitution (resolution B only).
- 6543 pooler: semantic query uses M1's `SET LOCAL ef_search` in-txn pattern; the
  v1 query-embed latency budget + cache + keyword-only degrade still apply
  (knowledge search is on the same hot path).
- `source_weight_map` defaults must be inert-safe: an unmapped `source_type`
  defaults to `1.0` (no silent suppression of a new type).

## Done criteria

- `hybrid_search` returns RRF-fused hits across all M1 source types, each hit
  carrying `source_type`; a test asserts hits from each type appear and are
  correctly labeled.
- A test proves source weighting changes ranking: with a non-default
  `source_weight_map`, the relative order of two equal-RRF hits of different types
  flips as expected; with the default map (all `1.0`-relative) v1 message ranking
  is unchanged (no regression).
- **A partner can never receive a `dyad_shareable` memory's raw `content` via
  any `hybrid_search` mode** (test) — index-excluded (A) or substituted (B).
- The OOB exclusion holds across all arms (test: an OOB-linked row never appears).
- The provenance affordance returns the source messages for a distillation /
  observation via `supporting_message_ids`, visibility-filtered; a test asserts
  it, and asserts memories have no such link (documented gap, not a crash).
- v1 message-only search behavior is preserved when only the `message` arm is in
  scope (no regression vs the v1 golden thresholds).

## Touchpoints

- Modify: `app/services/retrieval.py` — read `v_searchable_content`; add
  `source_type` to `RetrievalResult`; `_fuse_rrf_results` (`:435`) applies
  `source_weight_map`; preserve `match_type` + the v1 latency budget/cache/degrade.
- Modify: `app/config.py` — `source_weight_map` (config, documented default lean).
- New/modify: `app/services/tools/read_tools.py` — the `search` tool result
  carries `source_type` + handle; the provenance "source messages this insight is
  based on" tool/affordance.
- Reference: `app/services/retrieval.py:407` (OOB exclusion);
  `distillations.supporting_message_ids` / `observations.supporting_message_ids`;
  M1's `v_searchable_content` arms (the authoritative visibility predicates).
- Reuse: v1 query-embed cache + latency budget + keyword-only degrade.

## Anti-scope (explicit)

No view/worker/schema changes, no hot-context wiring or render, no weight tuning,
no new source types, no parallel visibility filter (compose the M1 arms), no
embedding-model change.
