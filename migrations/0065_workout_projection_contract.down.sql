-- 0065_workout_projection_contract.down.sql: Revert the versioned
-- projection ledger back to single-version semantics.
--
-- WARNING: This will drop the partial unique index and the supersession
-- chain, and reinstate single-column UNIQUE on source_record_id.
-- If there are multiple versions per source record (versioned history),
-- this rollback will FAIL because the single-column UNIQUE cannot
-- accommodate more than one row per source_record_id.
-- Only revert after consolidating or deleting versioned rows.
--
-- Reversal ownership: projection-owned events (rows linked via
-- event_id in this table) are never mutated by the down migration.
-- Any detached event_ids remain in mediator.events and can be
-- manually audited.

BEGIN;

-- 1. Drop the supersession chain index.
DROP INDEX IF EXISTS mediator.idx_health_source_to_event_projections_supersedes;

-- 2. Drop the supersession column.
ALTER TABLE mediator.health_source_to_event_projections
    DROP COLUMN IF EXISTS supersedes_projection_id;

-- 3. Drop the decision fields.
ALTER TABLE mediator.health_source_to_event_projections
    DROP COLUMN IF EXISTS matched_local_date;

ALTER TABLE mediator.health_source_to_event_projections
    DROP COLUMN IF EXISTS decision_reason;

-- 4. Drop the partial unique index enforcing at-most-one-active.
DROP INDEX IF EXISTS mediator.idx_health_source_to_event_projections_active_source;

-- 5. Drop the composite UNIQUE constraint.
ALTER TABLE mediator.health_source_to_event_projections
    DROP CONSTRAINT IF EXISTS health_source_to_event_projections_source_version_key;

-- 6. Reinstate single-column UNIQUE on source_record_id.
ALTER TABLE mediator.health_source_to_event_projections
    ADD CONSTRAINT health_source_to_event_projections_source_record_id_key
        UNIQUE (source_record_id);

-- RLS policies are preserved (they were created in 0063 and not
-- modified by 0065).

COMMIT;
