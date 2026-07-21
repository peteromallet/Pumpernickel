-- 0066_reflection_foundation: Durable reflection storage contract (M1).
--
-- Adds three reflection domain tables under the mediator schema:
--   mediator.reflection_sessions   — mutable coordination state
--   mediator.reflection_entries    — immutable normalized reflection documents
--   mediator.reflection_derivations — auditable knowledge derivation ledger
--
-- Locked boundary (see plan_v1 / idea_snapshot / SD-001–SD-016):
--   * Exactly three reflection domain tables; no fourth join/association table.
--   * Ordered source message IDs are stored as uuid[] arrays on sessions and
--     entries, not in a separate association/join table.
--   * Finalized sessions are the durable processing queue — no scheduled_jobs
--     integration and no separate processing-jobs table.
--   * Entries are immutable revisions; corrections append a superseding revision
--     without mutating history.  Mutable coordination state (claim, retry,
--     status) stays on sessions.
--   * No feature flags, no scheduled jobs, no proactive scheduling.
--   * No inbound routing, SuperPOM prompt, retrieval, embedding, hot context,
--     admin UI, or scheduling behavior changed.
--
-- Sections:
--   1. mediator.reflection_sessions   — mutable coordination + queue state
--   2. mediator.reflection_entries    — immutable revisions
--   3. mediator.reflection_derivations — knowledge derivation ledger
--   4. Indexes
--   5. RLS: ENABLE + FORCE + REVOKE (anon, authenticated) + deny policies +
--      owner-scoped policies bound directly to user_id
--
-- Allowed status values (reflection_sessions):
--   collecting, finalizing, processed, abandoned, processing_failed
--
-- Allowed temporal_scope values:
--   instant, day, week, month, custom, none
--
-- Allowed phase values:
--   opening, closing, checkpoint, prospective, retrospective, freeform
--
-- Allowed derivation_kind values (reflection_derivations):
--   memory, observation, distillation, orientation
--
-- Allowed assertion_source values:
--   user_explicit, user_implied, agent_inferred
--
-- Allowed decision values:
--   applied, reinforced, deferred, rejected, superseded
--
-- Allowed failure_class values (open enum, CHECK validates known values):
--   retryable_processor, terminal_input, terminal_internal, stale_claim

BEGIN;

-- ===========================================================================
-- 1. mediator.reflection_sessions — mutable coordination + claim/queue state
-- ===========================================================================

CREATE TABLE mediator.reflection_sessions (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     uuid NOT NULL
        REFERENCES mediator.users(id),
    topic_id                    uuid
        REFERENCES mediator.topics(id),
    bot_id                      text NOT NULL
        REFERENCES mediator.bots(id),

    -- Session initiation.
    opened_at                    timestamptz NOT NULL DEFAULT now(),
    opened_by_message_id        uuid
        REFERENCES mediator.messages(id),
    opened_by_turn_id           uuid
        REFERENCES mediator.bot_turns(id) ON DELETE SET NULL,

    -- Ordered source message IDs collected during this session.  Every message
    -- that was part of the reflection train of thought is appended here in
    -- sent_at order so the processor does not need to query messages to
    -- reconstruct chronology.
    source_message_ids          uuid[] NOT NULL DEFAULT '{}',

    -- Template and temporal classification.
    template_key                text NOT NULL,
    temporal_scope              text NOT NULL
        CHECK (temporal_scope IN ('instant', 'day', 'week', 'month', 'custom', 'none')),
    phase                       text NOT NULL
        CHECK (phase IN ('opening', 'closing', 'checkpoint', 'prospective', 'retrospective', 'freeform')),
    period_start                timestamptz,
    period_end                  timestamptz,
    timezone                    text,

    -- Classification metadata.
    classification_source       text,
    classification_confidence   real
        CHECK (classification_confidence IS NULL OR (classification_confidence >= 0 AND classification_confidence <= 1)),
    classification_metadata     jsonb,

    -- Lifecycle status.
    status                      text NOT NULL DEFAULT 'collecting'
        CHECK (status IN ('collecting', 'finalizing', 'processed', 'abandoned', 'processing_failed')),
    idle_finalize_at            timestamptz,
    finalized_at                timestamptz,
    processed_at                timestamptz,
    abandoned_at                timestamptz,

    -- Claim / queue / retry state (finalized sessions are the durable queue).
    claimed_by                  text,
    claimed_at                  timestamptz,
    retry_count                 integer NOT NULL DEFAULT 0
        CHECK (retry_count >= 0),
    failure_class               text
        CHECK (failure_class IS NULL OR failure_class IN ('retryable_processor', 'terminal_input', 'terminal_internal', 'stale_claim')),
    failure_reason              text,
    last_error                  text,

    -- Idempotency key so external callers can safely retry session creation
    -- without producing duplicate rows.
    idempotency_key             text,

    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    -- Lifecycle consistency guards:
    -- (a) abandoned requires abandoned_at.
    -- (b) processed / processing_failed require finalized_at (you cannot
    --     process a session that was never finalized).
    -- (c) self-consistency: idle_finalize_at is only meaningful during
    --     collection.
    CHECK (
        status <> 'abandoned'
        OR abandoned_at IS NOT NULL
    ),
    CHECK (
        status NOT IN ('processed', 'processing_failed')
        OR finalized_at IS NOT NULL
    ),
    CHECK (
        idle_finalize_at IS NULL
        OR status = 'collecting'
    ),

    -- Idempotency keys must be unique when non-null so retried creation
    -- attempts hit the same row.
    UNIQUE (idempotency_key)
);

