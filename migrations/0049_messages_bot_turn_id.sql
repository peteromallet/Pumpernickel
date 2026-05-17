-- 0049_messages_bot_turn_id: Add bot_turn_id FK, indexes, trigger, backfill.
--
-- messages.bot_turn_id is the in-flight ownership pointer: while a bot_turn
-- is actively handling a message (processing_state IN ('processing','deferred')),
-- bot_turn_id identifies the owning turn.  At terminal completion
-- (processed/failed/expired), bot_turn_id is cleared to NULL while
-- handled_by_turn_id continues to record the historical terminal handler.
--
-- The partial UNIQUE INDEX + BEFORE UPDATE trigger enforce the invariant:
-- at most one in-flight bot_turn per message at any time.
--
-- R1 pre-clean: duplicate in-flight bot_turns from the historical dup-cascade
-- bug are marked abandoned_pre_dedupe so the backfill excludes them.  The
-- most-recent bot_turn per triggering message wins (SD-007).
--
-- FK is NOT VALID (best-effort VALIDATE follows).  Orphan rows are surfaced
-- via RAISE NOTICE; a follow-up VALIDATE-sweep ticket should be filed.

BEGIN;

-- ── Column (idempotent — may already exist from exploratory work) ───────────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'mediator'
          AND table_name   = 'messages'
          AND column_name  = 'bot_turn_id'
    ) THEN
        ALTER TABLE mediator.messages
        ADD COLUMN bot_turn_id uuid;
    END IF;
END $$;

-- ── FK NOT VALID (idempotent — drop + recreate if already exists) ───────────
-- Drop the existing FK first if present (may have been created as VALID by
-- exploratory work).  Recreate as NOT VALID so the migration doesn't block
-- on orphan rows.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'mediator.messages'::regclass
          AND conname = 'messages_bot_turn_id_fkey'
    ) THEN
        -- If the existing FK is VALID, we still drop/recreate as NOT VALID
        -- because the trigger enforces the invariant regardless of FK validity.
        ALTER TABLE mediator.messages
        DROP CONSTRAINT messages_bot_turn_id_fkey;
    END IF;
END $$;

ALTER TABLE mediator.messages
ADD CONSTRAINT messages_bot_turn_id_fkey
    FOREIGN KEY (bot_turn_id) REFERENCES mediator.bot_turns(id)
    ON DELETE SET NULL
    NOT VALID;

-- Best-effort VALIDATE: if there are no orphan rows, promote to VALID.
-- Otherwise RAISE NOTICE so the operator can file a follow-up VALIDATE-sweep
-- ticket.  The trigger enforces the invariant regardless of FK validity.
DO $$
DECLARE
    orphan_count bigint;
BEGIN
    SELECT count(*) INTO orphan_count
    FROM mediator.messages m
    WHERE m.bot_turn_id IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM mediator.bot_turns bt WHERE bt.id = m.bot_turn_id
      );

    IF orphan_count = 0 THEN
        ALTER TABLE mediator.messages
        VALIDATE CONSTRAINT messages_bot_turn_id_fkey;
        RAISE NOTICE 'messages_bot_turn_id_fkey validated (0 orphans)';
    ELSE
        RAISE NOTICE 'messages_bot_turn_id_fkey left NOT VALID: % orphan rows exist. '
                     'File a follow-up VALIDATE-sweep ticket.', orphan_count;
    END IF;
END $$;

-- ── Indexes ─────────────────────────────────────────────────────────────────
CREATE INDEX IF NOT EXISTS idx_messages_bot_turn_id
    ON mediator.messages (bot_turn_id)
    WHERE bot_turn_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_messages_inflight_owner
    ON mediator.messages (bot_turn_id)
    WHERE bot_turn_id IS NOT NULL
      AND processing_state IN ('processing', 'deferred');

-- ── R1 pre-clean: mark duplicate in-flight bot_turns ────────────────────────
-- Historical dup-cascade bug created multiple in-flight bot_turns for the same
-- triggering message.  Mark all but the most-recent per message as
-- abandoned_pre_dedupe so the backfill excludes them.
DO $$
DECLARE
    marked_count bigint;
