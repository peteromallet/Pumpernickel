# Xen v1 — Message Search & Navigation Epic (Plan)

> Ships an excellent message search + navigation system into the Veas app **and**
> into the hot context of **every agent**, serving content-relevant queries —
> both COMPLEX (semantic/meaning) and SIMPLE (recent-before-current, before-a-
> given-ID, scrollback, topic-scoped recent). Grounded in the v3 design spine
> (`xen-retrieval-brief.md`) descoped to "Surface 1 only + per-message embeddings
> + a single suppress-tier forget".

This is the top-level tie-together. Each milestone has a self-contained brief
under `megaplans/`. Build them in order; later milestones depend on earlier ones.

---

## Prerequisite decisions resolved here (so milestones don't re-litigate)

These were left open in `xen-retrieval-brief.md` §"Open questions"/§v3. Resolved
now with a defensible default; genuinely-human-call items are flagged at the end.

1. **Embedding granularity → per-message.** Forced by the forget model
   (`xen-retrieval-brief.md:284-291`): forgetting one message must not contaminate
   a shared window vector. One row → one vector. (Recall risk from terse messages
   is mitigated by hybrid keyword+semantic, not by window-pooling.)

2. **Embedding model → a single immutable named model with a fixed dim, stored
   per row.** Default: a hosted embeddings API (OpenAI `text-embedding-3-small`,
   **1536 dims**) — chosen for zero local-GPU/infra cost, strong terse-text
   recall, and a stable hosted contract. The model id + dim are recorded in a
   `message_embeddings.model` column and are **immutable for a given vector**;
   re-embedding under a new model is a new backfill, never an in-place mutation.
   Repo has no embedding dep today (confirmed: no openai/voyage/pgvector in
   pyproject) — this is net-new and intentional. (If product rejects sending
   message text to a hosted vendor, the drop-in alternative is a local
   `bge-small-en-v1.5` / `gte-small`, both 384-dim, via `fastembed`; the schema
   stores dim so the swap is a backfill, not a redesign. This vendor-vs-local
   choice is the one genuine human call — see bottom.)

3. **Pooler approach → split read path from build path.**
   - **Query/runtime** runs on the existing transaction pooler (6543,
     `statement_cache_size=0`, transaction-per-call — `app/db.py:95-100`). ANN
     tuning is `SET LOCAL hnsw.ef_search = N` **inside the same
     transaction** as the query (the only session state the pooler can hold;
     `SchemaPool` already issues `SET LOCAL search_path` per txn — same pattern).
   - **Index build + embedding backfill** run **out of band on a 5432 session
     connection** (a separate `DIRECT_DATABASE_URL`), never through the app pool.
     `CREATE INDEX CONCURRENTLY` and long backfills need session state the pooler
     cannot hold. This is a scripted, gated op (Milestone 1), not auto-run.
   - **Embed-on-write is async**, never inline on the message-write path: writing
     a message enqueues an embed job; a worker embeds and upserts the vector.
     Honoured everywhere `deleted_at`/visibility is honoured.

4. **Hybrid method → Reciprocal Rank Fusion (RRF).** Keyword half = Postgres
   `tsvector`/GIN full-text (net-new — today it's `ILIKE`, `read_tools.py:438-447`).
   Semantic half = pgvector cosine ANN. Fuse with RRF (`score = Σ 1/(k+rank)`,
   k=60 default) rather than tuning a weighted blend — RRF needs no per-corpus
   weight calibration and is robust to score-scale mismatch. The eval harness
   (Milestone 4) is what proves RRF ≥ semantic-alone ≥ keyword-alone before
   anything ships.

5. **Forget tier in v1 → suppress only.** A single `messages.search_suppressed_at`
   flag (or a `message_search_state` row) that physically excludes the row from
   search/scrollback/the vector index via the query-time view. Erase-tier (GDPR
   hard-delete + `total` decrement) and the derived-data re-derivation cascade are
   **deferred** (`xen-retrieval-brief.md:199-209`). v1's promise: "stop surfacing
   this," not "it never happened." The reply-turn agent never sees a tombstone.

6. **Surface scope → Surface 1 only (navigation + point lookup + scrollback +
   search).** Surface 2 (bulk/enumeration — `debrief.py` already does it right)
   and Surface 3 (aggregation/trends) are **out of this epic** per the v3
   descope verdict. The user's stated SIMPLE examples (recent-before-current,
   before-ID, scrollback, topic-recent) are all Surface 1.

---

## Milestone order & dependencies

```
M0  Eval fairness + simple-nav + hot-context-inclusion eval   (no prod deps)
        │  (extends existing eval/retrieval/ — gate for M1)
        ▼
M1  Production retriever + index (pgvector + tsvector + RRF + async embed)
        │  (depends on M0 go/no-go threshold; backfill scripted, gated, not auto-run)
        ▼
M2  Agent tool surface — SIMPLE nav verbs + COMPLEX search   (depends on M1)
        │
        ▼
M3  Hot-context integration for ALL agents                    (depends on M1, M2)
```

