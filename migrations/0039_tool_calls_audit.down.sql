-- 0039_tool_calls_audit down: Drop the audit columns and index.
BEGIN;

DROP INDEX IF EXISTS mediator.idx_tool_calls_turn_kind;

ALTER TABLE mediator.tool_calls
  DROP COLUMN IF EXISTS summary,
  DROP COLUMN IF EXISTS kind;

COMMIT;
