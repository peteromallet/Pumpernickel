BEGIN;

CREATE TABLE IF NOT EXISTS turn_audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    turn_id uuid NOT NULL REFERENCES bot_turns(id) ON DELETE CASCADE,
    event_seq integer NOT NULL CHECK (event_seq >= 1),
    event_type text NOT NULL,
    step text,
    severity text NOT NULL DEFAULT 'info' CHECK (severity IN ('debug', 'info', 'warning', 'error')),
    occurred_at timestamptz NOT NULL DEFAULT now(),
    duration_ms integer CHECK (duration_ms IS NULL OR duration_ms >= 0),
    actor text NOT NULL DEFAULT 'system',
    message text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    sensitive_metadata_encrypted bytea,
    UNIQUE (turn_id, event_seq)
);

CREATE INDEX IF NOT EXISTS idx_turn_audit_events_turn_seq
    ON turn_audit_events (turn_id, event_seq);

CREATE INDEX IF NOT EXISTS idx_turn_audit_events_type_time
    ON turn_audit_events (event_type, occurred_at DESC);

CREATE INDEX IF NOT EXISTS idx_turn_audit_events_metadata_gin
    ON turn_audit_events USING gin (metadata);

ALTER TABLE turn_audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE turn_audit_events FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_turn_audit_events ON turn_audit_events
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
