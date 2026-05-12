-- ============================================================
-- Rollback for 0032_pregnancy.sql
-- Drops the partial index, both CHECK constraints, and all 8 pregnancy columns.
-- ============================================================

BEGIN;

DROP INDEX IF EXISTS mediator.idx_users_active_pregnancy;

ALTER TABLE mediator.users
    DROP CONSTRAINT IF EXISTS pregnancy_outcome_requires_ended_at;

ALTER TABLE mediator.users
    DROP CONSTRAINT IF EXISTS pregnancy_dating_basis_requires_edd;

ALTER TABLE mediator.users
    DROP COLUMN IF EXISTS pregnancy_outcome,
    DROP COLUMN IF EXISTS pregnancy_ended_at,
    DROP COLUMN IF EXISTS pregnancy_started_at,
    DROP COLUMN IF EXISTS pregnancy_scan_corrected_at,
    DROP COLUMN IF EXISTS pregnancy_scan_date,
    DROP COLUMN IF EXISTS pregnancy_lmp_date,
    DROP COLUMN IF EXISTS pregnancy_dating_basis,
    DROP COLUMN IF EXISTS pregnancy_edd;

COMMIT;