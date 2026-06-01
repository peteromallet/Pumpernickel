# Xen v1 — Embedding decision + pre-launch validation plan

> Durable record of the design conversation so the main thread can compact.
> Companion to `xen-v1-epic-plan.md` + the M0–M4 briefs.

## Decision: Option A — hosted embeddings + our own pgvector — **LOCKED (validated by test #1, 2026-05-31)**

- **Embeddings:** hosted `text-embedding-3-small` (1536-d) called as a stateless
  text→vector converter — on message write (async worker) and on query.
- **Index + search + ranking:** stays in **our Supabase** (pgvector ANN + tsvector
  keyword, RRF fuse). Vectors, messages, and the matching all live in our DB, so
  all visibility/partner-share/OOB/deleted/suppress gating enforces in SQL.
- **Why:** Railway-light (no model weights/heavy RAM in the container, fast cold
  start), keeps the safety gating local (vs. an external vector store, where ACL
  gets dangerous), tiny to operate at Veas scale, and a 1-line swap from M1's
  current local default (schema already stores model+dim → reversible).
- **Privacy posture — RESOLVED:** message *text* is sent to the embedding vendor;
  this tradeoff is **accepted as the chosen default**. The local ONNX model
  (fastembed, **in the worker only**, slower/heavier on Railway) stays the
  reversible fallback. Because the schema stores model+dim per row and the only
  text egress is at the gated M1 backfill, the decision remains cleanly
  revisitable up to that op — but the default is locked to hosted.

## Cost reframe (the important part)

**Validation cost ≠ launch cost. They're decoupled.**
- *Launch* expense = embedding the **entire message history** (backfill) + a query
  embed on every semantic search, ongoing, at scale.
- *Validation* only needs to embed the **eval sample** — the golden-set corpus
  (a few hundred messages) + the queries. At `text-embedding-3-small` (~$0.02 / 1M
  tokens) that's **fractions of a cent**. No prod backfill, no Railway, no deploy.

So you spend **cents to decide whether to spend the real money** — exactly the
pre-launch de-risk you want.

## Pre-launch validation plan (cheap → trusted)

