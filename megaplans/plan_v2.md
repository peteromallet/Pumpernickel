# Implementation Plan: Xen v1 M0 — Eval extension (fairness + simple-nav + hot-context-inclusion) — v2

## Overview
Extend the existing offline retrieval eval harness in `eval/retrieval/` with three new capabilities, plus an opt-in DB-backed adapter — without breaking the harness's zero-dep offline guarantee or touching `app/*`.

Three new questions the harness must answer:
1. **Fairness re-eval** of ranked search — does semantic actually beat keyword on real query *shapes*? Add `difficulty`/`fairness` tags to all 62 existing golden cases (via the generator), author ≥6 new keyword-plausible fair-shake cases, and extend `_make_comparison.py` to produce side-by-side reports keyed on those tags.
2. **Simple-nav correctness** — deterministic exact-ordered-match eval for `messages_before`/`messages_after`/`open_thread`/`scroll`/`topic_recent`/`before_message_id`/`recent_before_current`. Pass/fail per case.
3. **Hot-context-inclusion** — set-precision / set-recall @ budget over conversational-state fixtures.

Plus: an opt-in DB-backed adapter (`Retriever` Protocol) that runs against pgvector Postgres when `DIRECT_DATABASE_URL` is set; off by default. Plus: hard-gate threshold assertions for the M1 and M3 epic gates.

### Actual state of the codebase (verified)
- `eval/retrieval/schema.py:27-37` — `GoldenCase` has no `difficulty`/`fairness` fields yet. `query_type` is `Literal['topic_recall','verbatim_quote','paraphrase','cross_thread']`.
- `eval/retrieval/adapters.py:62-90` — `Retriever` Protocol with `retrieve(query, scope, *, thread_id, topic_id, limit)`. Fully functional adapters exist: `IlikeBaselineRetriever` (lines 93-163), `SemanticRetriever` (MiniLM cosine, lines 187-233), `HybridRetriever` (RRF fusion, lines 236-289), `StubSemanticRetriever` (empty). Semantic and Hybrid are NOT stubs — they run against local `all-MiniLM-L6-v2` embeddings.
- `eval/retrieval/runner.py:240-337` — argparse CLI; adapter dispatch at lines 298-314 supports `baseline`, `stub`, `semantic`, `hybrid`. `run_eval()` at line 55 populates per_case results with `query_type`/`scope` but not yet `fairness`/`difficulty`.
- `eval/retrieval/metrics.py` — `recall_at_k`, `reciprocal_rank`, `aggregate`, `aggregate_by_query_type`. No exact-match or set-precision/recall yet.
- `eval/retrieval/_generate_fixtures.py` — **Canonical source of truth** for `corpus.yaml` and `golden_set.yaml`. Contains `THREADS` (message data), `CASES` (62 cases, GC01–GC62), `build_golden()` (emits golden_set.yaml). CASES dicts already carry `hard_zero: bool` and `overlap_hint: str`.
- `eval/retrieval/_make_comparison.py` — Already produces three-way baseline-vs-semantic-vs-hybrid comparison reports. Reads per-adapter JSON reports, writes `reports/comparison_report.md`.
- `eval/retrieval/README.md` — Documents FAIR rebuild as complete (§2), generator workflow (§4), scope-model divergence (§3).
- Tests: `tests/test_retrieval_eval_metrics.py`, `test_retrieval_eval_adapters.py`, `test_retrieval_eval_runner.py`, `test_retrieval_eval_semantic.py`.

