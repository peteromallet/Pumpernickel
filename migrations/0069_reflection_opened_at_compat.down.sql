-- 0069 is a compatibility repair for a column that is part of the canonical
-- 0066 schema. Rolling it back must not remove that canonical column.

BEGIN;
COMMIT;
