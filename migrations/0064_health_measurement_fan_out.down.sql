-- 0064_health_measurement_fan_out.down.sql: Revert composite UNIQUE
-- back to single-column UNIQUE on source_record_id.
--
-- WARNING: This will fail if there are rows with the same source_record_id
-- and different metrics (the fan-out case this migration enables). Only
-- revert if you have first consolidated or deleted fan-out rows.

BEGIN;

-- 1. Drop the composite UNIQUE constraint.
ALTER TABLE mediator.health_normalized_measurements
    DROP CONSTRAINT IF EXISTS health_normalized_measurements_source_metric_key;

-- 2. Reinstate the single-column UNIQUE.
ALTER TABLE mediator.health_normalized_measurements
    ADD CONSTRAINT health_normalized_measurements_source_record_id_key
        UNIQUE (source_record_id);

COMMIT;
