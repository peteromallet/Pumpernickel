-- 0053_live_debrief_statuses down: Restore original CHECK constraint and drop
-- the partial debrief_failed index.

BEGIN;

-- ── Drop the partial debrief_failed index first ────────────────────────────
DROP INDEX IF EXISTS mediator.idx_conversations_status_debrief_failed;

-- ── Drop the expanded CHECK constraint ─────────────────────────────────────
ALTER TABLE mediator.conversations
    DROP CONSTRAINT IF EXISTS conversations_status_check;

-- ── Restore the original CHECK (without 'debriefing' / 'debrief_failed') ──
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
        'failed',
        'prep_failed'
    ));

COMMIT;
