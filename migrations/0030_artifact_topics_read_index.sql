-- Migration 0030: Partial index for artifact_topics read cutover (Sprint 3)
-- Supports the JOIN artifact_topics pattern used by every artifact read.
-- The partial index on (topic_id, artifact_table) WHERE status='active'
-- covers the join condition: at.topic_id = $N AND at.status = 'active'.

CREATE INDEX IF NOT EXISTS idx_artifact_topics_topic_artifact_active
    ON artifact_topics (topic_id, artifact_table)
    WHERE status = 'active';