### Resolved design decisions
- **Generator-first**: All fixture changes (fairness/difficulty tags, new cases, nav anchor messages) go through `_generate_fixtures.py`'s `CASES`/`THREADS` data structures and `build_golden()`. The YAML files are never hand-edited.
- **Retriever Protocol extension**: Add `**extra_scope: Any` to the `Retriever.retrieve()` signature (default empty). The runner passes production-scope fields (`bot_id`, `participant`, `partner_share`, `date`) via `extra_scope` when using the DB-backed adapter. Existing call-sites and adapters are unaffected — they already accept keyword args.
- **M1 gate**: Gate flags (`--assert-m1-gate`, etc.) accept JSON report file paths as arguments (rather than coupling the runner to multi-adapter execution). `_make_comparison.py` is extended with fairness-by-tag tables and gate assertion logic.
- **Comparison reports**: Extend `_make_comparison.py` with fairness/difficulty breakdown tables. Do NOT add a `--compare` flag to the runner.
- **Nav `n=None` semantics**: `n` is *required* for `messages_before`, `messages_after`, `scroll`, `before_message_id` (missing = invalid case). `n` is *optional* for `topic_recent` (default 20), `recent_before_current` (default 20). `n` is *ignored* for `open_thread` (returns all messages in thread before anchor, chronological).
- **contiguous_boundary_ok**: Validates that the returned list is a contiguous subsequence of corpus chronological order AND that the first/last elements match the expected first/last. Interior elements are NOT checked (use `exact_ordered_match` for full-content validation). This metric answers "is the window frame in the right place?"
- **aggregate_set_metrics** returns `{"set_precision": float, "set_recall": float, "f1": float, "n": int}`.
- **Fairness/difficulty None in aggregation**: Groups under `"unlabeled"` string key, never Python `None`.
- **DB adapter construction failure**: The runner catches `ValueError` on adapter dispatch, prints a clear message, and exits with code 1. No traceback dump.
- **Hot-context loader validation**: `gap_continue` → gold ids NOT in last_window; `topic_switch` → gold topic_id ≠ last_window topic_id; `no_relevant_prior` → gold list is empty; `near_duplicate_prior` → gold ids share topic but describe similar events with distinguishing detail.

---

## Phase 1: Generator updates (canonical source of truth)

### Step 1: Add fairness/difficulty tags to all 62 existing cases + author ≥6 new keyword-plausible cases (`eval/retrieval/_generate_fixtures.py`)
**Scope:** Medium — Complexity: 3
1. **Add** `difficulty: Literal['easy','medium','hard']` and `fairness: Literal['keyword_favored','semantic_favored','either','adversarial']` to every dict in the `CASES` list (lines 569–818). Map existing `hard_zero` field: `hard_zero=True` → `difficulty='hard', fairness='adversarial'`. Derive other tags from query type + overlap characteristics:
   - `verbatim_quote` with strong overlap → `difficulty='easy', fairness='keyword_favored'`
   - `paraphrase` with nonzero overlap, not hard_zero → `difficulty='medium', fairness='semantic_favored'`
   - `cross_thread` → `difficulty='hard', fairness='semantic_favored'` (unless keyword-plausible, then `fairness='either'`)
   - `topic_recall` with good overlap → `difficulty='medium', fairness='either'`
2. **Author** ≥6 new cases where keyword also plausibly succeeds (`fairness='keyword_favored'` or `'either'`) — short, terse, context-dependent query *shapes* (not real user content) that genuinely contain substrings matchable by the ILIKE baseline.
3. **Update** `build_golden()` (line 837) to emit `difficulty` and `fairness` fields into the golden YAML output, after `query_type`.
4. **Run** `python -m eval.retrieval._generate_fixtures` to regenerate `corpus.yaml` and `golden_set.yaml`.
5. **Reconcile** with existing `hard_zero`/`overlap_hint` fields: `hard_zero` becomes a *derived* convenience flag in the CASES dict (kept for the fairness audit); `difficulty`/`fairness` are the canonical tags emitted to YAML.

### Step 1a: Add nav anchor messages to thread data (`eval/retrieval/_generate_fixtures.py`)
**Scope:** Small — Complexity: 2
1. **Add** deterministic anchor message ids to existing `THREADS` entries — messages at known positions within each thread that nav-eval fixtures can reference (e.g., a message roughly 30% through a thread, one near the end, one at the start). These are normal `CorpusMessage` entries with distinct content like `"[NAV_ANCHOR: thread_X midpoint]"` so they're recognizable.
2. **Add** at least one message per thread that can serve as a "current hot-context window edge" anchor for `recent_before_current` nav op testing.
3. **Regenerate** `corpus.yaml` via the generator.

