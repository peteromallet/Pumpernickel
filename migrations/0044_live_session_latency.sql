-- 0044_live_session_latency: per-stage latency spans for SLO tracking.
--
-- Sprint 4 DoD: write per-stage spans (asr_finalize, orchestrator+db,
-- llm_ttft, tts_first_byte) per turn. p50/p95/p99 are computed against
-- these rows.

BEGIN;

CREATE TABLE mediator.live_session_latency (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    turn_index integer NOT NULL,
    stage text NOT NULL
        CHECK (stage IN (
            'asr_finalize',
            'orchestrator_db',
            'llm_ttft',
            'tts_first_byte',
            'ear_to_ear'
        )),
    elapsed_ms integer NOT NULL CHECK (elapsed_ms >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_live_session_latency_conv_turn
    ON mediator.live_session_latency (conversation_id, turn_index);
CREATE INDEX idx_live_session_latency_stage_recent
    ON mediator.live_session_latency (stage, created_at DESC);

ALTER TABLE mediator.live_session_latency ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.live_session_latency FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.live_session_latency FROM anon;

DO $$
BEGIN
    CREATE POLICY deny_anon_live_session_latency ON mediator.live_session_latency
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_live_session_latency ON mediator.live_session_latency
        FOR SELECT
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = live_session_latency.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
