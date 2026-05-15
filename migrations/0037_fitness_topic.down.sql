-- 0037_fitness_topic down: Remove Hector bot row, then fitness topic (FK order).
BEGIN;

DELETE FROM mediator.bots WHERE id = 'hector';
DELETE FROM mediator.topics WHERE slug = 'fitness';

COMMIT;
