# Xen v2 — M1: Content embeddings (unified index + per-type visibility + backfill)

> Part of the Xen v2 epic (`xen-v2-epic-plan.md`). The infrastructure spine.
> **Highest-risk milestone** — it generalizes the live table that holds the 1826
> production message vectors, encodes the per-type visibility correctness spine,
> and runs a live backfill that must preserve every existing vector. Runs
> **premium / thorough / high**. Reuses the v1 retriever spine; does NOT rebuild
> it. Grounded in `xen-v2-strategy.md:36-55`.

## Outcome

A single generalized embedding index over **all first-cut knowledge types** that
M2's retriever queries: `message_embeddings` becomes
`content_embeddings(source_type, source_id, …)`, the **1826 live message vectors
are preserved** by an in-place backfill (`source_type='message',
source_id=message_id` — never a re-embed), a `v_searchable_content` UNION-ALL
view exposes one arm per source type **each carrying its own visibility
predicate** (so retrieval can never surface what the dedicated hot-context
section hides), and the async embed job/worker is generalized to embed
distillations, memories, observations, and conversation_artifacts via per-type
canonical-text builders.

## Scope

IN:
- **Schema migration (new migration, next free number; verify at build time;
  with a `.down.sql`).**
  - **Generalize `message_embeddings` → `content_embeddings`.** Add
    `source_type TEXT NOT NULL DEFAULT 'message'` and `source_id UUID NOT NULL`;
    new PK `(source_type, source_id)`. Keep `embedding vector(N)`, `model text
    NOT NULL` (immutable per row), `embedded_at`, `content_hash`. The
    generalization must be **non-destructive**: existing rows are migrated to
    `source_type='message', source_id=message_id` **in place** (the 1826 live
    vectors survive — they are NOT re-embedded). Drop the old
    `message_id`-specific FK/PK only after the backfill column is populated.
  - **`v_searchable_content` = UNION ALL, one arm per source type, each arm
    carrying ITS OWN visibility predicate** (the correctness spine — see below).
    Arms for m1: `message`, `distillation`, `memory`, `observation`,
    `conversation_artifact`. The arm exposes the columns the retriever + visibility
    filter need (`source_type`, `source_id`, scope ids, the visibility columns).
  - **`embed_jobs` generalization.** Add `source_type TEXT NOT NULL DEFAULT
    'message'` + `source_id UUID NOT NULL`; update the dedupe index to key on
    `(source_type, source_id)`. (Resolves v1 open-Q: dedicated `embed_jobs` table
    is already the chosen substrate.)
- **Worker generalization (one loop, dispatch on `source_type`).** The existing
  embed worker gains a dispatch: `source_type` → a per-type **canonical-text
  builder**, then embed + upsert into `content_embeddings` keyed by
  `(source_type, source_id)`. Builders (canonical text per
  `xen-v2-strategy.md:16-26`):
  - `message` → existing behavior (unchanged).
  - `distillation` → `distillations.content`.
  - `memory` → `memories.content` (**NOT** `shareable_summary`).
  - `observation` → `observations.content`.
  - `conversation_artifact` → **type-aware jsonb text extraction** from
    `payload` (dispatch on artifact type — agenda / prep / debrief — to the
    relevant text fields; agenda/prep briefs are high-value prose).
- **Enqueue on write for the new types.** Where each type is created/edited,
  enqueue an embed job (`source_type`, `source_id`); on edit (`content_hash`
  mismatch) re-embed; on delete/soft-delete/status change that removes visibility,
  drop the embedding row. Mirror the existing message embed-on-write enqueue.
- **Per-type backfill (scripted, gated, NOT auto-run).** Extend the out-of-band
  backfill script: (1) migrate the existing message vectors in place (the 1826),
  (2) batch-embed all existing un-embedded distillations/memories/observations/
  artifacts (resumable, rate-limited), (3) verify coverage per type. The prod run
  is the **final gated op** — the script lands and is tested against a local/test
  pgvector DB; a human triggers the prod run.

OUT (anti-scope):
- No retriever changes (M2). No hot-context changes (M3). M1 ships schema + view
  + worker + builders + backfill only, exercised by unit/DB tests.
- **NEVER embed:** `commitments`, `watch_items` (structured, already in hot
  context), `out_of_bounds` (`sensitive_core` ENCRYPTED / partner-hidden — stays
  a hard exclusion filter, `retrieval.py:407`), `artifact_topics` /
  `artifact_links` (join tables — topic-filtering only). No `themes`, no
  `conversation_notes` (deferred to M4).
- No source-weighting (M2). No new embedding model — reuse the v1 model + dim
  (the `model` column stays immutable per row).
- No automatic prod backfill — human-gated.

## Locked decisions (from the epic plan; do not re-litigate)

