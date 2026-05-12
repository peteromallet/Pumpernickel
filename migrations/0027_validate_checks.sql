-- ============================================================================
-- 0027: VALIDATE CONSTRAINT for every CHECK ... NOT VALID added in 0026
-- ============================================================================
-- Pattern: ALTER TABLE ... VALIDATE CONSTRAINT <name>
-- Validates that every row passes the CHECK; acquires weak lock, does full scan.
-- If any NULL rows remain after 0025 backfill, validation will fail — STOP.
-- ============================================================================

-- messages: bot_id, topic_id
ALTER TABLE mediator.messages
    VALIDATE CONSTRAINT messages_bot_id_not_null_check;

ALTER TABLE mediator.messages
    VALIDATE CONSTRAINT messages_topic_id_not_null_check;

-- bot_turns: bot_id, topic_id
ALTER TABLE mediator.bot_turns
    VALIDATE CONSTRAINT bot_turns_bot_id_not_null_check;

ALTER TABLE mediator.bot_turns
    VALIDATE CONSTRAINT bot_turns_topic_id_not_null_check;

-- scheduled_jobs: bot_id, topic_id
ALTER TABLE mediator.scheduled_jobs
    VALIDATE CONSTRAINT scheduled_jobs_bot_id_not_null_check;

ALTER TABLE mediator.scheduled_jobs
    VALIDATE CONSTRAINT scheduled_jobs_topic_id_not_null_check;

-- feedback: bot_id, topic_id
ALTER TABLE mediator.feedback
    VALIDATE CONSTRAINT feedback_bot_id_not_null_check;

ALTER TABLE mediator.feedback
    VALIDATE CONSTRAINT feedback_topic_id_not_null_check;

-- bridge_candidates: bot_id, topic_id, dyad_id
ALTER TABLE mediator.bridge_candidates
    VALIDATE CONSTRAINT bridge_candidates_bot_id_not_null_check;

ALTER TABLE mediator.bridge_candidates
    VALIDATE CONSTRAINT bridge_candidates_topic_id_not_null_check;

ALTER TABLE mediator.bridge_candidates
    VALIDATE CONSTRAINT bridge_candidates_dyad_id_not_null_check;