---

## Phase 2: Schema extensions

### Step 2: Add difficulty/fairness fields to GoldenCase schema (`eval/retrieval/schema.py`, `eval/retrieval/loader.py`)
**Scope:** Small — Complexity: 2
1. **Add** to `GoldenCase` in `eval/retrieval/schema.py`:
   ```python
   difficulty: Literal['easy','medium','hard'] | None = None
   fairness: Literal['keyword_favored','semantic_favored','either','adversarial'] | None = None
   ```
   Both fields default to `None` for backward compatibility with test fixtures that construct `GoldenCase` directly.
2. **No loader changes needed** — `load_golden_set` validates via Pydantic; unknown tag values fail automatically. Since both fields are Optional, existing test fixtures without them continue to validate.
3. **Verify** `tests/test_retrieval_eval_runner.py`, `test_retrieval_eval_adapters.py`, `test_retrieval_eval_semantic.py` all still pass (schema is backward-compatible).

---

## Phase 3: Metric extensions

### Step 3: Add exact-match + set-precision/recall metrics (`eval/retrieval/metrics.py`, `tests/test_retrieval_eval_metrics.py`)
**Scope:** Small — Complexity: 2
1. **Add** to `eval/retrieval/metrics.py`:
   - `exact_ordered_match(returned: list[str], expected: list[str]) -> bool` — strict list equality (same ids, same order).
   - `contiguous_boundary_ok(returned: list[str], expected: list[str], corpus_order: list[str]) -> bool` — returned is a contiguous subsequence of `corpus_order` AND `returned[0] == expected[0]` AND `returned[-1] == expected[-1]`. Interior elements are NOT checked (this validates window-frame placement, not full content — use `exact_ordered_match` for full validation).
   - `set_precision(returned: set[str], expected: set[str]) -> float` — `|returned ∩ expected| / |returned|` (returns 1.0 if returned is empty).
   - `set_recall(returned: set[str], expected: set[str]) -> float` — `|returned ∩ expected| / |expected|` (returns 0.0 if expected is empty).
   - `aggregate_set_metrics(per_case: list[dict]) -> dict` — returns `{"set_precision": float, "set_recall": float, "f1": float, "n": int}`. F1 is harmonic mean of macro-averaged precision and recall. Returns zeros if per_case is empty.
   - `aggregate_by_group(per_case_results: list[dict], group_key: str) -> dict[str, dict]` — generic grouping function (extracted from `aggregate_by_query_type`) that groups by any key in per_case dicts. `aggregate_by_query_type` is refactored to call this.
2. **Test** each metric in `tests/test_retrieval_eval_metrics.py` with empty inputs, identity, partial overlap, budget-truncated, and boundary-only cases.

---

## Phase 4: Nav-eval module

### Step 4: Define nav-eval schema + YAML fixtures (`eval/retrieval/nav_eval.py`, `eval/retrieval/nav_golden.yaml`)
**Scope:** Medium — Complexity: 3
1. **Create** `eval/retrieval/nav_eval.py`. Define:
   ```python
   from typing import Literal
   from pydantic import BaseModel
   from eval.retrieval.schema import Scope

   NavOp = Literal['messages_before','messages_after','open_thread','scroll',
                    'topic_recent','before_message_id','recent_before_current']

   class NavCase(BaseModel):
       id: str
       op: NavOp
       anchor: str | None = None  # message id anchor; None for ops that don't need it
       n: int | None = None  # required for messages_before/after/scroll/before_message_id
       scope: Scope
       thread_id: str | None = None
       topic_id: str | None = None
       expected_ids_in_order: list[str]
       notes: str | None = None

   class NavGoldenSet(BaseModel):
       cases: list[NavCase]
   ```
