# Retrieval Evaluation Harness

## 1. Purpose

This is an **offline retrieval evaluation harness** for measuring and comparing
message-retrieval implementations over a **deterministic synthetic fixture
corpus**. It exists to answer the question: *"Is a given retriever better than
today's keyword (ILIKE) search?"* — before any embedding, vector index, or
production database is involved.

It produces standard IR metrics (recall@k, MRR) per golden test case and
aggregated by query type, with JSON and markdown reports.

## 2. Methodology

### Why a synthetic corpus?

The production corpus is small and privacy-sensitive; labeling it manually is
not feasible for this sprint. A synthetic fixture lets us design specific
adversarial conditions that stress-test retrieval in ways a real (but
privately-scrubbed) corpus might not surface.

### Hard-case categories

The corpus deliberately exercises six failure modes for naive string matching:

| Category | What it tests | Why ILIKE struggles |
|---|---|---|
| **Terse replies** | One-word / low-signal messages (`"fine."`, `"sure"`, `"I told you so."`) | ILIKE matches only if the query contains the exact word; semantic meaning is lost. |
| **Paraphrase pairs** | Target wording restates the query's intent with synonyms / different phrasing — e.g. a message *"the OAuth2 login integration is mostly done"* queried as `login integration` or `caching layer` for *"I've been thinking about the caching layer"*. | ILIKE matches the whole `text_contains` argument as one substring. A restated query rarely matches verbatim, so ILIKE either misses or ranks by recency rather than meaning. |
| **Cross-thread continuations** | The same `topic_id` appears across two different `thread_id` values. A topic-scoped query must return messages from **all** threads sharing that topic. | ILIKE can find a lexical hit in *one* thread, but the topic answer spans **both** threads; the second thread usually restates the topic in different words, so substring matching leaves recall low. |
| **Near-duplicate incidents** | Two messages describe the same event with wording differing by one detail, plus **decoy** incidents that share words but are the wrong answer (e.g. Nexus payment-transaction duplication vs. a notification-email duplication vs. Orion billing double-charge). | ILIKE finds all lexical matches indiscriminately; a semantic system should rank the right incident and reject the same-word decoy. |
| **Same-word-different-meaning distractors** | Words reused with a different sense: API `rate` limiting vs. customer-satisfaction `rate`; software `crash` vs. a roller-coaster scare; dinner `reservation` vs. having `reservations` (doubts); a chess `move` vs. an apartment `move`. | ILIKE has no way to tell the senses apart — these are pure lexical traps that hurt its precision. |
| **Media-analysis-only signal** | Messages whose `content` is generic (`"Check this out"`, `"See attached"`) with all relevant information in `media_analysis.{explanation,description,summary}`. | ILIKE baseline **does** search these fields, but only if the query substring appears there — it cannot *understand* the media description. |

### Fairness: the baseline gets a real lexical shot (this rebuild)

> **History.** The first version of this harness deliberately built every
> paraphrase / cross-thread query to share **zero** substrings with its target,
> pinning the ILIKE baseline at exactly 0% on those types. That floored the
> baseline artificially and inflated the semantic lift (recall@10 0.26 → 0.87,
> ~3x+). **This rebuild fixes that bias** so the comparison is trustworthy.

The `IlikeBaselineRetriever` is a pure-Python reimplementation of production
`search_messages` ILIKE semantics: it matches the **whole query string** as a
case-insensitive substring (`content ILIKE '%query%'`) across `content` and the
three `media_analysis` fields. To give it a *fair* shot:

- **Paraphrase / cross-thread / topic-recall queries are short, keyword-style
  search phrases** — the way a user or agent actually drives `search_messages` —
  and most of them contain a contiguous substring that genuinely appears in at
  least one expected target. The baseline therefore scores **nonzero** on these
  types. The semantic win has to come from *meaning*: pulling restated/synonym
  targets the baseline misses, spanning the second thread of a topic, and
  out-ranking same-word decoys — not from an artificial zero-overlap floor.

- **A labeled minority of paraphrase cases are genuinely zero-overlap** (synonym
  only, marked `[HARD zero-overlap]` in the golden-set notes, e.g. `UV protection`
  → *"sunscreen"*, `NPE fix` → *"null pointer ... fixed it"*). These preserve a
  pure-semantic ceiling measurement but do **not** dominate.

