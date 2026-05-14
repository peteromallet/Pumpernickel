BEGIN;

-- Cross-partner nudge primitive (SD-007, SD-012).
--
-- This migration adds ONLY a unique partial index on scheduled_jobs to
-- prevent two simultaneously pending partner_nudge rows from the same
-- originator to the same (user, bot). Nudge-specific state lives in the
-- existing scheduled_jobs.context jsonb — no new columns, no new tables.
--
-- Predicate matches the row shape inserted by schedule_partner_checkin:
--   job_type='scheduled_task', status='pending',
--   context->>'kind'='partner_nudge'
-- The code-side 24h rate limit in schedule_partner_checkin fires first
-- in normal operation; this index is a backstop against races and a
-- correctness guarantee at the DB level.

CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_jobs_one_pending_partner_nudge
    ON scheduled_jobs (user_id, bot_id, (context->>'originating_user_id'))
    WHERE status = 'pending'
      AND job_type = 'scheduled_task'
      AND context->>'kind' = 'partner_nudge';

COMMIT;