- **Extend v1, don't rebuild.** `content_embeddings(source_type DEFAULT 'message',
  source_id)`; the 1826 live message vectors are PRESERVED by in-place backfill,
  never re-embedded.
- **`v_searchable_content` = UNION ALL, one arm per source type, each arm with
  its own visibility predicate.** This is the correctness spine.
- **First-cut types:** distillations, memories, observations,
  conversation_artifacts + existing messages. Canonical text exactly as the table
  above (memory = `content`, not `shareable_summary`; artifact = type-aware
  `payload` extract).
- `embed_jobs` + worker generalized; one loop dispatches on `source_type`.
- Reuse the v1 embedding model + dim (immutable per row); no model change.

## Per-type visibility — THE correctness spine (highest-stakes section)

Each `v_searchable_content` arm MUST replicate the **EXACT** visibility logic of
that type's existing dedicated hot-context section. If an arm's predicate is
looser than its section's, retrieval will surface content the section
deliberately hides — a privacy leak. Replicate, do not approximate:

- **memories** → `hot_context.py:836-862`. `status='active'` + `visibility`.
  **THE LEAK: `dyad_shareable`.** We embed `memories.content`, but a partner must
  only ever see `shareable_summary` — never the raw `content`. Two acceptable
  resolutions, pick one and document it: **(A) exclude `dyad_shareable` rows from
  the index entirely** (simplest, safest — the partner-facing summary path stays
  outside retrieval), OR **(B) keep them in the index but post-filter +
  substitute** `shareable_summary` for `content` before any partner-visible
  return (the substitution then lives on M2's return path). The arm MUST encode
  whichever choice so a partner can NEVER retrieve a `dyad_shareable` memory's raw
  `content`. This is the single highest-stakes item in the epic.
- **observations** → `hot_context.py:913-942`. `status='active' AND
  significance >= 3` + topic scope + `about_user_id`.
- **distillations** → `hot_context.py:986-1054`. `visibility` /
  `source_user_ids` / `partner_share`.
- **messages** → the v1 `v_searchable_messages` predicate (deleted/suppressed
  excluded + `raw_message_visibility`/`partner_share`), now expressed as the
  `message` arm.
- **out_of_bounds** is NEVER an arm — it remains a hard exclusion filter
  (`retrieval.py:407`); `sensitive_core` is encrypted/partner-hidden.

A launch gate: a test must prove that for each type, a row the dedicated section
would hide (a non-active memory, a `significance<3` observation, a partner-private
distillation, and crucially a `dyad_shareable` memory's raw `content`) NEVER
appears through `v_searchable_content`.

## Open questions

1. `dyad_shareable` resolution: exclude-from-index (A) vs post-filter+substitute
   (B). Recommend **A for m1** (keeps the leak out of the index by construction;
   simplest to prove correct); revisit B in m4 only if partner-visible shareable
   summaries must themselves be retrievable.
2. Artifact `payload` text extraction: how exhaustive per artifact type. Pick the
   high-value prose fields per type (agenda/prep/debrief); defer exotic types.

## Constraints

- Build + tests must NOT require live prod. Use a local/test pgvector DB (docker)
  or fixtures, as in v1.
- The 1826 live message vectors are **preserved, not re-embedded** — the
  migration is non-destructive and the backfill migrates them in place.
- `v_searchable_content` is the **only** retrieval entry point for any type; no
  path may bypass it (invisibility is a schema fact, not per-call discipline).
- `statement_cache_size=0` + transaction-per-call on the read path
  (`app/db.py:95-100`) — no query may assume cross-call session state.
- Backfill runs out-of-band on the 5432 `DIRECT_DATABASE_URL` session, never the
  6543 pooler.

## Done criteria

- Migration applies cleanly on a local pgvector DB and its `.down.sql` reverses;
  existing message-vector rows survive the generalization (`source_type='message',
  source_id=message_id`) — proven by a test counting vectors before/after.
- `v_searchable_content` returns rows for all five arms with correct columns;
  **a test proves each arm hides what its dedicated section hides** — non-active
  memory, `significance<3` observation, partner-private distillation, deleted/
  suppressed message, and **a `dyad_shareable` memory's raw `content` is never
  retrievable by a partner**.
- The worker dispatches on `source_type` to the right canonical-text builder and
  upserts `(source_type, source_id)` rows (test with a fake embedder, one case
  per type — incl. the type-aware artifact `payload` extractor).
- Enqueue-on-write fires for each new type on create/edit; re-embeds on
  `content_hash` change; drops the row on delete/visibility-loss (test).
- The per-type backfill script runs end-to-end against a local pgvector DB
  (migrate-in-place messages, embed all of each new type, verify per-type
  coverage); the prod run remains a documented, human-gated step.

## Touchpoints

- New/modify: the v2 migration (`migrations/*.sql` + `.down.sql`) generalizing
  `message_embeddings`→`content_embeddings` and `embed_jobs`, and creating
  `v_searchable_content`.
- Modify: `app/services/retrieval.py` (the view name / source-of-truth constant;
  no fusion change yet), the embed worker module (dispatch on `source_type`), the
  embedder usage stays as v1.
- New: per-type canonical-text builders (`canonical_memory_text`,
  `canonical_observation_text`, `canonical_distillation_text`, type-aware
  artifact `payload` extractor) — colocate with the worker.
- Modify: `scripts/backfill_embeddings.py` (in-place message migration + per-type
  backfill + per-type coverage verification).
- Modify: the write sites for distillations / memories / observations /
  conversation_artifacts to enqueue embed jobs.
- Reference (visibility spine): `app/services/hot_context.py:836-862` (memories),
  `:913-942` (observations), `:986-1054` (distillations);
  `app/services/retrieval.py:407` (OOB exclusion).
- Reuse: `app/db.py` `SET LOCAL` pattern; `cross_thread_privacy`,
  `partner_sharing` helpers; the v1 pluggable embedder + immutable model/dim.

## Anti-scope (explicit)

No retriever fusion changes, no source-weighting, no hot-context wiring, no new
embedding model, no embedding of commitments/watch_items/OOB/join-tables, no
themes/notes (M4), no automatic prod backfill, no re-embed of the existing
message vectors.
