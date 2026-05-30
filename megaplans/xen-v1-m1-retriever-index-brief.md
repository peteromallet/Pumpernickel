# Xen v1 — M1: Production retriever + index (pgvector + tsvector + RRF)

> Part of the Xen v1 epic (`xen-v1-epic-plan.md`). The infrastructure spine.
> **Highest-risk milestone** (live-DB invariant, async worker, gated prod
> backfill, README reversal). Depends on M0's go/no-go thresholds. Reverses the
> README "must not create pgvector" rule (`README.md:91-92`) — a conscious,
> documented sign-off.

## Outcome

A production-grade hybrid retriever over `messages` that M2's tools and M3's
hot-context queries call: per-message semantic embeddings (pgvector ANN) +
Postgres full-text (tsvector/GIN) fused by RRF, honoring every visibility rule
at query time, with embedding done **async on write** and the index built /
backfilled **out of band on a 5432 session connection** as a scripted, gated op
— never auto-run, never inline, never on the 6543 pooler.

## Scope

IN:
- **Schema (new migration `0056_*`, next free number; verify at build time).**
  - Enable `pgvector` (reverses `README.md:91-92` — document the sign-off in the
    migration header and update the README line).
  - `message_embeddings` table (per-message, **not** a column on `messages`, so
    forget/re-embed is a clean row op and dim is per-vector):
    `message_id uuid PK REFERENCES messages(id) ON DELETE CASCADE`,
    `embedding vector(1536)` (dim per the model decision in the epic plan —
    parameterize so a 384-dim local model is a backfill, not a redesign),
    `model text NOT NULL` (immutable per row), `embedded_at timestamptz`,
    `content_hash text` (detect edits → re-embed). HNSW index built
    `CONCURRENTLY` on a 5432 session.
  - **Full-text**: a generated/maintained `tsvector` over `content` +
    `media_analysis->>{explanation,description,summary}` (the same fields
    `search_messages` ILIKEs today, `read_tools.py:438-447`) with a GIN index.
    Net-new — today there is no FTS.
  - **Suppress-tier forget**: `messages.search_suppressed_at timestamptz` (or a
    `message_search_state` row). v1 forget = suppress only (epic decision 5).
  - **A query-time view / helper** (`v_searchable_messages` or a SQL function)
    that is the single source of "what is searchable": excludes
    `deleted_at IS NOT NULL` AND `search_suppressed_at IS NOT NULL`, and carries
    the columns the visibility filter needs. **Every** new retriever path reads
    through this — invisibility is a schema fact, not per-call discipline
    (`xen-retrieval-brief.md:246-247`).
- **Async embed-on-write.** A job/worker (mirror the existing scheduled_jobs /
  inbound-queue worker pattern — `migrations/0004`, `0041`) that: on message
  insert/edit, enqueues an embed job; the worker computes the embedding via the
  pluggable embedder and upserts `message_embeddings`. **Never inline on the
  message write path** (`xen-retrieval-brief.md:226`). On edit (`edited_at`
  changes / `content_hash` mismatch) → re-embed. On suppress/delete → delete the
  embedding row (cascade handles delete; suppress handler deletes explicitly).
- **Pluggable embedder.** One interface `embed(texts) -> list[vector]`; default
  impl = the chosen model (epic decision 2: hosted `text-embedding-3-small`
  1536-dim, OR local `bge-small` 384-dim if the vendor call is the human-call
  rejection). Model id + dim are recorded per row and immutable. New net-new
  Python dep — add it; the repo has none today.
- **Hybrid retriever (the core).** A service function (call it
  `app/services/retrieval.py::hybrid_search(...)`) that:
  - Runs the semantic half: cosine ANN over `message_embeddings` joined to
    `v_searchable_messages`, with `SET LOCAL hnsw.ef_search = N` **in the same
    transaction** (the only session state the 6543 pooler holds —
    `app/db.py:39-41` `SchemaPool` already does `SET LOCAL search_path` this way;
    follow that exact pattern).
  - Runs the keyword half: `tsvector @@ websearch_to_tsquery` ranked by
    `ts_rank`.
  - Fuses with **RRF** (k=60 default; epic decision 4). Returns ranked
    `message_id`s + per-hit `{match_type: exact|semantic|both, rrf_score,
    keyword_rank, semantic_rank}`.
  - Honors scope params (bot_id, topic_id, participant ids) and
    `raw_message_visibility()` (`app/services/cross_thread_privacy.py:49`) +
    `partner_share` exactly as `search_messages` does today
    (`read_tools.py:469-483`). **Prove the embedding/FTS index honors
    OOB/partner_share/deleted/suppress before launch** — it is a gate, not a
    nice-to-have (`xen-retrieval-brief.md:51-53`).
  - Supports `mode=exact` (keyword-only, for verbatim quotes — semantic hits may
    never be presented as quotes, `xen-retrieval-brief.md:88-91`) and
    `mode=semantic` (full hybrid).
