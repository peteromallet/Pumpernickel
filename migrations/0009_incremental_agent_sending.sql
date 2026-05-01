BEGIN;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS bot_turn_id uuid REFERENCES bot_turns(id),
    ADD COLUMN IF NOT EXISTS outbound_part_key text,
    ADD COLUMN IF NOT EXISTS outbound_part_index integer CHECK (outbound_part_index IS NULL OR outbound_part_index >= 1);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_outbound_part_key
    ON messages (outbound_part_key);

CREATE INDEX IF NOT EXISTS idx_messages_bot_turn_parts
    ON messages (bot_turn_id, outbound_part_index)
    WHERE direction = 'outbound' AND outbound_part_index IS NOT NULL;

COMMIT;