Note on M0 vs the existing `xen-eval-harness-brief.md`: that harness already
shipped (`eval/retrieval/` exists: corpus, golden_set, runner, metrics,
adapters, README — 28 cases). **M0 extends it**; it does not rebuild it. M0 is
deliberately first because it is the cheapest de-risker and the **go/no-go gate
on the embedding-granularity and model choices** before M1 spends any backfill.

| # | Milestone | Brief file | One-line |
|---|-----------|-----------|----------|
| M0 | Eval: fairness + simple-nav + hot-context-inclusion | `xen-v1-m0-eval-extension-brief.md` | Extend the eval harness with a real-DB fairness adapter, deterministic SIMPLE-nav assertions, and a hot-context-inclusion metric — the gate for M1. |
| M1 | Production retriever + index | `xen-v1-m1-retriever-index-brief.md` | pgvector per-message embeddings + tsvector/GIN + RRF hybrid retriever, async embed-on-write, out-of-band gated backfill on 5432, runtime ANN tuning via SET LOCAL, all behind a visibility+suppress view. |
| M2 | Agent tool surface | `xen-v1-m2-tool-surface-brief.md` | SIMPLE nav tools (`messages_before`, `open_thread`/`scroll`, jump/before-ID, topic-recent) + COMPLEX `search(mode=exact\|semantic)` with snippets, resolved speaker labels, edit/retraction surfacing — wired into the registry for all relevant bots. |
| M3 | Hot-context integration | `xen-v1-m3-hot-context-brief.md` | New bounded "previous on this topic" section in `hot_context.py` for every agent: topic-scoped recent + semantically-relevant prior messages beyond the last-20 window, plus the cursor that ties nav to the hot-context window. |

---

## GOAL → milestone mapping (so nothing is dropped)

The verbatim intent decomposes as follows. Every clause maps to a milestone.

| Goal clause | Milestone(s) |
|---|---|
| "excellent search system ... serving all content-relevant queries" | M1 (retriever) + M2 (search tool) |
| "implemented fully into the app" | M2 (tools available to agents = the app's surface; web UI nav reuses the same cursor/scroll contract from M2) |
| "into the hot context of all the agents" | M3 (new section in `hot_context.py`, available to every bot) |
| COMPLEX queries (semantic/meaning) | M1 (semantic + RRF) + M2 (`search(mode=semantic)`) |
| "most recent messages before the current ones in hot context" | M2 (`messages_before(anchor="current", n)`) + M3 (cursor ties "current" to the hot-context window edge) |
| "before a certain message ID" | M2 (`messages_before(anchor=message_id, n)`) |
| scrollback | M2 (`open_thread(around)` + `scroll(cursor, dir, n)`) |
| topic-scoped recent | M2 (`topic_recent`) + M3 (surfaced in hot context) |
| "navigable in an intuitive way" | M2 (stable cursors, spoken-summary-friendly results) + M3 (the hot-context handle) |
| tests for SIMPLE nav (deterministic, assert-exact) | M0 |
| tests for COMPLEX search (recall@k go/no-go) + fairness re-eval | M0 |
| "evaluate hot-context inclusion: are the right previous-on-topic messages surfaced" | M0 (new metric + fixtures) |

---

## Recommended megaplan dials (per milestone)

Dials per `megaplan-prep`: **profile** (intelligence tier), **robustness**
(review/critique depth), **depth** (thinking).

| # | Profile | Robustness | Depth | Why |
|---|---------|-----------|-------|-----|
| M0 | mid (claude/gpt mix) | standard | medium | Pure offline test code over an existing harness; deterministic; low blast radius. The hot-context-inclusion metric design is the only subtle part. |
| M1 | high | high (adversarial — this touches a live DB invariant + reverses a README rule + new infra) | deep | Highest-risk milestone: pgvector on a pooler, async embed worker, gated prod backfill, visibility correctness, README invariant reversal. Wants the strongest profile + an adversarial gate before the prod op. |
| M2 | mid-high | high | medium-deep | Touches the tool registry + step/bot allowlists (`registry.py`) and verbatim-integrity semantics (exact vs semantic, edit/retraction surfacing) — correctness-sensitive but bounded. |
| M3 | mid-high | standard-to-high | medium | Edits the heart of the system (`hot_context.py`), token-budget sensitive; wants care but the queries are bounded SQL over M1's retriever. |

---

## The one genuine human call (flagged, not silently decided)

**Vendor vs local embeddings (privacy posture).** M1's default sends raw message
text to a hosted embeddings vendor (OpenAI). Veas content is intimate dyadic
messaging; some users/jurisdictions may forbid that. The schema is designed so
the choice is a backfill, not a redesign (dim + model stored per row). **Decision
needed before M1's backfill op runs**, not before M1's code lands. If unanswered,
M1 builds against a local `bge-small-en-v1.5` (384-dim, `fastembed`, no data
egress) as the safe default and leaves the hosted adapter as an opt-in config.

All other v3 open questions are resolved above (granularity, pooler, hybrid,
forget tier, surface scope).
