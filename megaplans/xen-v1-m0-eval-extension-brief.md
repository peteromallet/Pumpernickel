# Xen v1 — M0: Eval extension (fairness + simple-nav + hot-context-inclusion)

> Part of the Xen v1 epic (`xen-v1-epic-plan.md`). **First** milestone, no prod
> deps. Extends the already-shipped harness in `eval/retrieval/` (corpus.yaml,
> golden_set.yaml, runner.py, metrics.py, adapters.py, README.md — 28 cases).
> This is the **go/no-go gate** that must pass before M1 spends any embedding
> backfill, and it makes the user's SIMPLE-navigation queries testable with
> deterministic, assert-exact answers.

## Outcome

The eval harness can answer three new questions it cannot answer today:
1. **Fairness** — does semantic actually beat keyword on *real-life query
   nuance*, not just a synthetic strawman? (The in-progress fairness re-eval.)
2. **Simple navigation correctness** — for `messages_before(anchor, n)`,
   before-a-given-ID, recent-before-current, and topic-recent, the correct
   answer is **deterministic**; assert it exactly (not recall@k).
3. **Hot-context inclusion** — given a conversational state, are the *right*
   previous-on-topic messages surfaced into hot context? Define the metric and
   fixtures.

## Scope

IN:
- **Fairness re-eval (a).** Harden `golden_set.yaml` against the strawman risk
  flagged in `xen-eval-harness-brief.md:54-62`: add cases where keyword *also*
  plausibly succeeds (so semantic doesn't win for free), and cases mined from
  real query *shapes* (terse, context-dependent, dyadic) without using real
  user content. Add a `difficulty`/`fairness` tag per case. Produce a
  side-by-side baseline-vs-semantic report that is honest about where keyword
  wins. The semantic adapter under test is M1's real retriever via the
  **optional DB-backed adapter** (see below) — until M1 exists, this runs
  against `StubSemanticRetriever` and the report just shows the keyword floor.
- **DB-backed adapter (interface + optional).** A new adapter satisfying the
  existing `Retriever` Protocol (`eval/retrieval/adapters.py`) that, when
  `DIRECT_DATABASE_URL` is set, runs against a real Postgres-with-pgvector
  (local/test, never prod) applying the **production scope model** the synthetic
  harness deliberately omits (bot_id, participant, partner_share, date — see
  `eval/retrieval/README.md:58-109`). Off by default; the offline baseline path
  must stay zero-dep. This is the bridge that lets M1's retriever be scored on
  the same golden set + metrics.
- **Simple-nav eval (b).** A NEW eval module + golden format for navigation,
  because nav answers are exact sequences, not ranked sets:
  - Cases: `{id, op, anchor, n, scope, expected_ids_in_order, notes}` where `op`
    ∈ {`messages_before`, `messages_after`, `open_thread`, `scroll`,
    `topic_recent`, `before_message_id`, `recent_before_current`}.
  - Metric: **exact ordered match** (and a "contiguous + correct boundary"
    check) — pass/fail, not recall@k. A nav op that returns the wrong boundary
    message or wrong order is a hard fail.
  - Fixtures over the synthetic corpus (extend `corpus.yaml` with `sent_at`
    ordering + a synthetic "current hot-context window edge" anchor so
    `recent_before_current` has a defined correct answer).
  - The adapter under test is M2's nav SQL (via the DB-backed adapter) OR a
    pure-python reference implementation of the same contract for offline runs.
- **Hot-context-inclusion eval (c).** The hardest, most novel piece:
  - **Metric proposal:** given a fixture conversational state = `{topic_id,
    last-N-window message_ids, a "gold set" of prior-on-topic message_ids a good
    system should surface}`, score the candidate hot-context builder's selected
    "previous on this topic" set with **set-precision / set-recall @ budget**
    (budget = the M3 cap, e.g. 5). Report both: recall guards against missing
    the relevant prior message; precision guards against flooding context with
    noise (the "context bomb" risk, `per-bot-sharing-brief.md:62`).
  - **Fixtures:** ≥8 conversational-state fixtures spanning: (i) topic continues
    after a gap (relevant prior message is *outside* the last-20 window — the
    core case), (ii) topic switch (prior on the *new* topic should surface, not
    the old), (iii) nothing relevant prior (good system surfaces *nothing* —
    precision test), (iv) near-duplicate prior incidents (surface the
    authoritative one). Each fixture names its gold set with a rationale.
  - The candidate under test is M3's selection query (via DB-backed adapter) or
    a reference impl offline.
- **Go/no-go thresholds (locked numbers the epic gates on):**
  - Semantic+RRF must beat keyword on the **paraphrase** query-type by
    **recall@10 ≥ 0.7** (keyword floor there is ~0) AND not regress verbatim
    (`recall@1 ≥ keyword recall@1` on `verbatim_quote`). These are the M1 gate.
  - Simple-nav: **100% exact-match** on all nav cases (deterministic — anything
    less is a bug, not a tuning miss).
  - Hot-context-inclusion: **set-recall ≥ 0.8 AND set-precision ≥ 0.6** at
    budget on the fixture suite (the M3 gate).
- Unit tests for every new metric + loader; README section per new eval type.

OUT (anti-scope):
- No embeddings/pgvector/retriever **implementation** (that's M1). M0 only
  builds the measuring instrument + thresholds + the optional DB seam.
- No labeling of real production corpus (privacy — synthetic + real query
  *shapes* only, never real content; `xen-eval-harness-brief.md:41`).
- No changes to `app/*` production code paths. The DB-backed adapter lives under
  `eval/retrieval/` and reads only.
- No Surface 2/3 (bulk/aggregation) eval.

## Locked decisions

- Reuse the existing `Retriever` Protocol + runner + metrics for ranked search;
  add a **separate** nav-eval module with exact-match semantics (different
  correctness model — don't force nav through recall@k).
- Hot-context-inclusion metric = **set-precision + set-recall @ budget**, not
  ranked metrics (inclusion is a set decision, ordering is M3's concern).
- DB-backed adapter is **opt-in via env**, offline baseline stays zero-dep
  (preserves the existing harness's "runs with no DB/API/network" guarantee).
- Thresholds above are the epic's hard gates on M1 and M3.

## Open questions (resolve in-flight, don't silently invent)

1. Exact YAML schema for nav-eval cases + hot-context-state fixtures (mirror the
   existing golden_set.yaml style + loader validation discipline).
2. Whether the "current hot-context window edge" for `recent_before_current` is
   modeled as an explicit anchor message id in the fixture or derived from a
   declared window size. (Recommend: explicit anchor — least ambiguous.)

## Constraints

- Deterministic, fast, offline for the default path (no network/API/DB). The
  DB-backed adapter is the only thing that may touch a DB, only when env-gated.
- Must not import-break or modify the existing harness's offline guarantee.

## Done criteria

- One command runs the extended ranked-search eval and prints a fair
  baseline-vs-semantic report including the paraphrase + verbatim breakdown and
  a fairness/difficulty axis.
- One command runs the nav-eval suite with exact-match pass/fail per case.
- One command runs the hot-context-inclusion suite and prints set-precision /
  set-recall @ budget per fixture + aggregate.
- The DB-backed adapter runs the same golden set against a local
  pgvector Postgres when `DIRECT_DATABASE_URL` is set (proven on a local DB or
  fixture, never prod).
- The three locked thresholds are encoded as assertions/flags so M1 and M3 can
  be gated programmatically.
- All new unit tests pass; README documents each new eval type + how to run.

## Touchpoints

- Extend: `eval/retrieval/golden_set.yaml`, `eval/retrieval/corpus.yaml`,
  `eval/retrieval/adapters.py` (DB-backed adapter), `eval/retrieval/runner.py`
  (adapter dispatch + new report sections), `eval/retrieval/README.md`.
- New: `eval/retrieval/nav_eval.py` + `eval/retrieval/nav_golden.yaml`;
  `eval/retrieval/hotcontext_eval.py` + `eval/retrieval/hotcontext_fixtures.yaml`;
  `eval/retrieval/metrics.py` (add set-precision/recall + exact-match).
- New tests under `tests/` mirroring `test_retrieval_eval_*`.
- Reference only: `app/services/tools/read_tools.py` (`search_messages` ILIKE
  semantics for the baseline), `app/services/hot_context.py` (last-20 window
  shape the inclusion metric models).

## Why this first

Cheapest de-risker for the whole epic. It turns the embedding-model and
granularity choices into measured go/no-go decisions, and it makes the user's
SIMPLE-navigation queries (which have exact correct answers) provable before any
of M1/M2/M3 is built.
