BEGIN;

-- Discord pacing preferences and durable decision/event observability.
-- Runtime access uses service-role DB connections; RLS still denies anon by
-- default for defense in depth.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS pacing_preferences jsonb NOT NULL DEFAULT '{}'::jsonb;

DO $$
BEGIN
    ALTER TABLE users
        ADD CONSTRAINT users_pacing_preferences_object_check
        CHECK (jsonb_typeof(pacing_preferences) = 'object');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS pacing_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users(id),
    message_ids uuid[] NOT NULL DEFAULT '{}',
    source text NOT NULL DEFAULT 'live',
    decision text NOT NULL CHECK (
        decision IN (
            'wait',
            'react',
            'silence',
            'answer',
            'typing_wait',
            'typing_start',
            'typing_stop',
            'fallback'
        )
    ),
    reason text NOT NULL,
    signal_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    preference_snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
    wait_ms integer CHECK (wait_ms IS NULL OR wait_ms >= 0),
    reaction text,
    llm_judgement jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pacing_events_user_created
    ON pacing_events (user_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pacing_events_decision_created
    ON pacing_events (decision, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pacing_events_message_ids_gin
    ON pacing_events USING gin (message_ids);

ALTER TABLE pacing_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE pacing_events FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    CREATE POLICY deny_anon_pacing_events ON pacing_events
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
