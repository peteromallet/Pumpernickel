# Retrieval Comparison: Keyword vs Semantic vs Hybrid

**Question:** Does semantic search beat keyword (ILIKE) search for message
retrieval on the synthetic eval corpus, and by how much?

- **Corpus:** `eval/retrieval/corpus.yaml` (60 synthetic messages, 2 topics, 4 threads)
- **Golden set:** `eval/retrieval/golden_set.yaml` (28 labeled query -> expected-message cases)
- **Metrics:** macro-averaged recall@{1,5,10} + MRR (see `metrics.py`)
- **Embedding backend used:** **local sentence-transformers `all-MiniLM-L6-v2`**
  (384-dim, runs fully offline; embeddings cached to disk in
  `eval/retrieval/.embedding_cache/`).
  - OpenAI `text-embedding-3-small` was **not** used: no `OPENAI_API_KEY` was
    present in the environment.
  - The TF-IDF char-ngram "floor" backend was available as a last resort but
    was **not** needed.
- **Retrievers compared:**
  - `baseline` — `IlikeBaselineRetriever` (case-insensitive substring match over
    `content` + `media_analysis.{explanation,description,summary}`).
  - `semantic` — `SemanticRetriever` (cosine similarity over MiniLM embeddings,
    same scope filtering as baseline).
  - `hybrid` — `HybridRetriever` (Reciprocal Rank Fusion, k=60, of the keyword
    and semantic rankings — the design the Xen brief proposes).

## Overall metrics

| Metric     | Baseline (ILIKE) | Semantic | Hybrid (RRF) |
|------------|-----------------:|---------:|-------------:|
| recall@1   | 0.1429           | **0.3077** | 0.2958     |
| recall@5   | 0.2560           | **0.6527** | **0.6527** |
| recall@10  | 0.2619           | **0.8732** | **0.8732** |
| MRR        | 0.2500           | **0.7241** | 0.7063     |

Semantic and hybrid more than **3x** the baseline on recall@10 (0.26 -> 0.87)
and nearly **3x** MRR (0.25 -> 0.72). The lift is real, not marginal.

## Per-query-type breakdown — recall@10

| Query type      | n | Baseline | Semantic | Hybrid |
|-----------------|--:|---------:|---------:|-------:|
| verbatim_quote  | 8 | 0.9167   | **1.0000** | **1.0000** |
| paraphrase      | 10| **0.0000** | **0.9000** | **0.9000** |
| cross_thread    | 4 | **0.0000** | **0.6667** | **0.6667** |
| topic_recall    | 6 | 0.0000   | **0.7970** | **0.7970** |

## Per-query-type breakdown — MRR

| Query type      | n | Baseline | Semantic | Hybrid |
|-----------------|--:|---------:|---------:|-------:|
| verbatim_quote  | 8 | 0.8750   | **1.0000** | 0.9375 |
| paraphrase      | 10| 0.0000   | **0.3610** | **0.3610** |
| cross_thread    | 4 | 0.0000   | **0.8750** | **0.8750** |
| topic_recall    | 6 | 0.0000   | **0.8611** | **0.8611** |

## The headline cases: paraphrase and cross_thread

These are where the baseline scores **0%** by construction (paraphrase queries
share no substring with their targets; topic/cross-thread queries are
conceptual, not lexical). They are the reason the harness exists.

- **paraphrase: 0% -> 90% recall@10.** 9 of 10 paraphrase cases are now
  recovered within the top 10. The single miss is GC21 *"NPE resolution"* — the
  target uses the spelled-out *"null pointer exception"* and MiniLM does not
  bridge the **NPE** acronym to it. MRR here is modest (0.36): semantic finds the
  right message but often not at rank 1, so paraphrase is a recall win more than
  a precision win.
- **cross_thread: 0% -> 67% recall@10, MRR 0.875.** These are topic-scoped
  queries that must gather many messages spread across two threads (expected
  sets of 6-9 messages). The *first* relevant hit lands very high (MRR 0.875),
  but full recall of the whole set is capped at @10 — recall@10 can't exceed
  10/expected when expected > 10, and ranking noise costs the rest. The thesis
  (semantic understands topic membership; keyword cannot) clearly holds.
- **topic_recall: 0% -> 80% recall@10, MRR 0.861** — same story.

## Verdict: semantic vs hybrid

- **Semantic search decisively beats the keyword baseline** and justifies the
  build on this corpus: recall@10 0.26 -> 0.87, and it converts the two 0%
  failure modes (paraphrase, cross_thread) into 90% / 67%.
- **On this corpus, pure semantic is marginally better than hybrid.** They are
  *identical* on recall@5 and recall@10 (RRF reorders within the top-k but the
  same documents are present), but pure semantic edges ahead on the
  rank-sensitive metrics: overall MRR 0.724 vs 0.706, and recall@1 0.308 vs
  0.296. The reason is that RRF blends in the keyword ranking, which on
  verbatim cases occasionally demotes the exact-match hit from rank 1
  (verbatim MRR drops 1.000 -> 0.9375). Hybrid never *loses* recall here, but it
  doesn't add any either, because the semantic ranker already finds everything
  the keyword ranker finds.

**Recommendation:** semantic retrieval is worth building. Hybrid/RRF is a safe
default in production (it can only help when the embedding model has a blind
spot the keyword path covers — e.g. the **NPE** acronym miss above, or rare
proper nouns), but on this clean synthetic corpus it shows no measurable recall
advantage over semantic alone and a tiny MRR cost. Re-measure hybrid on the
real corpus before concluding it's redundant.

## Caveats (read before trusting these numbers)

- **Synthetic corpus.** It was hand-designed to make paraphrase/cross-thread
  cases that ILIKE *must* miss (zero substring overlap). That guarantees the
  baseline floor is 0% on those types — real user queries overlap lexically with
  messages far more often, so the real-world baseline will not be this bad and
  the semantic lift will likely be **smaller** than 3x.
- **Tiny scale.** 60 messages, 28 queries, one dyad (Alice/Bob), two topics. With
  so few candidates per scope, cosine ranking has an easy job; recall@10 over a
  ~dozen-candidate pool is generous. Numbers here are directional, not predictive
  of production precision.
- **Model dependence.** Results are for MiniLM specifically. OpenAI
  `text-embedding-3-small` would likely do at least as well (and might catch the
  NPE acronym), but was untestable without a key.
- **Scope model divergence.** Per the harness README §3, the harness scope model
  (thread/topic/all) is a simplification of production `search_messages`
  (bot/participant/partner/date filters). These numbers do not reflect production
  recall; a DB-backed adapter with the full scope model is still needed before
  shipping.

---

Reports regenerated via:
```
python -m eval.retrieval.runner --adapter baseline
python -m eval.retrieval.runner --adapter semantic
python -m eval.retrieval.runner --adapter hybrid
```
Raw per-adapter reports: `baseline_report.{json,md}`, `semantic_report.{json,md}`,
`hybrid_report.{json,md}` in this directory.