1. **Retriever quality (cents, mechanical).** ✅ **BUILT** (2026-05-31). Added
   `OpenAIEmbedder` (text-embedding-3-small, 1536-d, disk-cached, explicit L2-norm)
   to `eval/retrieval/embeddings.py` and two runner adapters: `openai` (semantic-only,
   the apples-to-apples vs local `semantic`) and `hybrid-openai` (RRF, the real prod
   shape). Parses/imports clean; `openai` pkg already installed (2.32.0); no-key path
   raises a friendly error. **Only blocker = `OPENAI_API_KEY` not set.** Once set:
   `OPENAI_API_KEY=… python -m eval.retrieval.runner --adapter openai` (then
   `--adapter hybrid-openai`) → writes `eval/retrieval/reports/openai_report.{json,md}`.
   Compare hosted recall@k/MRR vs local 0.86 recall@10 and the keyword baseline.
   Corpus = 273 msgs + 70 queries ≈ 4k tokens ⇒ **sub-cent per run**. *Caveat:
   synthetic corpus — validates the mechanism, not real data (that's #2).*
2. **Real-data golden set (cents of embeddings + human labeling — the GATE).** ~20–40
   realistic queries over the *real* messages with correct targets marked by a human;
   embed only those (still cents). The only signal that says "delivers relevant
   context for OUR data/phrasing." **Must pass before trusting in the live app.**
   ✅ **SCAFFOLDED** (2026-05-31): `eval/retrieval/extract_real_corpus.py` (bounded
   pull from prod `messages` via `DIRECT_DATABASE_URL`; synthesizes thread_id from
   `in_reply_to` chains, `no_topic` sentinel for null topics, resolves names from
   `users.name`), `browse_corpus.py` (find target ids), `real_golden_set.template.yaml`
   (committed, placeholder), `REAL_GOLDEN_SET.md` (labeling guide), `.gitignore` updated
   so real_corpus.yaml + real_golden_set.yaml stay out of git. Loader-accepts-template
   verified. **Remaining = human labeling** (find ids via browse, fill 20–40 cases
   balanced across the 4 query_types, over-weight topic_recall — the #1 watch-item).
   Run: `runner --adapter hybrid-openai --corpus real_corpus.yaml --golden
   real_golden_set.yaml`. GATE: recall@10 ≥ ~0.80, no topic_recall regression vs local.
3. **Shadow + LLM-as-judge (post-launch net).** Behind a flag, retrieve-but-don't-act
   on real queries; log query→retrieved; LLM rates relevance. Ongoing prod health.
4. **M4 Sisypy** — orthogonal: does the *agent* invoke/use the tools well.

**Recommendation:** #1 immediately (validates the Option-A swap for pennies) → #2 as
the launch gate → #3 as the always-on net.

### #1 RESULT (2026-05-31, n=70, key from repo .env, cost < 1¢)

| Adapter | r@1 | r@5 | r@10 | MRR |
|---|---|---|---|---|
| baseline (ILIKE) | 0.338 | 0.502 | 0.510 | 0.660 |
| semantic-local (MiniLM 384-d) | 0.464 | 0.741 | 0.853 | 0.849 |
| openai-hosted (te-3-small 1536-d) | 0.440 | 0.754 | **0.864** | 0.821 |
| **hybrid-openai (hosted + RRF)** | **0.482** | **0.757** | **0.864** | **0.883** |

By query type (recall@10): hosted **wins paraphrase** (0.962 vs local 0.902) and
cross_thread (0.710 vs 0.681); **loses topic_recall** (0.696 vs 0.779, n=12); ties
verbatim (0.955). **Verdict: Option A validated on the mechanism** — hosted ≥ local on
recall@10, and **hybrid-openai (the prod RRF shape) is the best config on every overall
metric.** The one watch-item is topic_recall, where local edged hosted (small n=12);
re-check on the real-data golden set (#2). Hosted's MRR/recall@1 dip vs local is erased
once RRF fusion is added. *Synthetic corpus — #2 (real data) remains the launch gate.*

## ✅ EPIC COMPLETE (2026-06-01)

All 5 milestones built (vendor: codex), merged to `origin/main`, auto-merge:
- m0-eval (#13) · **m1-retriever #15** (`ffe6cf9`) · **m2-tools #16** (`4103cf6`) ·
  **m3-hotcontext #17** (`ecba4d6`) · **m4-sisypy #18** (`01dd308`).
- Landed: `app/services/retrieval.py` (hybrid_search), `migrations/0056_retrieval_index`,
  `app/services/hot_context.py` (+solo) "previous on this topic" section, the M2 tool
  surface, and the M4 sisypy structural-run evidence.
- Docker stayed wedged the whole run but never mattered — execute completed via fixtures.
- **Remaining (NOT part of the epic):** the gated prod embedding **backfill** (human-run,
  sends real message text to OpenAI) and validation **#2 real-data golden set** (the launch
  gate — scaffolding shipped, needs human labeling). Code is on main; nothing is deployed
  (web app still offline; auth PR #12 + secret rotation still a separate track).

## Status / blockers (historical — pre-completion)

- Chain: **M0 ✅ merged**. **M1 = FAILED PARTIAL** (corrected 2026-05-31). PR #14
  (`epic/xen-m1-retriever`, open, `awaiting_human`) is **only 4 of 11 tasks** —
  migration `0056` (pgvector ext + `message_embeddings vector(1536)` + `search_tsv`
  + `search_suppressed_at` + searchable view, HNSW deferred) + `app/config.py` +
  `.env.example` + `docker-compose.yml` + an eval adapter tweak. The embedder,
  async worker, `hybrid_search`/retrieval.py, all write-path enqueue hooks, the
  backfill script, the M0-adapter wiring, and the README flip **never executed** —
  the run died on **ENOSPC** (`/private/tmp/claude-501`) mid-build (T3 embedder +
  T7a retrieval blocked). **DO NOT merge PR #14 as M1 done**: the chain tracks
  completion at the PR level, so merging it marks m1 complete and advances to m2,
  silently skipping ~64% of M1. The schema in #14 *is* correct + matches the locked
  hosted-OpenAI/1536 decision (not stale).
- The existing M1 plan (`xen-v1-m1-production-20260530-0619`) is pinned to the
  **2026-05-30 brief snapshot** — today's query-embed latency-budget/cache/degrade
  additions and `vendor: codex` are NOT in its scope. Resuming it finishes the OLD
  scope under Claude; a fresh re-plan is required to pick up both.
- **DONE (2026-05-31):** validation #1 ran (results above); M1 brief + epic plan +
  chain now lock the embedding default to hosted OpenAI (`text-embedding-3-small`,
  1536-d, hybrid+RRF), with local `bge-small` as the reversible fallback. Remaining
  epic items: #2 (real-data golden set — the launch gate) and #3 (shadow + judge).
- Web app remains offline on Railway (security holes fixed in PR #12, awaiting merge +
  secret rotation — separate track).
