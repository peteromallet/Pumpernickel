-- 0063_health_provider_foundation.down.sql: Reverse secure provider foundation.
--
-- Drops all policies and health tables created by 0063 in FK-safe reverse order.
-- No other schema objects are modified.

BEGIN;

DROP POLICY IF EXISTS deny_anon_health_source_to_event_projections ON mediator.health_source_to_event_projections;
DROP POLICY IF EXISTS owner_scoped_health_source_to_event_projections ON mediator.health_source_to_event_projections;
DROP POLICY IF EXISTS deny_anon_health_normalized_sleep ON mediator.health_normalized_sleep;
DROP POLICY IF EXISTS owner_scoped_health_normalized_sleep ON mediator.health_normalized_sleep;
DROP POLICY IF EXISTS deny_anon_health_normalized_workouts ON mediator.health_normalized_workouts;
DROP POLICY IF EXISTS owner_scoped_health_normalized_workouts ON mediator.health_normalized_workouts;
DROP POLICY IF EXISTS deny_anon_health_normalized_measurements ON mediator.health_normalized_measurements;
DROP POLICY IF EXISTS owner_scoped_health_normalized_measurements ON mediator.health_normalized_measurements;
DROP POLICY IF EXISTS deny_anon_health_dirty_categories ON mediator.health_dirty_categories;
DROP POLICY IF EXISTS owner_scoped_health_dirty_categories ON mediator.health_dirty_categories;
DROP POLICY IF EXISTS deny_anon_health_webhook_receipts ON mediator.health_webhook_receipts;
DROP POLICY IF EXISTS owner_scoped_health_webhook_receipts ON mediator.health_webhook_receipts;
DROP POLICY IF EXISTS deny_anon_health_sync_runs ON mediator.health_sync_runs;
DROP POLICY IF EXISTS owner_scoped_health_sync_runs ON mediator.health_sync_runs;
DROP POLICY IF EXISTS deny_anon_health_source_records ON mediator.health_source_records;
DROP POLICY IF EXISTS owner_scoped_health_source_records ON mediator.health_source_records;
DROP POLICY IF EXISTS deny_anon_health_connections ON mediator.health_connections;
DROP POLICY IF EXISTS owner_scoped_health_connections ON mediator.health_connections;

DROP TABLE IF EXISTS mediator.health_source_to_event_projections;
DROP TABLE IF EXISTS mediator.health_normalized_sleep;
DROP TABLE IF EXISTS mediator.health_normalized_workouts;
DROP TABLE IF EXISTS mediator.health_normalized_measurements;
DROP TABLE IF EXISTS mediator.health_dirty_categories;
DROP TABLE IF EXISTS mediator.health_webhook_receipts;
DROP TABLE IF EXISTS mediator.health_sync_runs;
DROP TABLE IF EXISTS mediator.health_source_records;
DROP TABLE IF EXISTS mediator.health_connections;

COMMIT;
