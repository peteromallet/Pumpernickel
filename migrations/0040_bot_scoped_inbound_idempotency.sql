-- 0040_bot_scoped_inbound_idempotency: Replace the global UNIQUE on
-- messages.whatsapp_message_id with a bot-scoped UNIQUE (bot_id, whatsapp_message_id).
-- This allows the same Discord/WhatsApp message id to be stored for different bots
-- (e.g., mediator and hector), while still preventing duplicate rows for the same bot.
--
-- The old constraint was created in 0001_init.sql line 30 as an inline UNIQUE,
-- which PostgreSQL implements as an implicit unique index named
-- messages_whatsapp_message_id_key.
--
-- bot_id is guaranteed NOT NULL per migration 0028, so the new composite
-- UNIQUE constraint is safe.
BEGIN;

-- Drop the old global unique constraint (inline UNIQUE on whatsapp_message_id
-- from 0001_init.sql line 30). PostgreSQL auto-names the constraint
-- messages_whatsapp_message_id_key for an inline UNIQUE on this column.
-- Using ALTER TABLE ... DROP CONSTRAINT is the correct DDL operation for
-- UNIQUE constraints (DROP INDEX on a constraint-owned index requires CASCADE
-- and works in some PG versions but is not the canonical approach).
ALTER TABLE mediator.messages
    DROP CONSTRAINT IF EXISTS messages_whatsapp_message_id_key;

-- Add the new bot-scoped unique constraint.
-- ON CONFLICT in application code must target (bot_id, whatsapp_message_id)
-- to match this constraint.
ALTER TABLE mediator.messages
    ADD CONSTRAINT messages_bot_whatsapp_unique
    UNIQUE (bot_id, whatsapp_message_id);

COMMIT;