The generator `_generate_fixtures.py` prints a fairness audit showing, per query
type, how many cases the whole-query substring can match — confirming the
baseline is no longer 0% on paraphrase/cross-thread. See
`reports/comparison_report.md` for the fair three-way numbers (baseline vs
semantic vs hybrid) that **supersede** the first run's inflated figures.

## 3. Scope-model divergence: harness vs. production `search_messages`

**This section is required reading** before comparing harness results to
production behavior.

### Harness baseline (`IlikeBaselineRetriever`)

The harness `IlikeBaselineRetriever` implements a simplified scope model using
only the concepts present in the synthetic corpus:

| Scope  | Filter applied                  | Used for                                           |
|--------|---------------------------------|-----------------------------------------------------|
| thread | `message.thread_id == thread_id` | Isolate messages within a single conversation thread |
| topic  | `message.topic_id == topic_id`   | Cross-thread retrieval within a shared topic          |
| all    | No filter                        | Entire corpus                                        |

It orders results by `(sent_at DESC, id DESC)` for deterministic ranking per
[SD3](/Users/peteromalley/Documents/Veas/megaplans/xen-eval-harness-brief.md).

### Production `search_messages` (`app/services/tools/read_tools.py`)

The production `search_messages` function applies a **much richer** scope model
that the harness deliberately does not exercise. It includes:

- **`bot_id` scoping**: Messages are always scoped to the current bot.
- **`topic_id` scoping**: Always present; ties messages to a topic.
- **Participant scoping**: Filters by `sender_id` / `recipient_id` based on the
  current user and partner.
- **Partner sharing visibility**: Respects `partner_share` flags for cross-user
  visibility.
- **`_message_in_current_scope` filtering**: Additional session-level scope
  rules.
- **Local-day / date-range filtering**: Temporal window constraints.

### Why this divergence exists (deliberate, per SD2)

The harness is a **pure-Python re-implementation** of ILIKE semantics only,
designed to run completely offline with zero database, zero API keys, and zero
imports from `app.*`. The synthetic corpus uses `thread_id`/`topic_id` as its
scoping primitive because that matches the fixture design — not because those
columns exist in production tables (the messages table has no `thread_id`
column).

**Consequence**: Harness recall numbers reflect the lexical substring-matching
ability of the baseline within a simplified scoping model. They do **not**
reflect production recall because production applies additional filters
(bot/participant/partner/date). A retriever that scores well in the harness
still needs end-to-end evaluation against real production queries with full
scoping to confirm production behavior.

This is accepted scope-model divergence per SD2; a future follow-up slice may
add a DB-backed adapter that exercises the full production scope model.

## 4. How to add a golden case

### Required fields

Add a new entry under `cases:` in `golden_set.yaml`:

```yaml
- id: GC29
  query: "your search query here"
  expected_message_ids:
    - m001
    - m015
  scope: topic            # "thread" | "topic" | "all"
  query_type: verbatim_quote  # "topic_recall" | "verbatim_quote" | "paraphrase" | "cross_thread"
  thread_id: thread_nexus_kickoff   # required only when scope == "thread"
  topic_id: topic_project_nexus     # required only when scope == "topic"
  notes: "Optional notes about this case."
```

### Validation rules (enforced by loader)

The loader (`eval/retrieval/loader.py`) enforces these at load time:

1. **`expected_message_ids` must be non-empty** (SD6 / correctness-4). A case
   with an empty expected list will raise `ValueError`.
2. **Every message id in `expected_message_ids` must exist in the corpus**.
   A dangling reference raises `ValueError`.
3. **Scope/id consistency**:
   - `scope == 'thread'` requires a **non-None `thread_id`**.
   - `scope == 'topic'` requires a **non-None `topic_id`**.
   - `scope == 'all'` must have `thread_id` and `topic_id` omitted or `None`.
   Violations raise `ValueError`.

### Editing the fixtures

The corpus and golden set are **generated** by `_generate_fixtures.py`; do not
hand-edit `corpus.yaml` / `golden_set.yaml` (they carry a "DO NOT hand-edit"
header). Edit the generator's `THREADS` / `CASES` data and re-run it.

#### Paraphrase case design notes (fair)

A *fair* paraphrase case restates the query's intent and shares **some** real
lexical overlap with at least one expected target, so the keyword baseline can
score nonzero:

