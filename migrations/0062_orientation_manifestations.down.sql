-- 0062_orientation_manifestations.down.sql — Reverse migration.
--
-- This assumes no remaining user_orientation_items rows have
-- kind='manifestation'. Delete or migrate those rows before rollback.

BEGIN;

ALTER TABLE mediator.user_orientation_items
    DROP CONSTRAINT IF EXISTS user_orientation_items_manifestation_target_date_check;

ALTER TABLE mediator.user_orientation_items
    DROP CONSTRAINT IF EXISTS user_orientation_items_kind_check;

ALTER TABLE mediator.user_orientation_items
    ADD CONSTRAINT user_orientation_items_kind_check
    CHECK (kind IN ('principle', 'goal', 'priority', 'anti_pattern'));

COMMIT;
