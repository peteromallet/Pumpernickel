-- 0038_commitments_events: Create mediator.commitments and mediator.events tables.
-- Must run after 0037_fitness_topic which seeds mediator.bots('hector').
BEGIN;

CREATE TABLE mediator.commitments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES mediator.users(id),
  topic_id uuid NOT NULL REFERENCES mediator.topics(id),
  bot_id text NOT NULL REFERENCES mediator.bots(id),

  label text NOT NULL,
  kind text NOT NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'completed', 'dropped')),

  cadence text NOT NULL DEFAULT 'custom',
  days_of_week int[] NOT NULL DEFAULT '{}',
  target_count int,
  start_date date NOT NULL DEFAULT CURRENT_DATE,
  end_date date,
  schedule_rule jsonb NOT NULL DEFAULT '{}'::jsonb,

  pressure_style text NOT NULL DEFAULT 'low_key'
    CHECK (pressure_style IN ('very_gentle', 'low_key', 'firm')),

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Partial index: only active commitments are needed for most queries.
CREATE INDEX idx_commitments_active_user_topic_bot
  ON mediator.commitments (user_id, topic_id, bot_id)
  WHERE status = 'active';

CREATE TABLE mediator.events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  commitment_id uuid REFERENCES mediator.commitments(id) ON DELETE SET NULL,
  user_id uuid NOT NULL REFERENCES mediator.users(id),
  topic_id uuid NOT NULL REFERENCES mediator.topics(id),
  bot_id text NOT NULL REFERENCES mediator.bots(id),

  metric_key text NOT NULL,
  adherence_status text
    CHECK (adherence_status IN ('done', 'missed', 'excused')),
  value_numeric numeric,
  value_text text,
  unit text,
  observed_at timestamptz NOT NULL DEFAULT now(),
  note text,
  source_message_ids uuid[] NOT NULL DEFAULT '{}',

  created_at timestamptz NOT NULL DEFAULT now(),

  CHECK (
    adherence_status IS NOT NULL
    OR value_numeric IS NOT NULL
    OR value_text IS NOT NULL
  )
);

-- Index for querying events by commitment (most recent first).
CREATE INDEX idx_events_commitment_observed
  ON mediator.events (commitment_id, observed_at DESC);

-- Index for listing recent events for a user/topic.
CREATE INDEX idx_events_user_topic_observed
  ON mediator.events (user_id, topic_id, observed_at DESC);

-- ============================================================
-- RLS: deny anon on both tables (private-table posture)
-- ============================================================

ALTER TABLE mediator.commitments ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.commitments FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.commitments FROM anon;

ALTER TABLE mediator.events ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.events FROM anon;

DO $$
BEGIN
    CREATE POLICY deny_anon_commitments ON mediator.commitments
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_events ON mediator.events
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
