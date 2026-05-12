-- ============================================================================
-- 0028: Apply NOT NULL constraints on every column covered in 0026/0027
-- ============================================================================
-- Prerequisite: 0027 (VALIDATE CONSTRAINT) must have succeeded, proving no NULLs.
-- Pattern: ALTER TABLE ... ALTER COLUMN ... SET NOT NULL
-- Cheap operation: the validated CHECK already guarantees no NULL rows exist.
-- Tables: messages, bot_turns, scheduled_jobs, feedback, bridge_candidates
-- ============================================================================

-- messages: bot_id, topic_id
ALTER TABLE mediator.messages
    ALTER COLUMN bot_id SET NOT NULL;

ALTER TABLE mediator.messages
    ALTER COLUMN topic_id SET NOT NULL;

-- bot_turns: bot_id, topic_id
ALTER TABLE mediator.bot_turns
    ALTER COLUMN bot_id SET NOT NULL;

ALTER TABLE mediator.bot_turns
    ALTER COLUMN topic_id SET NOT NULL;

-- scheduled_jobs: bot_id, topic_id
ALTER TABLE mediator.scheduled_jobs
    ALTER COLUMN bot_id SET NOT NULL;

ALTER TABLE mediator.scheduled_jobs
    ALTER COLUMN topic_id SET NOT NULL;

-- feedback: bot_id, topic_id
ALTER TABLE mediator.feedback
    ALTER COLUMN bot_id SET NOT NULL;

ALTER TABLE mediator.feedback
    ALTER COLUMN topic_id SET NOT NULL;

-- bridge_candidates: bot_id, topic_id, dyad_id
ALTER TABLE mediator.bridge_candidates
    ALTER COLUMN bot_id SET NOT NULL;

ALTER TABLE mediator.bridge_candidates
    ALTER COLUMN topic_id SET NOT NULL;

ALTER TABLE mediator.bridge_candidates
    ALTER COLUMN dyad_id SET NOT NULL;