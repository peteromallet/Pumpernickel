-- 0049_messages_bot_turn_id.down.sql
-- Reverse the 0049 migration: drop trigger, indexes, FK, column.

BEGIN;

-- Drop trigger
DROP TRIGGER IF EXISTS tg_messages_one_inflight_owner ON mediator.messages;
DROP FUNCTION IF EXISTS mediator.tg_messages_one_inflight_owner();

-- Drop indexes
DROP INDEX IF EXISTS mediator.idx_messages_inflight_owner;
DROP INDEX IF EXISTS mediator.idx_messages_bot_turn_id;

-- Drop FK constraint
ALTER TABLE mediator.messages
DROP CONSTRAINT IF EXISTS messages_bot_turn_id_fkey;

-- Drop column
ALTER TABLE mediator.messages
DROP COLUMN IF EXISTS bot_turn_id;

COMMIT;
