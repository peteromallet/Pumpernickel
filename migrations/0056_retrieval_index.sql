-- 0056_retrieval_index: M1 retrieval schema foundation.
--
-- This migration is the explicit Xen v1 M1 reversal of the earlier project
-- guardrail that forbade pgvector/vector storage. Human sign-off is required
-- before applying it outside local/test environments. Operational guardrails:
-- application queries continue to use the pooled app database URL; production
-- embedding backfill and HNSW index creation must use a direct session-mode
-- connection, be started by a human, and never be launched by this migration.
--
-- Creates:
--   1. pgvector extension.
--   2. mediator.messages.search_suppressed_at.
--   3. Coalesced mediator.messages.search_tsv generated from canonical fields.
--   4. mediator.message_embeddings with vector(1536) and canonical SHA-256 hash.
--   5. mediator.embed_jobs for async embed/reembed/drop work.
--   6. mediator.v_searchable_messages as the only production retrieval SQL
--      read surface. Worker cleanup may read raw messages only to delete
--      embeddings/jobs for deleted or search-suppressed rows.
--
-- No messages.content_hash column is added. Canonical SHA-256 hashes live on
-- mediator.message_embeddings and mediator.embed_jobs only.
--
-- HNSW CREATE INDEX CONCURRENTLY is deliberately absent: PostgreSQL forbids
-- CONCURRENTLY inside a transaction, and production HNSW builds must run from
-- the gated backfill/ops script on a direct connection.
--
-- Canonical text field order:
--   content, media_analysis->>'explanation', media_analysis->>'description',
--   media_analysis->>'summary'
-- Each field is COALESCE'd to the empty string and joined with a single newline.
-- Python helpers must use the same field order before hashing or embedding.

BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE mediator.messages
    ADD COLUMN IF NOT EXISTS search_suppressed_at timestamptz;

ALTER TABLE mediator.messages
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'simple'::regconfig,
            COALESCE(content, '') || E'\n' ||
            COALESCE(media_analysis->>'explanation', '') || E'\n' ||
            COALESCE(media_analysis->>'description', '') || E'\n' ||
            COALESCE(media_analysis->>'summary', '')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_messages_search_tsv
    ON mediator.messages USING GIN (search_tsv);

CREATE INDEX IF NOT EXISTS idx_messages_searchable_scope_sent
    ON mediator.messages (bot_id, topic_id, sent_at DESC, id)
    WHERE deleted_at IS NULL
      AND search_suppressed_at IS NULL;

CREATE TABLE IF NOT EXISTS mediator.message_embeddings (
    message_id      uuid PRIMARY KEY
        REFERENCES mediator.messages(id) ON DELETE CASCADE,
    embedding       vector(1536) NOT NULL,
    model           text NOT NULL,
    dimension       integer NOT NULL CHECK (dimension = 1536),
    content_hash    text NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    embedded_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_message_embeddings_model_dimension
    ON mediator.message_embeddings (model, dimension);

CREATE INDEX IF NOT EXISTS idx_message_embeddings_embedded_at
    ON mediator.message_embeddings (embedded_at);

CREATE TABLE IF NOT EXISTS mediator.embed_jobs (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id      uuid NOT NULL
        REFERENCES mediator.messages(id) ON DELETE CASCADE,
    job_kind        text NOT NULL
        CHECK (job_kind IN ('embed','reembed','drop')),
    status          text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','processing','succeeded','failed','skipped','superseded','cancelled')),
    model           text,
    dimension       integer CHECK (dimension IS NULL OR dimension > 0),
    content_hash    text CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),
    attempts        integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_error      text,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    locked_at       timestamptz,
    locked_by       text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    completed_at    timestamptz,
    CHECK (
        (job_kind = 'drop' AND content_hash IS NULL)
        OR (job_kind IN ('embed','reembed') AND content_hash IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_embed_jobs_claim
    ON mediator.embed_jobs (next_attempt_at, created_at, id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_embed_jobs_message_status
    ON mediator.embed_jobs (message_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_embed_jobs_active_dedupe
    ON mediator.embed_jobs (message_id, job_kind, COALESCE(content_hash, ''))
    WHERE status IN ('pending','processing');

CREATE OR REPLACE VIEW mediator.v_searchable_messages AS
SELECT
    m.id AS message_id,
    m.direction,
    m.sender_id,
    m.recipient_id,
    CASE
        WHEN m.direction = 'inbound' AND m.sender_id IS NOT NULL THEN m.sender_id
        WHEN m.direction = 'outbound' AND m.recipient_id IS NOT NULL THEN m.recipient_id
        ELSE COALESCE(m.sender_id, m.recipient_id)
    END AS thread_owner_user_id,
    m.sent_at,
    m.content,
    m.media_type,
    m.media_analysis,
    m.bot_id,
    m.topic_id,
    bb.dyad_id,
    ubs.partner_share AS thread_owner_partner_share,
    COALESCE(m.content, '') || E'\n' ||
        COALESCE(m.media_analysis->>'explanation', '') || E'\n' ||
        COALESCE(m.media_analysis->>'description', '') || E'\n' ||
        COALESCE(m.media_analysis->>'summary', '') AS canonical_text,
    m.search_tsv
FROM mediator.messages m
LEFT JOIN mediator.bot_bindings bb
  ON bb.bot_id = m.bot_id
 AND bb.dyad_id IS NOT NULL
LEFT JOIN mediator.user_bot_state ubs
  ON ubs.user_id = CASE
        WHEN m.direction = 'inbound' AND m.sender_id IS NOT NULL THEN m.sender_id
        WHEN m.direction = 'outbound' AND m.recipient_id IS NOT NULL THEN m.recipient_id
        ELSE COALESCE(m.sender_id, m.recipient_id)
    END
 AND ubs.bot_id = m.bot_id
WHERE m.deleted_at IS NULL
  AND m.search_suppressed_at IS NULL;

COMMENT ON VIEW mediator.v_searchable_messages IS
    'M1 retrieval read surface. Production retrievers must read messages through this view so deleted/search-suppressed rows are excluded consistently.';

COMMIT;
