-- 0042_live_conversations: Tables for the live voice / live-conversation surface.
--
-- Adds the session envelope and supporting tables for a real-time voice
-- conversation between a user (and optional partner) and a coach bot, per
-- docs/live-conversation-mode.md.
--
-- Tables (all under mediator schema, snake_case plural):
--   conversations                 — session envelope; one row per session
--   conversation_items            — agenda items (planned / dynamic / thread)
--   transcript_turns              — append-only utterance history
--   conversation_notes            — Haiku-flagged session-local facts
--   item_visits                   — audit log of item traversal
--   conversation_consent_events   — append-only consent audit (NOT JSONB)
--   conversation_speakers         — speaker_label -> role + consent state
--                                   (NOT JSONB; one row per session+label)
--
-- Per-bot/per-user sharing model: a session is owned by user_id; a partner
-- may be either an existing user (partner_user_id) OR a label-only
-- participant who never logs in (partner_label).  Exactly one of the two
-- (or neither) may be set — enforced by a CHECK.
--
-- All tables: ENABLE + FORCE ROW LEVEL SECURITY, REVOKE ALL FROM anon, a
-- deny_anon catch-all, plus an owner-scoped policy.  Service-role bypasses
-- RLS, so this is defense-in-depth aligned with the rest of the schema.

BEGIN;

-- ===========================================================================
-- 1. conversations — session envelope
-- ===========================================================================

CREATE TABLE mediator.conversations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES mediator.users(id),
    partner_user_id uuid REFERENCES mediator.users(id),
    partner_label text,
    topic_id uuid REFERENCES mediator.topics(id),
    bot_id text REFERENCES mediator.bots(id),
    mode text NOT NULL
        CHECK (mode IN ('steered', 'open')),
    steering_text text,
    prep_summary text,
    status text NOT NULL DEFAULT 'prepping'
        CHECK (status IN (
            'prepping',
            'ready',
            'live',
            'ended',
            'synthesizing',
            'review_pending',
            'synthesized',
            'discarded',
            'failed'
        )),
    -- Back-pointer into conversation_items.  FK added below after that table
    -- exists.  Nullable: set when prep completes / Haiku advances.
    current_item_id uuid,
    session_fields jsonb NOT NULL DEFAULT '{}'::jsonb,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),

    -- At most one of partner_user_id / partner_label may be set.  Both null
    -- is allowed (solo session).
    CONSTRAINT conversations_partner_xor
        CHECK (partner_user_id IS NULL OR partner_label IS NULL)
);

CREATE INDEX idx_conversations_user_started
    ON mediator.conversations (user_id, started_at DESC);
CREATE INDEX idx_conversations_partner_user
    ON mediator.conversations (partner_user_id)
    WHERE partner_user_id IS NOT NULL;
CREATE INDEX idx_conversations_status_active
    ON mediator.conversations (status)
    WHERE status IN ('prepping', 'ready', 'live', 'synthesizing', 'review_pending');

-- ===========================================================================
-- 2. conversation_items — agenda items (planned / dynamic / thread)
-- ===========================================================================

CREATE TABLE mediator.conversation_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    theme_id uuid REFERENCES mediator.themes(id),
    parent_item_id uuid REFERENCES mediator.conversation_items(id),
    kind text NOT NULL
        CHECK (kind IN ('planned', 'dynamic', 'thread')),
    title text NOT NULL,
    intent text,
    ask text,
    done_when text,
    next_item_ids uuid[] NOT NULL DEFAULT '{}',
    priority text NOT NULL DEFAULT 'should'
        CHECK (priority IN ('must', 'should', 'optional')),
    speaker_scope text NOT NULL DEFAULT 'primary'
        CHECK (speaker_scope IN ('primary', 'partner', 'both')),
    coverage_evidence_required text NOT NULL DEFAULT 'explicit_answer'
        CHECK (coverage_evidence_required IN (
            'explicit_answer',
            'emotional_shift',
            'concrete_decision',
            'blocker_named'
        )),
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'covered', 'skipped')),
    coverage_summary text,
    -- "No quote, no coverage" — schema-level enforcement of the
    -- evidence-quote requirement when status = 'covered'.
    coverage_evidence_quote text,
    order_hint integer NOT NULL DEFAULT 0,
    covered_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT conversation_items_covered_requires_quote
        CHECK (status <> 'covered' OR coverage_evidence_quote IS NOT NULL)
);

CREATE INDEX idx_conversation_items_conv
    ON mediator.conversation_items (conversation_id, order_hint);
CREATE INDEX idx_conversation_items_theme
    ON mediator.conversation_items (theme_id)
    WHERE theme_id IS NOT NULL;
CREATE INDEX idx_conversation_items_open_threads
    ON mediator.conversation_items (conversation_id)
    WHERE kind = 'thread' AND status IN ('pending', 'active');

-- Now that conversation_items exists, wire the back-pointer FK on
-- conversations.current_item_id.
ALTER TABLE mediator.conversations
    ADD CONSTRAINT conversations_current_item_fk
    FOREIGN KEY (current_item_id)
    REFERENCES mediator.conversation_items(id);

-- ===========================================================================
-- 3. transcript_turns — append-only utterance history
-- ===========================================================================

CREATE TABLE mediator.transcript_turns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    ts timestamptz NOT NULL DEFAULT now(),
    speaker_label text NOT NULL,
    speaker_role text NOT NULL
        CHECK (speaker_role IN ('primary', 'partner', 'other', 'bot')),
    text text NOT NULL,
    asr_confidence real,
    active_item_id uuid REFERENCES mediator.conversation_items(id),
    was_routing_input boolean NOT NULL DEFAULT false
);

