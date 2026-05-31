-- 0056_retrieval_index.down: Reverse M1 retrieval schema foundation.
--
-- Drops the view first, then job/embedding tables, indexes, generated FTS
-- artifacts, suppression column, and finally pgvector only when no vector
-- columns remain. HNSW indexes are not dropped here because the forward
-- migration deliberately never creates them; concurrently-built HNSW indexes
-- are owned by the gated backfill/ops workflow.

BEGIN;

DROP VIEW IF EXISTS mediator.v_searchable_messages;

DROP INDEX IF EXISTS mediator.idx_embed_jobs_active_dedupe;
DROP INDEX IF EXISTS mediator.idx_embed_jobs_message_status;
DROP INDEX IF EXISTS mediator.idx_embed_jobs_claim;
DROP TABLE IF EXISTS mediator.embed_jobs;

DROP INDEX IF EXISTS mediator.idx_message_embeddings_embedded_at;
DROP INDEX IF EXISTS mediator.idx_message_embeddings_model_dimension;
DROP TABLE IF EXISTS mediator.message_embeddings;

DROP INDEX IF EXISTS mediator.idx_messages_searchable_scope_sent;
DROP INDEX IF EXISTS mediator.idx_messages_search_tsv;

ALTER TABLE mediator.messages
    DROP COLUMN IF EXISTS search_tsv,
    DROP COLUMN IF EXISTS search_suppressed_at;

DO $$
BEGIN
    IF to_regtype('vector') IS NOT NULL
       AND NOT EXISTS (
           SELECT 1
           FROM pg_attribute a
           JOIN pg_class c ON c.oid = a.attrelid
           WHERE a.atttypid = to_regtype('vector')
             AND a.attnum > 0
             AND NOT a.attisdropped
             AND c.relkind IN ('r','p','m')
       )
    THEN
        DROP EXTENSION IF EXISTS vector;
    END IF;
END $$;

COMMIT;