1. Write a short, keyword-style `query` (what a user would type into search),
   containing a contiguous phrase that appears in an expected target.
2. Make the *right* answer require meaning — e.g. several messages share the
   phrase but only one matches the intent, or the second half of the expected
   set restates the topic with synonyms the baseline can't reach.
3. Set `query_type: paraphrase`. Document the shared substring in the note
   (the generator emits `(overlap: ...)`).
4. For a genuinely hard synonym-only case, set `hard_zero=True`; it is labeled
   `[HARD zero-overlap]` and is expected to be a baseline miss. Keep these a
   minority.

Run `python -m eval.retrieval._generate_fixtures` — it validates all
`expected_message_ids` exist and prints the per-type fairness audit.

## 5. How to run

### Prerequisites

- Python 3.11+
- Baseline / stub: `pip install pydantic pyyaml` — no database, no API keys, no
  network.
- Semantic / hybrid: additionally `pip install sentence-transformers numpy` and
  the `all-MiniLM-L6-v2` model present in the local Hugging Face cache (the
  embedder forces `HF_HUB_OFFLINE=1`, so it never hits the network). Corpus
  embeddings are cached to `reports/.emb_cache/` keyed by a content hash, so
  re-runs are deterministic and fast.

### Run an adapter

```bash
python -m eval.retrieval.runner --adapter baseline   # today's keyword/ILIKE
python -m eval.retrieval.runner --adapter semantic   # dense MiniLM cosine
python -m eval.retrieval.runner --adapter hybrid     # RRF(baseline, semantic)
python -m eval.retrieval.runner --adapter stub       # empty (proves the seam)
```

### Regenerate fixtures + the fair comparison report

```bash
# Rebuild corpus.yaml + golden_set.yaml deterministically (prints fairness audit)
python -m eval.retrieval._generate_fixtures
# Run all three adapters, then build reports/comparison_report.md
python -m eval.retrieval.runner --adapter baseline
python -m eval.retrieval.runner --adapter semantic
python -m eval.retrieval.runner --adapter hybrid
python -m eval.retrieval._make_comparison
```

### Run the semantic and hybrid adapters

```bash
python -m eval.retrieval.runner --adapter semantic   # cosine over embeddings
python -m eval.retrieval.runner --adapter hybrid      # RRF(keyword, semantic)
```

The `semantic` adapter (`SemanticRetriever`) embeds every corpus message and
the query and ranks scope-filtered candidates by cosine similarity. The
`hybrid` adapter (`HybridRetriever`) fuses the keyword (ILIKE) and semantic
rankings with Reciprocal Rank Fusion (k=60).

**Embedding backend selection** (`eval/retrieval/embeddings.py`,
`get_default_embedder`), in priority order:

1. OpenAI `text-embedding-3-small` — used only if `OPENAI_API_KEY` is already
   in the environment (the key is never read, logged, or hardcoded by this code;
   the openai SDK reads it). 
2. Local sentence-transformers `all-MiniLM-L6-v2` — used if importable; runs
   fully offline.
3. TF-IDF char-ngram **floor** — NOT a real embedding; a deterministic sanity
   floor used only when neither real backend is available. Reports must label it
   as such.

Corpus embeddings are cached to disk under `eval/retrieval/.embedding_cache/`
(gitignored) so reruns are cheap and need no network. A `--comparison`-style
side-by-side of all three retrievers lives in
`reports/comparison_report.md`.

Adapter tests use a tiny deterministic fake embedder so they need no network:

```bash
pytest tests/test_retrieval_eval_semantic.py -v
```

### Custom paths

```bash
python -m eval.retrieval.runner --adapter baseline \
    --corpus eval/retrieval/corpus.yaml \
    --golden eval/retrieval/golden_set.yaml \
    --out-dir eval/retrieval/reports/
```

### Default paths (used when flags are omitted)

| Flag        | Default                               |
|-------------|---------------------------------------|
| `--corpus`  | `eval/retrieval/corpus.yaml`          |
| `--golden`  | `eval/retrieval/golden_set.yaml`      |
| `--out-dir` | `eval/retrieval/reports/`             |

### Output

Reports are written to the output directory (created automatically if missing):

```
eval/retrieval/reports/
├── baseline_report.json   # Full structured report (per-case + aggregates)
├── baseline_report.md     # Human-readable markdown (tables, per-query-type breakdown)
├── stub_report.json
└── stub_report.md
```