CREATE INDEX idx_transcript_turns_conv_ts
    ON mediator.transcript_turns (conversation_id, ts);

-- ===========================================================================
-- 4. conversation_notes — Haiku-flagged session-local facts
-- ===========================================================================

CREATE TABLE mediator.conversation_notes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    text text NOT NULL,
    attributed_to_speaker text
        CHECK (attributed_to_speaker IS NULL
               OR attributed_to_speaker IN ('primary', 'partner', 'other')),
    evidence_turn_id uuid REFERENCES mediator.transcript_turns(id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversation_notes_conv
    ON mediator.conversation_notes (conversation_id, created_at);

-- ===========================================================================
-- 5. item_visits — audit log of item traversal
-- ===========================================================================

CREATE TABLE mediator.item_visits (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    item_id uuid NOT NULL REFERENCES mediator.conversation_items(id),
    entered_at timestamptz NOT NULL DEFAULT now(),
    exited_at timestamptz,
    transition_reason text
);

CREATE INDEX idx_item_visits_conv
    ON mediator.item_visits (conversation_id, entered_at);

-- ===========================================================================
-- 6. conversation_consent_events — append-only consent audit (NOT JSONB)
-- ===========================================================================

CREATE TABLE mediator.conversation_consent_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    speaker_label text NOT NULL,
    role text NOT NULL
        CHECK (role IN ('primary', 'partner', 'other')),
    event_type text NOT NULL
        CHECK (event_type IN ('granted', 'declined', 'withdrawn', 'reaffirmed')),
    method text
        CHECK (method IS NULL
               OR method IN ('voice', 'screen_tap', 'system')),
    note text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_conversation_consent_events_conv
    ON mediator.conversation_consent_events (conversation_id, created_at);

-- ===========================================================================
-- 7. conversation_speakers — speaker_label -> role + consent state
-- ===========================================================================

CREATE TABLE mediator.conversation_speakers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL
        REFERENCES mediator.conversations(id) ON DELETE CASCADE,
    speaker_label text NOT NULL,
    role text NOT NULL
        CHECK (role IN ('primary', 'partner', 'other', 'bot')),
    consent_state text NOT NULL DEFAULT 'pending'
        CHECK (consent_state IN ('pending', 'granted', 'declined', 'withdrawn')),
    first_heard_at timestamptz NOT NULL DEFAULT now(),
    consented_at timestamptz,
    withdrawn_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT conversation_speakers_unique_label
        UNIQUE (conversation_id, speaker_label)
);

CREATE INDEX idx_conversation_speakers_conv
    ON mediator.conversation_speakers (conversation_id);

-- ===========================================================================
-- RLS: enable + force + revoke anon + deny_anon + owner-scoped policies
-- ===========================================================================

ALTER TABLE mediator.conversations ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.conversations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.conversations FROM anon;

ALTER TABLE mediator.conversation_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.conversation_items FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.conversation_items FROM anon;

ALTER TABLE mediator.transcript_turns ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.transcript_turns FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.transcript_turns FROM anon;

ALTER TABLE mediator.conversation_notes ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.conversation_notes FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.conversation_notes FROM anon;

ALTER TABLE mediator.item_visits ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.item_visits FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.item_visits FROM anon;

ALTER TABLE mediator.conversation_consent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.conversation_consent_events FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.conversation_consent_events FROM anon;

ALTER TABLE mediator.conversation_speakers ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.conversation_speakers FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.conversation_speakers FROM anon;

-- deny_anon catch-all on every table.
DO $$
BEGIN
    CREATE POLICY deny_anon_conversations ON mediator.conversations
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_conversation_items ON mediator.conversation_items
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_transcript_turns ON mediator.transcript_turns
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_conversation_notes ON mediator.conversation_notes
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_item_visits ON mediator.item_visits
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_conversation_consent_events
        ON mediator.conversation_consent_events
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY deny_anon_conversation_speakers ON mediator.conversation_speakers
        FOR ALL TO anon USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- Owner-scoped policies.  Owner = the user who started the session OR the
-- partner user (when set).  Child tables EXISTS-join through conversations.
-- These are intentionally permissive for any role that has auth.uid() set
-- (i.e. authenticated end-user JWTs); service-role bypasses RLS entirely.

DO $$
BEGIN
    CREATE POLICY owner_scoped_conversations ON mediator.conversations
        FOR ALL
        USING (user_id = auth.uid() OR partner_user_id = auth.uid())
        WITH CHECK (user_id = auth.uid() OR partner_user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_conversation_items ON mediator.conversation_items
        FOR ALL
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_items.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_items.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_transcript_turns ON mediator.transcript_turns
        FOR ALL
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = transcript_turns.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = transcript_turns.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_conversation_notes ON mediator.conversation_notes
        FOR ALL
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_notes.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_notes.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_item_visits ON mediator.item_visits
        FOR ALL
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = item_visits.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = item_visits.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_conversation_consent_events
        ON mediator.conversation_consent_events
        FOR ALL
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_consent_events.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_consent_events.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_conversation_speakers
        ON mediator.conversation_speakers
        FOR ALL
        USING (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_speakers.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ))
        WITH CHECK (EXISTS (
            SELECT 1 FROM mediator.conversations c
            WHERE c.id = conversation_speakers.conversation_id
              AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid())
        ));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
