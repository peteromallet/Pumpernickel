BEGIN;

-- Per-bot partner sharing replaces users.cross_thread_sharing_default.
-- Ordering is intentional: add and backfill the new user_bot_state column
-- before dropping the legacy users column at the end of this transaction.

INSERT INTO bots (id, display_name)
VALUES ('tante_rosi', 'Tante Rosi')
ON CONFLICT (id) DO NOTHING;

ALTER TABLE user_bot_state
    ADD COLUMN IF NOT EXISTS partner_share text;

DO $$
BEGIN
    ALTER TABLE user_bot_state
        ADD CONSTRAINT user_bot_state_partner_share_check
        CHECK (partner_share IS NULL OR partner_share IN ('opt_in', 'opt_out'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'users'
          AND column_name = 'cross_thread_sharing_default'
    ) THEN
        INSERT INTO user_bot_state (user_id, bot_id, onboarding_state, partner_share, updated_at)
        SELECT
            u.id,
            'mediator',
            COALESCE(u.onboarding_state, 'pending'),
            u.cross_thread_sharing_default,
            now()
        FROM users u
        WHERE u.cross_thread_sharing_default IS NOT NULL
        ON CONFLICT (user_id, bot_id) DO UPDATE
        SET partner_share = EXCLUDED.partner_share,
            updated_at = now();
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_user_bot_state_partner_share
    ON user_bot_state (bot_id, partner_share)
    WHERE partner_share IS NOT NULL;

ALTER TABLE memories
    ADD COLUMN IF NOT EXISTS visibility text NOT NULL DEFAULT 'private',
    ADD COLUMN IF NOT EXISTS shareable_summary text,
    ADD COLUMN IF NOT EXISTS shareable_summary_encrypted bytea;

DO $$
BEGIN
    ALTER TABLE memories
        ADD CONSTRAINT memories_visibility_check
        CHECK (visibility IN ('private', 'dyad_shareable'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE memories
        ADD CONSTRAINT memories_shareable_summary_required_check
        CHECK (
            visibility <> 'dyad_shareable'
            OR (shareable_summary IS NOT NULL AND length(btrim(shareable_summary)) > 0)
        );
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_memories_visibility_status_created
    ON memories (visibility, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_memories_shareable_about_bot_recent
    ON memories (
        about_user_id,
        recorded_by_bot_id,
        COALESCE(last_referenced_at, created_at) DESC
    )
    WHERE status = 'active' AND visibility = 'dyad_shareable';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'users'
          AND column_name = 'cross_thread_sharing_default'
    ) THEN
        ALTER TABLE users DROP CONSTRAINT IF EXISTS users_cross_thread_sharing_default_check;
        ALTER TABLE users DROP COLUMN cross_thread_sharing_default;
    END IF;
END $$;

COMMIT;
