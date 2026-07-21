-- 0064_health_measurement_fan_out: Replace single-column UNIQUE on
-- health_normalized_measurements.source_record_id with composite
-- UNIQUE (source_record_id, metric) so grouped Withings measure
-- responses can fan out into distinct normalized rows per metric.
--
-- Preserves:
--   * FK cascade to health_source_records
--   * Existing index idx_health_normalized_measurements_user_metric_measured
--   * FORCE RLS and deny-anon policy posture (not recreated here)
--   * Sleep uniqueness (one row per source record remains on
--     health_normalized_sleep via its own single-column UNIQUE)

BEGIN;

-- 1. Drop the existing single-column UNIQUE constraint.
--    The inline UNIQUE in 0063 auto-generated the name below.
ALTER TABLE mediator.health_normalized_measurements
    DROP CONSTRAINT IF EXISTS health_normalized_measurements_source_record_id_key;

-- 2. Add the composite UNIQUE so one source record can fan out into
--    multiple rows (one per metric) while still preventing exact duplicates.
ALTER TABLE mediator.health_normalized_measurements
    ADD CONSTRAINT health_normalized_measurements_source_metric_key
        UNIQUE (source_record_id, metric);

-- No RLS, index, or deny-anon changes needed — those are inherited from
-- 0063 and remain intact.

COMMIT;
