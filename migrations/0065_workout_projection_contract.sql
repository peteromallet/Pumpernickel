-- 0065_workout_projection_contract: Upgrade health_source_to_event_projections
-- into a versioned projection ledger.
--
-- Changes:
--   1. Replace single-column UNIQUE on source_record_id with composite
--      UNIQUE (source_record_id, projection_version) so the ledger can
--      retain version history.
--   2. Add a partial UNIQUE index ensuring at most one active projection
--      (projection_status in ('pending', 'projected')) per source record.
--   3. Add decision fields: decision_reason TEXT and matched_local_date DATE
--      so every projection row carries a queryable rationale and the
--      matched commitment-slot local date.
--   4. Add optional supersession linkage:
--      supersedes_projection_id UUID self-referencing the prior projection
--      row, producing an auditable chain for revision/rematch/reversal.
--   5. Re-affirm FORCE RLS and deny-anon posture (inherited from 0063;
--      preserved by these ALTER-only operations).
--   6. Reversal ownership: only projection-owned events (those linked via
--      event_id) may be detached or deleted during supersession.
--      Manual log_event testimony is never mutated by projection code.
--
-- Preserves:
--   * All FK cascades (source_record_id, event_id, commitment_id)
--   * Existing indexes (idx_health_source_to_event_projections_event,
--     idx_health_source_to_event_projections_commitment)
--   * RLS policies (deny_anon + owner_scoped)
--   * CHECK constraints and NOT NULL defaults

BEGIN;

-- 1. Drop the existing single-column UNIQUE constraint.
--    Inline UNIQUE in 0063 generated the name below.
ALTER TABLE mediator.health_source_to_event_projections
    DROP CONSTRAINT IF EXISTS health_source_to_event_projections_source_record_id_key;

-- 2. Add composite UNIQUE so the ledger can keep version history.
ALTER TABLE mediator.health_source_to_event_projections
    ADD CONSTRAINT health_source_to_event_projections_source_version_key
        UNIQUE (source_record_id, projection_version);

-- 3. Partial unique index: at most one active projection per source record.
--    'pending' and 'projected' are the active states;
--    'superseded' and 'removed' are archival and may coexist.
CREATE UNIQUE INDEX idx_health_source_to_event_projections_active_source
    ON mediator.health_source_to_event_projections (source_record_id)
    WHERE projection_status IN ('pending', 'projected');

-- 4. Add decision fields so every projection row carries its rationale.
ALTER TABLE mediator.health_source_to_event_projections
    ADD COLUMN IF NOT EXISTS decision_reason text;

ALTER TABLE mediator.health_source_to_event_projections
    ADD COLUMN IF NOT EXISTS matched_local_date date;

-- 5. Optional supersession linkage for revision/rematch/reversal chains.
ALTER TABLE mediator.health_source_to_event_projections
    ADD COLUMN IF NOT EXISTS supersedes_projection_id uuid
        REFERENCES mediator.health_source_to_event_projections(id)
        ON DELETE SET NULL;

-- 6. Index the supersession chain for audit queries.
CREATE INDEX IF NOT EXISTS idx_health_source_to_event_projections_supersedes
    ON mediator.health_source_to_event_projections (supersedes_projection_id)
    WHERE supersedes_projection_id IS NOT NULL;

-- RLS and deny-anon posture are inherited from 0063 and preserved
-- by these ALTER-only operations. No policy recreation needed.

COMMIT;
