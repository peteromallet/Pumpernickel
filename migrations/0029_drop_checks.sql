-- ============================================================================
-- 0029: Drop CHECK constraints — NOT NULL subsumes them
-- ============================================================================
-- Prerequisite: 0028 (ALTER COLUMN ... SET NOT NULL) must have succeeded.
-- Pattern: ALTER TABLE ... DROP CONSTRAINT <name>
-- Once NOT NULL is set, the CHECK constraint is redundant.
-- Tables: messages, bot_turns, scheduled_jobs, feedback, bridge_candidates
-- ============================================================================

-- messages: bot_id, topic_id
ALTER TABLE mediator.messages
    DROP CONSTRAINT messages_bot_id_not_null_check;

ALTER TABLE mediator.messages
    DROP CONSTRAINT messages_topic_id_not_null_check;

-- bot_turns: bot_id, topic_id
ALTER TABLE mediator.bot_turns
    DROP CONSTRAINT bot_turns_bot_id_not_null_check;

ALTER TABLE mediator.bot_turns
    DROP CONSTRAINT bot_turns_topic_id_not_null_check;

-- scheduled_jobs: bot_id, topic_id
ALTER TABLE mediator.scheduled_jobs
    DROP CONSTRAINT scheduled_jobs_bot_id_not_null_check;

ALTER TABLE mediator.scheduled_jobs
    DROP CONSTRAINT scheduled_jobs_topic_id_not_null_check;

-- feedback: bot_id, topic_id
ALTER TABLE mediator.feedback
    DROP CONSTRAINT feedback_bot_id_not_null_check;

ALTER TABLE mediator.feedback
    DROP CONSTRAINT feedback_topic_id_not_null_check;

-- bridge_candidates: bot_id, topic_id, dyad_id
ALTER TABLE mediator.bridge_candidates
    DROP CONSTRAINT bridge_candidates_bot_id_not_null_check;

ALTER TABLE mediator.bridge_candidates
    DROP CONSTRAINT bridge_candidates_topic_id_not_null_check;

ALTER TABLE mediator.bridge_candidates
    DROP CONSTRAINT bridge_candidates_dyad_id_not_null_check;