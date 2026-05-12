-- ============================================================================
-- 0026: ADD CHECK ... NOT VALID constraints for NOT NULL columns
-- ============================================================================
-- Pattern: ADD CONSTRAINT ... CHECK (col IS NOT NULL) NOT VALID
-- NOT VALID = non-blocking, no full table scan during creation.
-- Tables: messages, bot_turns, scheduled_jobs, feedback, bridge_candidates
-- ============================================================================

-- messages: bot_id, topic_id
ALTER TABLE mediator.messages
    ADD CONSTRAINT messages_bot_id_not_null_check CHECK (bot_id IS NOT NULL) NOT VALID;

ALTER TABLE mediator.messages
    ADD CONSTRAINT messages_topic_id_not_null_check CHECK (topic_id IS NOT NULL) NOT VALID;

-- bot_turns: bot_id, topic_id
ALTER TABLE mediator.bot_turns
    ADD CONSTRAINT bot_turns_bot_id_not_null_check CHECK (bot_id IS NOT NULL) NOT VALID;

ALTER TABLE mediator.bot_turns
    ADD CONSTRAINT bot_turns_topic_id_not_null_check CHECK (topic_id IS NOT NULL) NOT VALID;

-- scheduled_jobs: bot_id, topic_id
ALTER TABLE mediator.scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_bot_id_not_null_check CHECK (bot_id IS NOT NULL) NOT VALID;

ALTER TABLE mediator.scheduled_jobs
    ADD CONSTRAINT scheduled_jobs_topic_id_not_null_check CHECK (topic_id IS NOT NULL) NOT VALID;

-- feedback: bot_id, topic_id
ALTER TABLE mediator.feedback
    ADD CONSTRAINT feedback_bot_id_not_null_check CHECK (bot_id IS NOT NULL) NOT VALID;

ALTER TABLE mediator.feedback
    ADD CONSTRAINT feedback_topic_id_not_null_check CHECK (topic_id IS NOT NULL) NOT VALID;

-- bridge_candidates: bot_id, topic_id, dyad_id
ALTER TABLE mediator.bridge_candidates
    ADD CONSTRAINT bridge_candidates_bot_id_not_null_check CHECK (bot_id IS NOT NULL) NOT VALID;

ALTER TABLE mediator.bridge_candidates
    ADD CONSTRAINT bridge_candidates_topic_id_not_null_check CHECK (topic_id IS NOT NULL) NOT VALID;

ALTER TABLE mediator.bridge_candidates
    ADD CONSTRAINT bridge_candidates_dyad_id_not_null_check CHECK (dyad_id IS NOT NULL) NOT VALID;