2. **`n` semantics** (enforced in loader validation):
   - Required (non-None): `messages_before`, `messages_after`, `scroll`, `before_message_id`
   - Optional (defaults to 20): `topic_recent`, `recent_before_current`
   - Ignored: `open_thread` (returns all messages in thread, chronologically, up to anchor)
3. **Create** `eval/retrieval/nav_golden.yaml` with ≥12 cases covering every `op` (at least 1 per op, 2+ for the most-used ops). `recent_before_current` uses explicit anchor message ids from the generator (Step 1a).
4. **Add** `load_nav_golden(path, corpus) -> NavGoldenSet` to `nav_eval.py` with validation: every `expected_ids_in_order` id exists in corpus, `n` is non-None for required ops, anchor exists in corpus, scope/id consistency mirrors the golden-set loader.

### Step 5: Implement PythonNavReference + runner + CLI (`eval/retrieval/nav_eval.py`, `tests/test_nav_eval.py`)
**Scope:** Medium — Complexity: 3
1. **Implement** `PythonNavReference(corpus: Corpus)` — pure-python reference implementation. Builds a sorted-by-sent_at view of the corpus. One method per op:
   - `messages_before(anchor, n)` → the `n` messages immediately preceding `anchor` in chronological order (most recent first).
   - `messages_after(anchor, n)` → the `n` messages immediately following `anchor` in chronological order (oldest first).
   - `open_thread(anchor, thread_id)` → all messages in `thread_id` with `sent_at < anchor.sent_at`, chronological order.
   - `scroll(anchor, n)` → `n` messages centered on anchor (±n/2), chronological order.
   - `topic_recent(topic_id, n)` → the `n` most recent messages in `topic_id`.
   - `before_message_id(anchor_id, n)` → the `n` messages immediately before `anchor_id`'s position, chronological.
   - `recent_before_current(anchor, n)` → the `n` most recent messages with `sent_at < anchor.sent_at`, across all topics/threads (simulates the "last-N before now" window).
2. **Implement** `run_nav_eval(adapter, golden, corpus) -> NavReport`:
   ```python
   class NavReport(BaseModel):
       per_case: list[dict]  # {case_id, op, pass_exact, pass_boundary, returned, expected, notes}
       pass_rate_exact: float
       pass_rate_boundary: float
       n: int
   ```
3. **Add** CLI entrypoint: `python -m eval.retrieval.nav_eval --adapter reference [--nav-golden PATH] [--corpus PATH]`. Prints per-case table (case id, op, exact pass/fail, boundary pass/fail) and aggregate pass rate. Exits 0 only if all cases pass exact match.
4. **Test** in `tests/test_nav_eval.py` — each op type with correct fixtures, deliberately-wrong-order case that must fail, edge cases (empty results, single-message corpus slices).

---

## Phase 5: Hot-context-inclusion eval module

### Step 6: Define hot-context fixture schema + fixtures (`eval/retrieval/hotcontext_eval.py`, `eval/retrieval/hotcontext_fixtures.yaml`)
**Scope:** Medium — Complexity: 3
1. **Create** `eval/retrieval/hotcontext_eval.py`:
   ```python
   from typing import Literal
   from pydantic import BaseModel

   class HotContextState(BaseModel):
       id: str
       topic_id: str
       last_window_message_ids: list[str]  # the last-N hot window
       gold_prior_on_topic_ids: list[str]  # what a good system should surface
       budget: int = 5
       rationale: str
       category: Literal['gap_continue','topic_switch','no_relevant_prior','near_duplicate_prior']

   class HotContextFixtures(BaseModel):
       fixtures: list[HotContextState]
   ```
2. **Author** ≥8 fixtures in `hotcontext_fixtures.yaml` spanning all four category values:
   - `gap_continue` (≥3): Topic continues after a gap; relevant prior message is outside the last-N window.
   - `topic_switch` (≥2): Switch to a new topic; gold should surface prior on the *new* topic, not the old.
   - `no_relevant_prior` (≥1): Nothing relevant prior; gold is empty → precision test.
   - `near_duplicate_prior` (≥2): Multiple similar incidents; gold surfaces the authoritative one.
