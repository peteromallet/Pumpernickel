-- 0051_conversation_artifacts.down.sql — Reverse migration.
--
-- Drops everything created by the forward migration in the correct
-- dependency order:
--   1. RLS policies on both new tables
--   2. artifact_links table (child — references conversation_artifacts)
--   3. conversation_artifacts table (parent)
--   4. Partial indexes on bot_turns new columns
--   5. bot_turns columns (kind, then conversation_id)
--
-- Every DROP uses IF EXISTS so re-applying the down migration is safe
-- even when some objects have already been removed.

BEGIN;

-- ===========================================================================
-- 1. Drop RLS policies on both new tables
-- ===========================================================================

DROP POLICY IF EXISTS deny_anon_conversation_artifacts ON mediator.conversation_artifacts;
DROP POLICY IF EXISTS owner_scoped_conversation_artifacts ON mediator.conversation_artifacts;
DROP POLICY IF EXISTS deny_anon_artifact_links ON mediator.artifact_links;
DROP POLICY IF EXISTS owner_scoped_artifact_links ON mediator.artifact_links;

-- ===========================================================================
-- 2. Drop tables (child first, then parent)
-- ===========================================================================

DROP TABLE IF EXISTS mediator.artifact_links;
DROP TABLE IF EXISTS mediator.conversation_artifacts;

-- ===========================================================================
-- 3. Drop partial indexes on bot_turns
-- ===========================================================================

DROP INDEX IF EXISTS mediator.idx_bot_turns_conversation_id;
DROP INDEX IF EXISTS mediator.idx_bot_turns_kind;

-- ===========================================================================
-- 4. Drop bot_turns columns
-- ===========================================================================

ALTER TABLE mediator.bot_turns DROP COLUMN IF EXISTS kind;
ALTER TABLE mediator.bot_turns DROP COLUMN IF EXISTS conversation_id;

COMMIT;
