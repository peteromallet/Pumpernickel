-- 0042_live_conversations down: Drop the live-conversation tables in reverse
-- dependency order.  ON DELETE CASCADE on the child tables means dropping
-- conversations alone would also drop child rows, but we drop tables in
-- explicit FK-aware order for clarity and to handle the back-pointer FK on
-- conversations.current_item_id (which references conversation_items).

BEGIN;

-- Drop the back-pointer FK first so conversation_items can be dropped without
-- conversations holding a reference into it.
ALTER TABLE IF EXISTS mediator.conversations
    DROP CONSTRAINT IF EXISTS conversations_current_item_fk;

-- Children that reference conversations and/or conversation_items.
DROP TABLE IF EXISTS mediator.conversation_speakers;
DROP TABLE IF EXISTS mediator.conversation_consent_events;
DROP TABLE IF EXISTS mediator.item_visits;
DROP TABLE IF EXISTS mediator.conversation_notes;
DROP TABLE IF EXISTS mediator.transcript_turns;

-- conversation_items references itself (parent_item_id) and is referenced by
-- the (now-removed) back-pointer on conversations.
DROP TABLE IF EXISTS mediator.conversation_items;

-- Finally the session envelope.
DROP TABLE IF EXISTS mediator.conversations;

COMMIT;