- **Out-of-band backfill script (scripted, gated, NOT auto-run).** A standalone
  script run against `DIRECT_DATABASE_URL` (5432 session mode) that: builds the
  HNSW index `CONCURRENTLY`, batch-embeds all existing un-embedded messages
  (resumable, rate-limited), and verifies coverage. The actual prod run is the
  **final gated op** of this milestone — the script lands and is tested against a
  local/test pgvector DB; a human triggers the prod run.

OUT (anti-scope):
- No agent tools (M2). No hot-context wiring (M3). M1 ships the service +
  schema + worker + script only, exercised by M0's DB-backed adapter and unit
  tests.
- No erase-tier forget, no derived-data cascade (epic decision 5 — deferred).
- No Surface 2 (bulk/snapshot `as_of`) or Surface 3 (aggregation). The held-
  snapshot completeness contract needs session state the pooler can't hold and
  is explicitly out (`xen-retrieval-brief.md:207-209`).
- No automatic prod backfill — it is human-gated.

## Locked decisions (from the epic plan; do not re-litigate)

- Per-message embeddings (forget-clean). Immutable model+dim per row.
- Query on 6543 pooler with `SET LOCAL` ANN tuning; build/backfill out-of-band
  on 5432 session.
- Async embed-on-write, never inline.
- Hybrid = tsvector/GIN + pgvector ANN fused by RRF (k=60).
- v1 forget = suppress tier only, enforced via the searchable view.
- Embedding model per epic decision 2 (hosted default; local fallback is a
  config+backfill swap, not a redesign).

## Open questions

1. Worker substrate: reuse `scheduled_jobs` vs a dedicated `embed_jobs` table.
   (Recommend a dedicated table — embeds are high-volume and shouldn't compete
   with user-facing check-ins.)
2. HNSW build params (`m`, `ef_construction`) — pick defaults, let M0's recall
   threshold confirm; `ef_search` is the runtime knob.

## Constraints

- Build + tests must NOT require live prod. Use a local/test
  Postgres-with-pgvector (docker) or fixtures. `.env.example` gains
  `DIRECT_DATABASE_URL` (5432) alongside the existing 6543 `DATABASE_URL`.
- `statement_cache_size=0` + transaction-per-call on the read path is a hard
  constraint (`app/db.py:95-100`) — no query may assume cross-call session state.
- The searchable view is the only retrieval entry point; no path may bypass it.

## Done criteria

- Migration applies cleanly on a local pgvector DB (and its `.down.sql`
  reverses); README pgvector line updated with the sign-off.
- `hybrid_search` returns RRF-fused ranked hits honoring scope + visibility +
  suppress + deleted, with per-hit match_type; proven by a test asserting a
  suppressed/deleted/partner-private message never appears via any mode.
- Async embed worker embeds on insert, re-embeds on edit, drops on
  suppress/delete; proven by tests with a fake embedder.
- The backfill script runs end-to-end against a local pgvector DB (index built
  CONCURRENTLY, all messages embedded, coverage verified); the prod run remains
  a documented, human-gated step.
- M0's DB-backed adapter, pointed at this retriever, **passes the M0 go/no-go
  thresholds** (paraphrase recall@10 ≥ 0.7, no verbatim regression).

## Touchpoints

- New: `migrations/0056_*.sql` (+ `.down.sql`); `app/services/retrieval.py`;
  `app/services/embeddings.py` (pluggable embedder); embed worker (new module +
  table); `scripts/backfill_embeddings.py`.
- Modify: `README.md:91-92` (reverse pgvector invariant, documented);
  `.env.example` (`DIRECT_DATABASE_URL`); `app/config.py` (direct url + embedder
  config); message-write path to enqueue embed jobs (`app/services/messaging.py`
  insert site + edit site).
- Reuse: `app/db.py` `SET LOCAL` pattern; `cross_thread_privacy.raw_message_
  visibility`; `partner_sharing.get_partner_share`.
- Reference: `eval/retrieval/` (M0 adapter scores this).

## Anti-scope (explicit)

No tools, no hot-context, no erase/cascade, no bulk/aggregation surfaces, no
auto prod backfill, no per-window embeddings, no weighted-blend ranking.
