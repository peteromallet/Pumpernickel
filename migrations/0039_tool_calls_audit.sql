-- 0039_tool_calls_audit: Add kind + summary columns to tool_calls so the
-- agent can audit its own past decisions (read vs. write tools, with a
-- short human-readable summary line per call).
BEGIN;

ALTER TABLE mediator.tool_calls
  ADD COLUMN kind text NOT NULL DEFAULT 'write'
    CHECK (kind IN ('read', 'write')),
  ADD COLUMN summary text;

-- Existing rows are all writes (only write_tools.py logged before this).
-- The default above covers them; nothing else to backfill.

CREATE INDEX idx_tool_calls_turn_kind
  ON mediator.tool_calls (turn_id, kind);

COMMIT;
