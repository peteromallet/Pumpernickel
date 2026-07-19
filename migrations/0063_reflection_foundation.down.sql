-- 0063_reflection_foundation.down.sql — Reverse migration.
--
-- Drops everything created by the forward migration in correct FK-safe
-- dependency order:
--   1. RLS policies on all three tables
--   2. reflection_derivations  (child — references entries)
--   3. reflection_entries      (child — references sessions; self-FK supersedes)
--   4. reflection_sessions     (parent)
--
-- No indexes need explicit DROP: Postgres drops a table's indexes with the
-- table.  No other migration's objects are touched — this down migration does
-- NOT drop or alter any messages, memories, observations, commitments, or
-- other existing tables (the forward migration added none of those).
--
-- Every DROP uses IF EXISTS so re-applying the down migration is safe even
-- when some objects have already been removed.

BEGIN;

-- ===========================================================================
-- 1. Drop RLS policies on all three tables
-- ===========================================================================

DROP POLICY IF EXISTS deny_anon_reflection_derivations ON mediator.reflection_derivations;
DROP POLICY IF EXISTS owner_scoped_reflection_derivations ON mediator.reflection_derivations;
DROP POLICY IF EXISTS deny_anon_reflection_entries ON mediator.reflection_entries;
DROP POLICY IF EXISTS owner_scoped_reflection_entries ON mediator.reflection_entries;
DROP POLICY IF EXISTS deny_anon_reflection_sessions ON mediator.reflection_sessions;
DROP POLICY IF EXISTS owner_scoped_reflection_sessions ON mediator.reflection_sessions;

-- ===========================================================================
-- 2. Drop tables (children first, then parent)
-- ===========================================================================

DROP TABLE IF EXISTS mediator.reflection_derivations;
DROP TABLE IF EXISTS mediator.reflection_entries;
DROP TABLE IF EXISTS mediator.reflection_sessions;

COMMIT;
