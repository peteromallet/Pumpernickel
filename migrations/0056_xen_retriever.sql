-- 0056_xen_retriever: Xen v1 M1 — production retriever schema.
--
-- Introduces the pgvector extension, a generated `search_tsv` tsvector on
-- messages, the message_embeddings table (no HNSW index yet — see below),
-- the embed_jobs work queue, the v_searchable_messages view, and the
-- search_suppressed_at soft-suppression column.
--
-- README pgvector reversal sign-off: the project README previously discouraged
-- pgvector; that guidance is reversed for Xen v1 M1. Hybrid retrieval
-- (pgvector + tsvector via RRF) is the production path.
--
-- HNSW defaults (created later by the backfill step, not this migration):
--   m = 16
--   ef_construction = 64
--   hnsw.ef_search = 40   (set per-session at query time)
--
-- HNSW index creation is intentionally deferred to the backfill phase so
-- that the index is built against populated data on local pgvector and
-- promoted with the rest of the wiring in a single coordinated step.

BEGIN;

-- ── Extension ─────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS vector;

-- ── messages: search_suppressed_at + generated search_tsv ─────────────────

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS search_suppressed_at timestamptz;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS search_tsv tsvector
    GENERATED ALWAYS AS (
        to_tsvector(
            'english',
            coalesce(content, '') || ' ' ||
            coalesce(media_analysis->>'explanation', '') || ' ' ||
            coalesce(media_analysis->>'description', '') || ' ' ||
            coalesce(media_analysis->>'summary', '')
        )
    ) STORED;

CREATE INDEX IF NOT EXISTS messages_search_tsv_gin
    ON messages USING gin (search_tsv);

-- ── message_embeddings (HNSW intentionally deferred to backfill) ──────────

CREATE TABLE IF NOT EXISTS message_embeddings (
    message_id uuid PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    embedding vector(1536),
    model text NOT NULL,
    embedded_at timestamptz NOT NULL DEFAULT now(),
    content_hash text NOT NULL
);

-- ── embed_jobs work queue ─────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS embed_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    message_id uuid NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'in_progress', 'done', 'failed', 'dead')),
    priority integer NOT NULL DEFAULT 0,
    scheduled_at timestamptz NOT NULL DEFAULT now(),
    attempts integer NOT NULL DEFAULT 0,
    last_error text,
    locked_by text,
    locked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS embed_jobs_status_priority_scheduled_at
    ON embed_jobs (status, priority DESC, scheduled_at);

CREATE INDEX IF NOT EXISTS embed_jobs_message_id
    ON embed_jobs (message_id);

-- ── v_searchable_messages view ────────────────────────────────────────────

CREATE OR REPLACE VIEW v_searchable_messages AS
    SELECT *
    FROM messages
    WHERE deleted_at IS NULL
      AND search_suppressed_at IS NULL;

COMMIT;
