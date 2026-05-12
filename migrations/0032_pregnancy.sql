-- ============================================================
-- Sprint: Tante Rosi — pregnancy coach bot
-- Migration 0032: Add pregnancy tracking columns to users
-- ============================================================
-- All columns are nullable; no existing rows are affected.
-- Two CHECK constraints enforce valid partial-state combinations:
--   1. pregnancy_dating_basis_requires_edd:
--        EDD and dating_basis must be both NULL or both non-NULL
--   2. pregnancy_outcome_requires_ended_at:
--        outcome and ended_at must be both NULL or both non-NULL
-- Partial index supports efficient "find users with active pregnancy" queries.
-- No NOT VALID — constraints validate immediately (users table is small).
-- ============================================================

BEGIN;

ALTER TABLE mediator.users
    ADD COLUMN pregnancy_edd date,
    ADD COLUMN pregnancy_dating_basis text
        CHECK (pregnancy_dating_basis IS NULL OR pregnancy_dating_basis IN ('lmp', 'scan')),
    ADD COLUMN pregnancy_lmp_date date,
    ADD COLUMN pregnancy_scan_date date,
    ADD COLUMN pregnancy_scan_corrected_at timestamptz,
    ADD COLUMN pregnancy_started_at timestamptz,
    ADD COLUMN pregnancy_ended_at timestamptz,
    ADD COLUMN pregnancy_outcome text
        CHECK (pregnancy_outcome IS NULL OR pregnancy_outcome IN ('birth', 'loss', 'termination'));

-- Constraint: EDD and dating_basis must be both NULL or both non-NULL
ALTER TABLE mediator.users
    ADD CONSTRAINT pregnancy_dating_basis_requires_edd
        CHECK ((pregnancy_edd IS NULL AND pregnancy_dating_basis IS NULL)
            OR (pregnancy_edd IS NOT NULL AND pregnancy_dating_basis IS NOT NULL));

-- Constraint: outcome and ended_at must be both NULL or both non-NULL
ALTER TABLE mediator.users
    ADD CONSTRAINT pregnancy_outcome_requires_ended_at
        CHECK ((pregnancy_outcome IS NULL AND pregnancy_ended_at IS NULL)
            OR (pregnancy_outcome IS NOT NULL AND pregnancy_ended_at IS NOT NULL));

-- Partial index for "find users with an active pregnancy" queries
CREATE INDEX idx_users_active_pregnancy
    ON mediator.users (id)
    WHERE pregnancy_edd IS NOT NULL AND pregnancy_ended_at IS NULL;

COMMIT;