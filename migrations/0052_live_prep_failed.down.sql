-- 0052_live_prep_failed down: Restore original CHECK constraint and drop the
-- partial prep_failed index.

BEGIN;

-- ── Drop the partial prep_failed index first ───────────────────────────────
DROP INDEX IF EXISTS mediator.idx_conversations_status_prep_failed;

-- ── Drop the expanded CHECK constraint ─────────────────────────────────────
ALTER TABLE mediator.conversations
    DROP CONSTRAINT IF EXISTS conversations_status_check;

-- ── Restore the original CHECK (without 'prep_failed') ─────────────────────
ALTER TABLE mediator.conversations
    ADD CONSTRAINT conversations_status_check
    CHECK (status IN (
        'prepping',
        'ready',
        'live',
        'ended',
        'synthesizing',
        'review_pending',
        'synthesized',
        'discarded',
        'failed'
    ));

COMMIT;
