-- 0056_xen_retriever (down): Reverse every object created by 0056_xen_retriever.sql.

BEGIN;

DROP VIEW IF EXISTS v_searchable_messages;

DROP INDEX IF EXISTS embed_jobs_message_id;
DROP INDEX IF EXISTS embed_jobs_status_priority_scheduled_at;
DROP TABLE IF EXISTS embed_jobs;

DROP TABLE IF EXISTS message_embeddings;

DROP INDEX IF EXISTS messages_search_tsv_gin;
ALTER TABLE messages DROP COLUMN IF EXISTS search_tsv;
ALTER TABLE messages DROP COLUMN IF EXISTS search_suppressed_at;

DROP EXTENSION IF EXISTS vector;

COMMIT;
