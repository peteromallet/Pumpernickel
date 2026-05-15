-- 0038_commitments_events down: Drop events, then commitments (FK order).
BEGIN;

DROP TABLE IF EXISTS mediator.events;
DROP TABLE IF EXISTS mediator.commitments;

COMMIT;