### Running tests

```bash
# Unit tests for each component
pytest tests/test_retrieval_eval_metrics.py -v
pytest tests/test_retrieval_eval_adapters.py -v
pytest tests/test_retrieval_eval_runner.py -v

# All retrieval eval tests
pytest tests/test_retrieval_eval_metrics.py \
        tests/test_retrieval_eval_adapters.py \
        tests/test_retrieval_eval_runner.py -v
```

## 6. How to implement a new adapter

### The `Retriever` Protocol

Every adapter must satisfy the `Retriever` protocol defined in
`eval/retrieval/adapters.py`:

```python
from typing import Protocol
from eval.retrieval.schema import Corpus, Scope


class Retriever(Protocol):
    """Protocol for retrieval adapters."""

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None,
        topic_id: str | None,
        limit: int,
    ) -> list[str]:
        """Retrieve ranked message ids for a query.

        Args:
            query: The search query string.
            scope: Filter scope ('thread', 'topic', or 'all').
            thread_id: Required for scope=='thread', ignored otherwise.
            topic_id: Required for scope=='topic', ignored otherwise.
            limit: Maximum number of results to return.

        Returns:
            Ordered list of message ids (rank 1 = index 0), truncated to limit.
        """
        ...
```

### Skeleton adapter

```python
from eval.retrieval.schema import Corpus, Scope


class MySemanticRetriever:
    """A semantic retriever using <your method here>."""

    def __init__(self, corpus: Corpus) -> None:
        self._corpus = corpus
        # Build your index here (embeddings, BM25, etc.)

    def retrieve(
        self,
        query: str,
        scope: Scope,
        *,
        thread_id: str | None = None,
        topic_id: str | None = None,
        limit: int = 50,
    ) -> list[str]:
        # 1. Apply scope filter (same pattern as IlikeBaselineRetriever)
        candidates = self._corpus.messages
        if scope == "thread":
            candidates = [m for m in candidates if m.thread_id == thread_id]
        elif scope == "topic":
            candidates = [m for m in candidates if m.topic_id == topic_id]
        # scope == 'all': no filter

        # 2. Score candidates against the query
        scored = []
        for msg in candidates:
            score = self._score(query, msg)  # Your similarity function
            if score > 0:
                scored.append((score, msg))

        # 3. Sort by score descending, then by deterministic tiebreaker
        scored.sort(key=lambda x: (x[0], x[1].sent_at, x[1].id), reverse=True)

        # 4. Slice to limit and return ids
        return [msg.id for _, msg in scored[:limit]]

    def _score(self, query: str, message) -> float:
        """Return a relevance score for query vs. message."""
        # TODO: implement your scoring (cosine similarity, BM25, etc.)
        return 0.0
```

### Registering the adapter in the CLI

In `eval/retrieval/runner.py`, add your adapter to the `main()` function's
adapter dispatch:

```python
from eval.retrieval.adapters import MySemanticRetriever

# In main():
if adapter_name == "my_semantic":
    retriever = MySemanticRetriever(corpus)
```

Or, if your adapter requires external dependencies (e.g. a model download), add
it as a lazy import so the baseline remains dependency-free.

## 7. Out-of-scope (deferred)

The following are **explicitly deferred** to a follow-up slice:

- **Real-corpus labeling and golden set**: The synthetic fixture is a stand-in.
  Producing a manually-labeled golden set from the production corpus requires
  privacy review and is not part of this sprint.

- **DB-backed adapter**: An adapter that connects to a real database (e.g. via
  `DATABASE_URL`), applies the full `search_messages` scope model (bot_id,
  participant visibility, partner sharing, date filters), and runs the harness
  against real production data. This is the natural next step once the harness
  is proven.

- **Production scope-model fidelity**: The harness baseline's scope model
  (`thread_id`, `topic_id`, `all`) diverges from production `search_messages`
  (which uses `bot_id` + `topic_id` + participant scoping with date and partner
  filters). See §3 above for the full accounting.

- **Embeddings, vector indexes, pgvector**: The harness provides the evaluation
  seam; selecting embedding granularity, building indexes, and implementing a
  semantic retriever is a separate project gated on this baseline.

- **No production code changes**: The harness must not import from `app.*`,
  modify production tables, or require a live database. It lives entirely under
  `eval/retrieval/`.
