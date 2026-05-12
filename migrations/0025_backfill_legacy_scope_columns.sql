-- 0025_backfill_legacy_scope_columns.sql
-- Backfill bot_id/topic_id (and dyad_id for bridge_candidates) on historical
-- rows that pre-date the S2a stamping work.  Uses COALESCE in SET so existing
-- non-NULL values are preserved (partial-stamp safety).  Idempotent: the WHERE
-- clause excludes rows that are already complete, so a second run is a no-op.
--
-- Must run BEFORE 0026 (CHECK ... NOT VALID) and 0027 (VALIDATE).

BEGIN;

-- Pre-capture the relationship topic id once.
DO $$
DECLARE
    rel_topic_id uuid;
    rel_dyad_id  uuid;
BEGIN
    SELECT id INTO STRICT rel_topic_id FROM mediator.topics WHERE slug = 'relationship';
    SELECT id INTO STRICT rel_dyad_id FROM mediator.dyads LIMIT 1;  -- only 1 dyad exists

    -- messages
    UPDATE mediator.messages
    SET bot_id   = COALESCE(bot_id, 'mediator'),
        topic_id = COALESCE(topic_id, rel_topic_id)
    WHERE bot_id IS NULL OR topic_id IS NULL;

    -- bot_turns
    UPDATE mediator.bot_turns
    SET bot_id   = COALESCE(bot_id, 'mediator'),
        topic_id = COALESCE(topic_id, rel_topic_id)
    WHERE bot_id IS NULL OR topic_id IS NULL;

    -- scheduled_jobs
    UPDATE mediator.scheduled_jobs
    SET bot_id   = COALESCE(bot_id, 'mediator'),
        topic_id = COALESCE(topic_id, rel_topic_id)
    WHERE bot_id IS NULL OR topic_id IS NULL;

    -- feedback
    UPDATE mediator.feedback
    SET bot_id   = COALESCE(bot_id, 'mediator'),
        topic_id = COALESCE(topic_id, rel_topic_id)
    WHERE bot_id IS NULL OR topic_id IS NULL;

    -- bridge_candidates (additionally backfill dyad_id)
    UPDATE mediator.bridge_candidates
    SET bot_id   = COALESCE(bot_id, 'mediator'),
        topic_id = COALESCE(topic_id, rel_topic_id),
        dyad_id  = COALESCE(dyad_id, rel_dyad_id)
    WHERE bot_id IS NULL OR topic_id IS NULL OR dyad_id IS NULL;
END $$;

COMMIT;