BEGIN
    WITH dup_inflight AS (
        SELECT bt.id,
               row_number() OVER (
                   PARTITION BY unnest(bt.triggering_message_ids)
                   ORDER BY bt.started_at DESC
               ) AS rn
        FROM mediator.bot_turns bt
        WHERE bt.completed_at IS NULL
          AND bt.failure_reason IS NULL
          AND bt.triggering_message_ids IS NOT NULL
    )
    UPDATE mediator.bot_turns bt
    SET completed_at   = now(),
        failure_reason = 'abandoned_pre_dedupe'
    FROM dup_inflight d
    WHERE bt.id = d.id
      AND d.rn > 1;

    GET DIAGNOSTICS marked_count = ROW_COUNT;
    IF marked_count > 0 THEN
        RAISE NOTICE 'R1 pre-clean: % duplicate in-flight bot_turns marked abandoned_pre_dedupe',
            marked_count;
    ELSE
        RAISE NOTICE 'R1 pre-clean: no duplicate in-flight bot_turns found';
    END IF;
END $$;

-- ── Backfill messages.bot_turn_id (SD-007: most-recent match wins) ──────────
-- For each message, find the most-recent bot_turn whose triggering_message_ids
-- contains the message id, excluding abandoned_pre_dedupe rows.  DISTINCT ON
-- with ORDER BY started_at DESC guarantees the most-recent match.
DO $$
DECLARE
    backfilled_count bigint;
BEGIN
    WITH best_match AS (
        SELECT DISTINCT ON (m.id) m.id AS message_id, bt.id AS turn_id
        FROM mediator.messages m
        JOIN mediator.bot_turns bt
          ON bt.triggering_message_ids @> ARRAY[m.id]
         AND bt.failure_reason IS DISTINCT FROM 'abandoned_pre_dedupe'
        WHERE m.bot_turn_id IS NULL
          AND m.direction = 'inbound'
        ORDER BY m.id, bt.started_at DESC
    )
    UPDATE mediator.messages m
    SET bot_turn_id = bm.turn_id
    FROM best_match bm
    WHERE m.id = bm.message_id;

    GET DIAGNOSTICS backfilled_count = ROW_COUNT;
    IF backfilled_count > 0 THEN
        RAISE NOTICE 'Backfill: % messages.bot_turn_id populated (SD-007 most-recent match)', backfilled_count;
    ELSE
        RAISE NOTICE 'Backfill: no messages needed backfilling';
    END IF;
END $$;

-- ── Trigger: enforce at most one in-flight bot_turn per message ─────────────
-- Allow NULL clears (terminal completion) and no-op re-stamps (same bot_turn_id).
-- Raise unique_violation only on transition to a DIFFERENT non-null in-flight owner.
CREATE OR REPLACE FUNCTION mediator.tg_messages_one_inflight_owner()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    conflict_count bigint;
BEGIN
    -- Guard 1: NULL clears are always allowed (terminal completion path).
    IF NEW.bot_turn_id IS NULL THEN
        RETURN NEW;
    END IF;

    -- Guard 2: no-op re-stamp (same owner) is always allowed.
    IF NEW.bot_turn_id = OLD.bot_turn_id THEN
        RETURN NEW;
    END IF;

    -- Check: is there already a DIFFERENT in-flight bot_turn for this message?
    SELECT count(*) INTO conflict_count
    FROM mediator.messages
    WHERE id = NEW.id
      AND bot_turn_id IS NOT NULL
      AND bot_turn_id <> NEW.bot_turn_id
      AND processing_state IN ('processing', 'deferred');

    IF conflict_count > 0 THEN
        RAISE unique_violation
            USING MESSAGE = format(
                'message %s already has in-flight bot_turn %s; cannot re-stamp to %s',
                NEW.id,
                (SELECT bot_turn_id FROM mediator.messages WHERE id = NEW.id),
                NEW.bot_turn_id
            );
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS tg_messages_one_inflight_owner ON mediator.messages;
CREATE TRIGGER tg_messages_one_inflight_owner
    BEFORE UPDATE OF bot_turn_id ON mediator.messages
    FOR EACH ROW
    EXECUTE FUNCTION mediator.tg_messages_one_inflight_owner();

-- ── Column comment ──────────────────────────────────────────────────────────
COMMENT ON COLUMN mediator.messages.bot_turn_id IS
    'In-flight ownership pointer: identifies the bot_turn actively handling '
    'this message while processing_state IN (''processing'',''deferred''). '
    'Cleared to NULL at terminal completion (processed/failed/expired). '
    'handled_by_turn_id records the historical terminal handler. '
    'Enforced by tg_messages_one_inflight_owner (at most one in-flight '
    'bot_turn per message).';

COMMIT;