3. **Add** loader `load_hotcontext_fixtures(path, corpus) -> HotContextFixtures` with validation:
   - All `last_window_message_ids` and `gold_prior_on_topic_ids` exist in corpus.
   - `gap_continue`: gold ids are NOT in `last_window_message_ids`.
   - `topic_switch`: gold topic_id differs from the topic_id of `last_window_message_ids[-1]`.
   - `no_relevant_prior`: `gold_prior_on_topic_ids` is empty.
   - `near_duplicate_prior`: gold ids share a topic, and at least one other message in corpus shares key words with the gold but is NOT in gold.

### Step 7: Implement PythonReferenceSelector + runner + CLI (`eval/retrieval/hotcontext_eval.py`, `tests/test_hotcontext_eval.py`)
**Scope:** Medium — Complexity: 3
1. **Define** `HotContextSelector` Protocol:
   ```python
   class HotContextSelector(Protocol):
       def select(self, state: HotContextState, corpus: Corpus) -> set[str]: ...
   ```
   Returns at most `state.budget` message ids from the corpus (excluding those already in `last_window_message_ids`).
2. **Implement** `PythonReferenceSelector` — honest baseline: for `gap_continue` and `topic_switch`, returns the `budget` most recent messages in the state's topic that are NOT in `last_window` (recency-only). For `no_relevant_prior`, returns empty set. For `near_duplicate_prior`, same recency approach (it will get some right, some wrong — that's the point of the metric). This is NOT hand-tuned to game thresholds.
3. **Implement** `run_hotcontext_eval(selector, fixtures, corpus) -> HotContextReport`:
   ```python
   class HotContextReport(BaseModel):
       per_fixture: list[dict]  # {fixture_id, category, precision, recall, f1, budget, returned_count, gold_count}
       aggregate: dict  # {set_precision, set_recall, f1, n} from aggregate_set_metrics
       by_category: dict[str, dict]  # aggregate_set_metrics per category
   ```
4. **Add** CLI: `python -m eval.retrieval.hotcontext_eval --selector reference [--fixtures PATH] [--corpus PATH]`. Prints per-fixture + aggregate metrics.
5. **Test** in `tests/test_hotcontext_eval.py` — at least one fixture per category, empty-gold precision-only case, budget edge cases.

---

## Phase 6: Runner wiring + fairness/difficulty aggregation + gate flags

### Step 8: Propagate fairness/difficulty into runner results + extend EvalReport (`eval/retrieval/runner.py`, `eval/retrieval/metrics.py`)
**Scope:** Medium — Complexity: 2
1. **Extend** `run_eval()` (line 88–98) to include `fairness` and `difficulty` in each per_case result dict:
   ```python
   "fairness": case.fairness or "unlabeled",
   "difficulty": case.difficulty or "unlabeled",
   ```
   Use `"unlabeled"` string for None values so report tables never show Python `None`.
2. **Add** `aggregate_by_fairness` and `aggregate_by_difficulty` functions to `metrics.py` — thin wrappers around `aggregate_by_group(per_case_results, "fairness")` / `aggregate_by_group(per_case_results, "difficulty")`.
3. **Extend** `EvalReport` model (runner.py:38-47) with optional fields:
   ```python
   by_fairness: dict[str, dict[str, float | int]] | None = None
   by_difficulty: dict[str, dict[str, float | int]] | None = None
   ```
4. **Compute** these in `run_eval()` (after the existing `by_query_type` aggregation) and store on the report.
5. **Extend** `write_markdown_report()` to emit fairness and difficulty breakdown tables (only when the aggregates are non-None and non-empty). Tables follow the same format as per-query-type tables.
6. **Update** `tests/test_retrieval_eval_runner.py` for the new aggregations and report fields.

