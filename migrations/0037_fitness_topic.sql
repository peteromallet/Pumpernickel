-- 0037_fitness_topic: Seed fitness topic + Hector bot row (idempotent).
-- Must run before 0038_commitments_events which adds FK refs to mediator.bots(id).
BEGIN;

INSERT INTO mediator.topics (id, slug, display_name, participants_shape)
VALUES (gen_random_uuid(), 'fitness', 'Fitness', 'solo')
ON CONFLICT (slug) DO NOTHING;

-- Backfill: if an earlier deploy seeded this row with the table default
-- 'dyad', flip it to 'solo' to match build_hector_spec()'s declaration.
UPDATE mediator.topics
   SET participants_shape = 'solo'
 WHERE slug = 'fitness'
   AND participants_shape <> 'solo';

INSERT INTO mediator.bots (id, display_name)
VALUES ('hector', 'Hector')
ON CONFLICT (id) DO NOTHING;

COMMIT;
