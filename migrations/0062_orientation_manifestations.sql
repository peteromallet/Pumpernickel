-- 0062_orientation_manifestations: Add manifestation as a Compass orientation kind.
--
-- Manifestations are date-bearing hoped-for moments. They reuse
-- user_orientation_items.target_date as the manifest-by date and stay inside
-- the existing User Orientation / Compass storage contract.

BEGIN;

ALTER TABLE mediator.user_orientation_items
    DROP CONSTRAINT IF EXISTS user_orientation_items_kind_check;

ALTER TABLE mediator.user_orientation_items
    ADD CONSTRAINT user_orientation_items_kind_check
    CHECK (kind IN ('principle', 'manifestation', 'goal', 'priority', 'anti_pattern'));

ALTER TABLE mediator.user_orientation_items
    DROP CONSTRAINT IF EXISTS user_orientation_items_manifestation_target_date_check;

ALTER TABLE mediator.user_orientation_items
    ADD CONSTRAINT user_orientation_items_manifestation_target_date_check
    CHECK (kind <> 'manifestation' OR target_date IS NOT NULL);

COMMIT;
