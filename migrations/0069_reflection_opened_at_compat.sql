-- 0069_reflection_opened_at_compat: reconcile early staging installs of the
-- reflection foundation with the finalization worker's session-age contract.

BEGIN;

ALTER TABLE mediator.reflection_sessions
    ADD COLUMN IF NOT EXISTS opened_at timestamptz;

UPDATE mediator.reflection_sessions
SET opened_at = created_at
WHERE opened_at IS NULL;

ALTER TABLE mediator.reflection_sessions
    ALTER COLUMN opened_at SET DEFAULT now(),
    ALTER COLUMN opened_at SET NOT NULL;

COMMIT;