### Step 9: Extend _make_comparison.py with fairness/difficulty tables + gate assertion logic (`eval/retrieval/_make_comparison.py`)
**Scope:** Medium — Complexity: 2
1. **Add** fairness/difficulty breakdown tables to `_make_comparison.py` — load per-adapter JSON reports and produce tables showing recall@10 and MRR broken down by `fairness` tag and `difficulty` tag (side-by-side for baseline/semantic/hybrid).
2. **Add** `--assert-m1-gate` flag to `_make_comparison.py`: loads baseline and semantic/hybrid JSON reports, checks:
   - `semantic_or_hybrid by_query_type['paraphrase']['recall@10'] >= 0.7`
   - `semantic_or_hybrid by_query_type['verbatim_quote']['recall@1'] >= baseline by_query_type['verbatim_quote']['recall@1']`
   Exits non-zero with a clear message if either condition fails.
3. **Test** in `tests/test_retrieval_eval_runner.py` — synthetic report dicts that pass and fail the gate.

### Step 10: Add gate flags to nav-eval and hotcontext-eval CLIs (`eval/retrieval/nav_eval.py`, `eval/retrieval/hotcontext_eval.py`)
**Scope:** Small — Complexity: 1
1. **Add** `--assert-nav-gate` to nav-eval CLI: exits non-zero unless aggregate pass rate is 1.0 (100% exact-match).
2. **Add** `--assert-m3-gate` to hotcontext-eval CLI: exits non-zero unless `aggregate['set_recall'] >= 0.8` AND `aggregate['set_precision'] >= 0.6`.
3. **Test** each flag with synthetic report dicts that pass and fail.

---

## Phase 7: DB-backed adapter (opt-in)

### Step 11: Extend Retriever Protocol with extra_scope + implement DbBackedRetriever (`eval/retrieval/adapters.py`, `eval/retrieval/runner.py`)
**Scope:** Medium — Complexity: 3
1. **Extend** the `Retriever.retrieve()` Protocol signature with `**extra_scope: Any`:
   ```python
   def retrieve(self, query: str, scope: Scope, *, thread_id: str | None,
                topic_id: str | None, limit: int, **extra_scope: Any) -> list[str]: ...
   ```
   All existing implementations (`IlikeBaselineRetriever`, `SemanticRetriever`, `HybridRetriever`, `StubSemanticRetriever`) already accept keyword args — they just ignore unknown ones. Callers pass `**extra_scope` unchanged. Zero breakage.
2. **Implement** `DbBackedRetriever` in `adapters.py`:
   - Constructor checks `DIRECT_DATABASE_URL` env var; raises `ValueError("DIRECT_DATABASE_URL must be set to use DbBackedRetriever")` if unset.
   - Lazy imports `psycopg` (sync) and `pgvector` inside `__init__` — no module-level DB imports.
   - Production scope filter applied from `extra_scope`: `bot_id`, `participant`, `partner_share`, `date` when present. Falls back to `thread_id`/`topic_id`/`all` scope when production fields are absent.
   - Read-only SQL queries. `retrieve()` translates to pgvector similarity search with scope filters.
3. **Update** `runner.py`'s adapter dispatch (line 298–314) to support `--adapter db`:
   ```python
   elif adapter_name == "db":
       from eval.retrieval.adapters import DbBackedRetriever
       try:
           retriever = DbBackedRetriever(corpus)
       except ValueError as e:
           print(f"Error: {e}", file=sys.stderr)
           sys.exit(1)
   ```
4. **Add** `test_retrieval_eval_adapters.py` tests:
   - Without env, `DbBackedRetriever(corpus)` raises `ValueError`.
   - With env (skip if unset via `pytest.mark.skipif`), construction succeeds.
   - With a local fixture DB, `retrieve()` returns expected ids on a single golden case.

