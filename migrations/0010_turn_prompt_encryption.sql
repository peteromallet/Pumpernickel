BEGIN;

ALTER TABLE bot_turns
    ADD COLUMN IF NOT EXISTS prompt_snapshot_encrypted bytea;

COMMIT;
