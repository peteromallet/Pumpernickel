-- ============================================================
-- Sprint: Tante Rosi — pregnancy coach bot
-- Migration 0033: Insert 'pregnancy' topic row
-- ============================================================
-- ON CONFLICT DO NOTHING makes this safe to re-run against any environment.
-- ============================================================

BEGIN;

INSERT INTO mediator.topics (id, slug, display_name)
VALUES (gen_random_uuid(), 'pregnancy', 'Pregnancy')
ON CONFLICT (slug) DO NOTHING;

COMMIT;