### Step 12: Wire DB-backed adapters into nav-eval and hotcontext-eval (`eval/retrieval/nav_eval.py`, `eval/retrieval/hotcontext_eval.py`)
**Scope:** Small — Complexity: 2
1. **Add** `DbNavAdapter` and `DbHotContextSelector` — thin wrappers that translate nav/hot-context operations to SQL queries against a pgvector Postgres. Same env-gating, same lazy-import pattern as Step 11.
2. **Hook** them into `--adapter db` / `--selector db` CLI branches.
3. **Document** that DB-backed adapters require `DIRECT_DATABASE_URL` and a pgvector-enabled Postgres with the messages table schema matching production.

---

## Phase 8: Retriever Protocol extension cleanup

### Step 13: Pass extra_scope through runner call sites (`eval/retrieval/runner.py`)
**Scope:** Small — Complexity: 1
1. **Update** `run_eval()` at line 80 to pass `extra_scope` to `retriever.retrieve()`. For the offline path, `extra_scope` is empty dict — zero effect. For `--adapter db`, the runner populates `extra_scope` from the golden case (or from CLI flags if we add `--bot-id` / `--participant` etc. in a follow-up).
2. **Add** `extra_scope: dict[str, Any] | None = None` parameter to `GoldenCase` in `schema.py` (default None → empty dict at call time). The generator can optionally emit `extra_scope` for cases that exercise production scope. This is the cleanest path: `GoldenCase` carries the optional production-scope metadata, `run_eval` passes it through.

---

## Phase 9: Documentation + final verification

### Step 14: Update README + final test/run pass (`eval/retrieval/README.md`)
**Scope:** Small — Complexity: 2
1. **Document** new features in `eval/retrieval/README.md`:
   - Fairness/difficulty tags (§2): how they map from existing `hard_zero`/`overlap_hint`, what each value means.
   - Nav-eval (§8 NEW): schema, golden set, CLI commands, example output.
   - Hot-context-inclusion (§9 NEW): metric (set-precision/recall/F1 @ budget), fixture categories, CLI commands.
   - DB-backed adapter (§10 NEW): env requirement, construction pattern, scope model, limitations.
   - Gate flags (§11 NEW): what each flag asserts, how to run.
2. **Update** §4 ("Editing the fixtures") to reference adding fairness/difficulty to CASES dicts.
3. **Update** §5 ("How to run") with new CLI commands.
4. **Run** full test suite:
   ```
   pytest tests/test_retrieval_eval_metrics.py tests/test_retrieval_eval_adapters.py \
          tests/test_retrieval_eval_runner.py tests/test_retrieval_eval_semantic.py \
          tests/test_nav_eval.py tests/test_hotcontext_eval.py -v
   ```
5. **Run** end-to-end offline CLIs:
   - `python -m eval.retrieval.runner --adapter baseline` (verify fairness/difficulty breakdowns in report)
   - `python -m eval.retrieval._make_comparison` (verify fairness/difficulty tables in comparison)
   - `python -m eval.retrieval.nav_eval --adapter reference`
   - `python -m eval.retrieval.hotcontext_eval --selector reference`

---

## Execution Order
1. Generator updates (Steps 1–1a) — foundation for all downstream modules.
2. Schema (Step 2) — enables metrics and runner.
3. Metrics (Step 3) — consumed by nav-eval, hot-context, runner.
4. Nav-eval (Steps 4–5) and Hot-context (Steps 6–7) — independent, can be done in parallel.
5. Runner wiring + aggregation + gates (Steps 8–10) — after metrics and evals exist.
6. DB-backed adapter (Steps 11–12) — after Protocol is extended.
7. Protocol cleanup (Step 13) — coordinates runner call sites.
8. Docs + final verification (Step 14) — last.

## Validation Order
1. Per-step unit tests (cheapest first: metrics → loaders → reference impls).
2. Runner-level integration tests after Phase 6.
3. Manual end-to-end CLI run (offline) in Step 14.
4. DB-backed adapter validated only when `DIRECT_DATABASE_URL` is present; otherwise skipped.
