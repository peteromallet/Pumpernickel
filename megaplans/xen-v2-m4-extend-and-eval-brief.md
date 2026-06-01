# Xen v2 — M4: Extend + eval (deferred types + golden-set + weight tuning)

> Part of the Xen v2 epic (`xen-v2-epic-plan.md`). Optional. Depends on M1–M3.
> Adds the **deferred source types** (conversation_artifacts beyond first-cut,
> conversation_notes, themes) over M1's now-proven per-type pattern, extends the
> eval golden set with knowledge-type queries, runs the **real-data #2 gate**, and
> **tunes `source_weight_map`** on the golden set — resolving the *numbers* of the
> one genuine human call. Runs **partnered / full / medium**. Grounded in
> `xen-v2-strategy.md:71-72, 22-23, 80-83`.

## Outcome

The retrieval corpus is extended to the remaining valuable knowledge types, and
the source-weighting numbers are no longer a guess: an extended golden set of
knowledge-type queries (run against real data, gate #2) measures recall/precision
per type and per query intent, and `source_weight_map` is tuned from those
measurements — settling the insights-above-messages vs messages-primary tension
with evidence rather than assertion.

## Scope

IN:
- **Add the deferred source arms** (over M1's exact pattern: a
  `v_searchable_content` arm + a canonical-text builder + enqueue-on-write +
  backfill, each carrying its own visibility predicate):
  - **conversation_notes** → `text`; visibility = parent conversation;
    provenance via `evidence_turn_id`.
  - **themes** → `title` + `description` (thin — enums dominate; low weight);
    topic scope; reverse provenance via `related_theme_ids`.
  - Any **conversation_artifact** types not covered in m1's first-cut payload
    extractor.
- **Extend the eval golden set with knowledge-type queries.** Add cases that
  exercise the new types and the two query intents the strategy calls out: "what
  do I know about X" (favors insights) vs "what exactly did she say" (favors
  messages). Reuse the existing `eval/retrieval/` harness (corpus, golden_set,
  runner, metrics, adapters) — extend it, don't rebuild.
- **Real-data #2 gate.** Run the extended golden set against a real-data adapter
  (the strategy's gate #2) to measure per-type and per-intent recall/precision —
  the evidence base for weight tuning.
- **Tune `source_weight_map`.** Sweep weights against the golden set; pick the
  values that best serve both query intents (likely intent-dependent — which is
  itself the argument for keeping weights tunable). Record the chosen defaults +
  the measurements that justify them. This resolves the *numbers* of the one
  genuine human call (the *mechanism* was locked in m2; the *philosophy*
  — tunable-default over hard-boosting — was the user's call).

OUT (anti-scope):
- No re-architecture — the arm/builder/enqueue/backfill pattern is M1's; m4
  applies it to the deferred types only.
- No retriever fusion changes (m2's `source_weight_map` mechanism is fixed; m4
  only sets its *values*).
- No hot-context render changes beyond letting the now-present artifact/note types
  flow through M3's `relevant_prior` block (which already excludes the
  dedicated-section types).
- Still NEVER embed: commitments, watch_items, OOB, the join tables.

## Locked decisions (from the epic plan; do not re-litigate)

- Deferred types = conversation_notes, themes, remaining artifact types — added
  over M1's pattern, each with its own visibility arm.
- The `source_weight_map` *mechanism* is M2's and fixed; m4 sets the *values* from
  the golden set.
- The weighting *philosophy* is the one genuine human call and was resolved
  **tunable-default over hard-boosting**; m4 supplies the evidence-backed numbers.
- Eval extends the existing `eval/retrieval/` harness; does not rebuild it.

## Open questions

1. Whether themes are worth the index slot at all (thin prose, enum-dominated).
   Recommend: add the arm but default its weight low; let the golden set decide if
   it earns retrieval surface.
2. Real-data #2 gate adapter shape — reuse the v1 DB-backed adapter pattern.

## Constraints

- Each new arm replicates its type's existing visibility logic exactly (same
  correctness-spine discipline as M1) — notes scoped to parent conversation,
  themes to topic scope.
- Eval must run offline / against a gated real-data adapter; no prod writes.
- Weight tuning changes config only (`source_weight_map`), not fusion code.

## Done criteria

- conversation_notes + themes (+ any remaining artifact types) have a
  `v_searchable_content` arm, canonical-text builder, enqueue-on-write, and
  backfill; each arm hides what its type's scope would hide (test per type).
- The eval golden set includes knowledge-type cases across both query intents;
  the harness runs them and reports per-type + per-intent recall/precision.
- The real-data #2 gate runs and produces the measurement set.
- `source_weight_map` defaults are set from the golden-set sweep, with the
  justifying measurements recorded; the chosen values serve both query intents
  acceptably (no intent regresses below threshold).

## Touchpoints

- Modify: the `v_searchable_content` view (add the `conversation_note` / `theme` /
  remaining-artifact arms); the embed worker (new canonical-text builders); the
  write sites for notes/themes (enqueue); `scripts/backfill_embeddings.py` (new
  per-type backfill).
- Modify: `app/config.py` — set the tuned `source_weight_map` values.
- Modify: `eval/retrieval/` — golden_set (knowledge-type + intent cases), adapter
  (real-data #2 gate), metrics/runner as needed.
- Reference: `conversation_notes.evidence_turn_id`; `themes.related_theme_ids`;
  M1's per-type pattern; M2's `source_weight_map` mechanism.

## Anti-scope (explicit)

No re-architecture, no fusion-mechanism change, no hot-context render change
beyond letting the new types flow through M3's block, no embedding of
commitments/watch_items/OOB/join-tables, no prod writes from the eval.