-- ===========================================================================
-- 2. mediator.reflection_entries — immutable normalized reflection documents
-- ===========================================================================

CREATE TABLE mediator.reflection_entries (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              uuid NOT NULL
        REFERENCES mediator.reflection_sessions(id) ON DELETE CASCADE,
    user_id                 uuid NOT NULL
        REFERENCES mediator.users(id),
    topic_id                uuid
        REFERENCES mediator.topics(id),
    bot_id                  text NOT NULL
        REFERENCES mediator.bots(id),

    -- Template + temporal classification (denormalized from session for
    -- self-contained reads of the immutable record).
    template_key            text NOT NULL,
    temporal_scope          text NOT NULL
        CHECK (temporal_scope IN ('instant', 'day', 'week', 'month', 'custom', 'none')),
    phase                   text NOT NULL
        CHECK (phase IN ('opening', 'closing', 'checkpoint', 'prospective', 'retrospective', 'freeform')),
    period_start            timestamptz,
    period_end              timestamptz,
    timezone                text,

    -- Ordered source message IDs (immutable snapshot at revision creation time).
    source_message_ids      uuid[] NOT NULL DEFAULT '{}',

    -- Encrypted structured payload (AES-GCM bytea, AGV1 prefix) following
    -- the dual-column convention from migration 0007 / app/services/crypto.py.
    -- payload_encrypted stores the full JSON payload; plaintext_searchable
    -- is the minimal canonical plaintext for retrieval/embedding.
    payload_encrypted       bytea,
    plaintext_searchable    text,

    -- Encrypted human-readable summary.
    summary_encrypted       bytea,

    -- Versioning.
    schema_version          integer NOT NULL DEFAULT 1
        CHECK (schema_version >= 1),
    processor_version       text,
    revision_number         integer NOT NULL DEFAULT 1
        CHECK (revision_number >= 1),

    -- Immutable revision chain: corrections create a new row with
    -- supersedes_entry_id pointing at the prior revision.  A row must
    -- never supersede itself.
    supersedes_entry_id     uuid
        REFERENCES mediator.reflection_entries(id) ON DELETE SET NULL,

    created_by_turn_id      uuid
        REFERENCES mediator.bot_turns(id) ON DELETE SET NULL,
    created_at              timestamptz NOT NULL DEFAULT now(),

    -- One revision number per session (entry ordering within a session).
    UNIQUE (session_id, revision_number),

    -- Self-supersession guard.
    CHECK (supersedes_entry_id IS NULL OR supersedes_entry_id <> id)
);

-- ===========================================================================
-- 3. mediator.reflection_derivations — knowledge derivation ledger
-- ===========================================================================

CREATE TABLE mediator.reflection_derivations (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reflection_entry_id         uuid NOT NULL
        REFERENCES mediator.reflection_entries(id) ON DELETE CASCADE,
    user_id                     uuid NOT NULL
        REFERENCES mediator.users(id),

    -- What kind of knowledge is being derived.
    derivation_kind             text NOT NULL
        CHECK (derivation_kind IN ('memory', 'observation', 'distillation', 'orientation')),

    -- Encrypted candidate payload (what the processor proposed to write).
    candidate_payload_encrypted bytea,

    -- Provenance: how strongly does the user's input support this?
    assertion_source            text NOT NULL
        CHECK (assertion_source IN ('user_explicit', 'user_implied', 'agent_inferred')),
    confidence                  real
        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),

    -- Deterministic eligibility reasons (JSON array of rule keys that fired).
    eligibility_reasons         jsonb,

    -- Exact supporting message IDs within the reflection entry.
    supporting_message_ids      uuid[] NOT NULL DEFAULT '{}',

    -- Decision taken by the knowledge derivation stage.
    decision                    text NOT NULL DEFAULT 'deferred'
        CHECK (decision IN ('applied', 'reinforced', 'deferred', 'rejected', 'superseded')),

    -- When decision='applied', these record the target durable row.
    applied_target_table        text,
    applied_target_id           uuid,

    -- Processor provenance.
    processor_version           text,
    processor_turn_id           uuid
        REFERENCES mediator.bot_turns(id) ON DELETE SET NULL,

    -- Idempotency key so retried processing does not create duplicate
    -- derivation rows for the same (entry, kind, logical candidate).
    idempotency_key             text,

    created_at                  timestamptz NOT NULL DEFAULT now(),
    decided_at                  timestamptz,

    -- Applied decisions must record both the target table and ID.
    CHECK (
        decision <> 'applied'
        OR (applied_target_table IS NOT NULL AND applied_target_id IS NOT NULL)
    ),

    UNIQUE (idempotency_key)
);

