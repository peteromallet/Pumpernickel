-- 0041_inbound_queue_handling: Durable inbound queue hardening.
--
-- Adds handling metadata columns to messages, expands processing_state to
-- include explicit queue states (processing, failed), adds a handling_result
-- CHECK, partial indexes for sweeper queries, and a processing_attempts counter.
--
-- Conservative backfill: existing rows keep null metadata.  Only the CHECK
-- constraint is widened to accept new states.
--
-- Design decisions: see SD-001 through SD-007 in the durable-inbound-queue brief.

BEGIN;

-- 1. Add handling metadata columns -------------------------------------------
ALTER TABLE mediator.messages
    ADD COLUMN IF NOT EXISTS handled_at           timestamptz,
    ADD COLUMN IF NOT EXISTS handled_by_turn_id   uuid REFERENCES mediator.bot_turns(id),
    ADD COLUMN IF NOT EXISTS handling_result      text,
    ADD COLUMN IF NOT EXISTS processing_started_at timestamptz,
    ADD COLUMN IF NOT EXISTS processing_error     text,
    ADD COLUMN IF NOT EXISTS processing_attempts  integer NOT NULL DEFAULT 0;

-- 2. Expand processing_state CHECK constraint --------------------------------
-- Drop the old constraint (created in 0005_plan6_ops.sql, possibly re-created
-- by later migrations) and add the replacement with explicit queue states.
-- DO NOT edit 0005_plan6_ops.sql.
ALTER TABLE mediator.messages
    DROP CONSTRAINT IF EXISTS messages_processing_state_check;

ALTER TABLE mediator.messages
    ADD CONSTRAINT messages_processing_state_check
    CHECK (processing_state IN (
        'raw',        -- stored, not yet claimed
        'deferred',   -- intentionally waiting for coalescing or pacing
        'processing', -- claimed by a worker/turn
        'processed',  -- successfully handled by a completed turn
        'expired',    -- intentionally no longer needs direct handling
        'failed',     -- attempted and failed; retryable or inspectable
        'withheld'    -- legacy (from 0005); retained for existing rows
    ));

-- 3. Add handling_result CHECK -----------------------------------------------
-- Only validated when handling_result IS NOT NULL (existing rows stay null).
ALTER TABLE mediator.messages
    ADD CONSTRAINT messages_handling_result_check
    CHECK (handling_result IS NULL OR handling_result IN (
        'replied',
        'silent',
        'withheld_newer_inbound',
        'no_action',
        'expired',
        'failed'
    ));

-- 4. Partial indexes for sweeper queries -------------------------------------
-- Sweeper finds inbound messages in raw/processing/failed states that are
-- within scope for recovery/retry.  direction='inbound' is part of the WHERE
-- clause so outbound rows are never picked up by these indexes.

-- Index for raw-message recovery (ordered by sent_at so sweeper can skip
-- very-recent messages within a grace window).
CREATE INDEX IF NOT EXISTS idx_messages_inbound_raw_sweeper
    ON mediator.messages (bot_id, topic_id, sent_at)
    WHERE direction = 'inbound' AND processing_state = 'raw';

-- Index for stale-processing recovery (ordered by processing_started_at).
CREATE INDEX IF NOT EXISTS idx_messages_inbound_processing_sweeper
    ON mediator.messages (bot_id, topic_id, processing_started_at)
    WHERE direction = 'inbound' AND processing_state = 'processing';

-- Index for retryable-failed recovery.
CREATE INDEX IF NOT EXISTS idx_messages_inbound_failed_sweeper
    ON mediator.messages (bot_id, topic_id, sent_at)
    WHERE direction = 'inbound' AND processing_state = 'failed';

-- 5. Conservative backfill ---------------------------------------------------
-- Existing rows keep null metadata.  No UPDATE is issued — existing rows with
-- processing_state='processed' or 'withheld' remain as-is and the new metadata
-- columns stay null.  The sweeper only acts on rows in raw/processing/failed
-- states, so already-terminal rows are undisturbed.

COMMIT;
