-- 0060_user_orientation.down.sql — Reverse migration.
--
-- Drops everything created by the forward migration in correct FK-safe
-- dependency order:
--   1. RLS policies on all three tables
--   2. user_orientation_item_reviews (child — references items)
--   3. user_orientation_item_links    (child — references items)
--   4. user_orientation_items         (parent; self-FK supersedes_item_id)
--
-- No indexes need explicit DROP: Postgres drops a table's indexes with the
-- table.  No other migration's objects are touched — this down migration does
-- NOT drop or alter compass_*, conversation_artifacts, or any commitments
-- column (the forward migration added none of those).
--
-- Every DROP uses IF EXISTS so re-applying the down migration is safe even
-- when some objects have already been removed.

BEGIN;

-- ===========================================================================
-- 1. Drop RLS policies on all three tables
-- ===========================================================================

DROP POLICY IF EXISTS deny_anon_user_orientation_items ON mediator.user_orientation_items;
DROP POLICY IF EXISTS owner_scoped_user_orientation_items ON mediator.user_orientation_items;
DROP POLICY IF EXISTS deny_anon_user_orientation_item_links ON mediator.user_orientation_item_links;
DROP POLICY IF EXISTS owner_scoped_user_orientation_item_links ON mediator.user_orientation_item_links;
DROP POLICY IF EXISTS deny_anon_user_orientation_item_reviews ON mediator.user_orientation_item_reviews;
DROP POLICY IF EXISTS owner_scoped_user_orientation_item_reviews ON mediator.user_orientation_item_reviews;

-- ===========================================================================
-- 2. Drop tables (children first, then parent)
-- ===========================================================================

DROP TABLE IF EXISTS mediator.user_orientation_item_reviews;
DROP TABLE IF EXISTS mediator.user_orientation_item_links;
DROP TABLE IF EXISTS mediator.user_orientation_items;

COMMIT;
