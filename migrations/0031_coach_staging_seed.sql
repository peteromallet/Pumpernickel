-- ============================================================
-- Sprint 4: STAGING-ONLY seed for the solo "coach" bot
-- ============================================================
-- This migration seeds the 'coach' bot, the 'career' topic, and a solo
-- bot_binding so that S5 can wire the coach prompt + transport in pure code.
--
-- Guard semantics:
--   ALL DML lives inside a single DO $$ block. The first IF checks that
--   the database name contains the substring 'staging'; otherwise RETURN
--   exits the PL/pgSQL block (RETURN does NOT exit a SQL transaction).
--   On non-staging databases the migration is a strict no-op.
-- ============================================================

BEGIN;

DO $$
BEGIN
    IF current_database() NOT LIKE '%staging%' THEN
        RAISE NOTICE '0031_coach_staging_seed: skipping — database % is not a staging database', current_database();
        RETURN;
    END IF;

    INSERT INTO topics (slug, display_name, description, participants_shape)
    VALUES ('career', 'Career', 'Solo career/work coaching topic', 'solo')
    ON CONFLICT (slug) DO NOTHING;

    INSERT INTO bots (id, display_name)
    VALUES ('coach', 'Coach')
    ON CONFLICT (id) DO NOTHING;

    -- Channel seeding deferred to S5 / out-of-band Discord provisioning (U3).
    -- Once the staging Discord bot account is registered, a follow-up
    -- INSERT INTO channels (...) can populate transport rows.
    -- bot_binding for coach is solo-shaped; user_id is left to S5 to fill
    -- once consent is obtained (see U3). Nothing to insert here yet without
    -- a target user, but the bot + topic rows are sufficient pre-flight.
END $$;

COMMIT;
