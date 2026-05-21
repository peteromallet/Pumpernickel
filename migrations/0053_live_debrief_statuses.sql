-- 0053_live_debrief_statuses: Add 'debriefing' and 'debrief_failed' to
-- mediator.conversations status CHECK.
--
-- Drops and re-adds the inline CHECK constraint on conversations.status with
-- 'debriefing' and 'debrief_failed' added to the IN list.  Also creates a
-- dedicated partial index for quick lookup of debrief_failed sessions (used
-- by retry endpoints and orphan-recovery sweeps).
--
-- The idx_conversations_status_active partial index is left unchanged:
-- 'debriefing' and 'debrief_failed' are intentionally excluded because
-- sessions in these statuses are not considered "active".
--
-- Likewise, idx_conversations_spend_active is not extended — per-session
-- spend attribution for debrief is deferred to Sprint 4.

BEGIN;

-- ── Drop the existing inline CHECK on conversations.status ────────────────
ALTER TABLE mediator.conversations
    DROP CONSTRAINT IF EXISTS conversations_status_check;

-- ── Re-add the CHECK with 'debriefing' and 'debrief_failed' added ─────────
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
        'prep_failed',
        'debriefing',
        'debrief_failed'
    ));

-- ── Partial index for debrief_failed sessions (retry / orphan-recovery) ───
CREATE INDEX IF NOT EXISTS idx_conversations_status_debrief_failed
    ON mediator.conversations (status, created_at)
    WHERE status = 'debrief_failed';

COMMIT;