-- ===========================================================================
-- 4. Indexes
-- ===========================================================================

-- --- reflection_sessions ---

-- Enforce at most one collecting session per (user_id, bot_id).  This is the
-- concurrency guard that prevents two simultaneous reflection sessions from
-- being opened for the same user+bot pair.
CREATE UNIQUE INDEX idx_reflection_sessions_one_collecting
    ON mediator.reflection_sessions (user_id, bot_id)
    WHERE status = 'collecting';

-- Sweeper: finalized sessions that are ready for processing (ordered by
-- finalized_at so oldest-first claim is natural).
CREATE INDEX idx_reflection_sessions_finalized_ready
    ON mediator.reflection_sessions (finalized_at)
    WHERE status = 'finalizing';

-- Sweeper: stale claims (processing_failed sessions eligible for retry).
CREATE INDEX idx_reflection_sessions_failed_retry
    ON mediator.reflection_sessions (retry_count, finalized_at)
    WHERE status = 'processing_failed';

-- Sweeper: idle sessions due for auto-finalization.
CREATE INDEX idx_reflection_sessions_idle_due
    ON mediator.reflection_sessions (idle_finalize_at)
    WHERE status = 'collecting' AND idle_finalize_at IS NOT NULL;

-- User-facing: recent sessions for a user (list / history view).
CREATE INDEX idx_reflection_sessions_user_recent
    ON mediator.reflection_sessions (user_id, created_at DESC);

-- Claim recovery: find sessions claimed by a specific worker.
CREATE INDEX idx_reflection_sessions_claimed_by
    ON mediator.reflection_sessions (claimed_by, claimed_at)
    WHERE claimed_by IS NOT NULL;

-- --- reflection_entries ---

-- Primary lookup: entries for a session ordered by revision.
CREATE INDEX idx_reflection_entries_session_rev
    ON mediator.reflection_entries (session_id, revision_number DESC);

-- Current (un-superseded) entry for a session.  Partial index so the common
-- "get current revision" query hits an index-only scan.
CREATE INDEX idx_reflection_entries_current
    ON mediator.reflection_entries (session_id)
    WHERE supersedes_entry_id IS NULL;

-- Revision chain traversal: given an entry, find its successor.
CREATE INDEX idx_reflection_entries_supersedes
    ON mediator.reflection_entries (supersedes_entry_id)
    WHERE supersedes_entry_id IS NOT NULL;

-- User-facing: recent entries across all sessions.
CREATE INDEX idx_reflection_entries_user_recent
    ON mediator.reflection_entries (user_id, created_at DESC);

-- --- reflection_derivations ---

-- Lookup derivations for a given entry.
CREATE INDEX idx_reflection_derivations_entry
    ON mediator.reflection_derivations (reflection_entry_id, derivation_kind);

-- Sweeper: pending derivation decisions (deferred).
CREATE INDEX idx_reflection_derivations_deferred
    ON mediator.reflection_derivations (reflection_entry_id)
    WHERE decision = 'deferred';

-- ===========================================================================
-- 5. RLS — defense-in-depth, matching 0038 / 0051 / 0060 conventions
-- ===========================================================================
-- Reflection state is per-user and MUST NOT mix participants.  All three
-- tables scope DIRECTLY on user_id (not through conversations), enforcing
-- the invariant that one participant's reflections are never visible to
-- another.

-- reflection_sessions --------------------------------------------------------

ALTER TABLE mediator.reflection_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.reflection_sessions FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.reflection_sessions FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_reflection_sessions ON mediator.reflection_sessions
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_reflection_sessions ON mediator.reflection_sessions
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- reflection_entries ---------------------------------------------------------

ALTER TABLE mediator.reflection_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.reflection_entries FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.reflection_entries FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_reflection_entries ON mediator.reflection_entries
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_reflection_entries ON mediator.reflection_entries
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- reflection_derivations -----------------------------------------------------

ALTER TABLE mediator.reflection_derivations ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.reflection_derivations FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.reflection_derivations FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_reflection_derivations ON mediator.reflection_derivations
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_reflection_derivations ON mediator.reflection_derivations
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
