-- 0060_user_orientation: Durable User Orientation storage contract.
--
-- Adds three tables under the mediator schema that store reviewed user
-- orientation state — the principles, goals, priorities, and anti-patterns a
-- participant has stated or confirmed.  This is the canonical orientation
-- layer; Compass (built in later batches) is the product/service read path
-- over these rows, and commitments/events remain the authoritative execution
-- evidence and progress surface.
--
-- Locked boundary (see plan_v1 / idea_snapshot):
--   * Storage name is "User Orientation".  Product read layer is "Compass".
--   * Do NOT create durable compass_* tables in this (or any) migration.
--   * Do NOT store orientation as a conversation_artifacts snapshot.
--   * Do NOT add commitments.orientation_goal_id here — goal<->commitment and
--     goal<->event relationships are represented only through
--     user_orientation_item_links, keeping orientation state cleanly
--     separated from execution evidence.
--   * Compass reads are scoped to a single user_id and explicit allowed
--     topics; no broad "all" topic bypass is encoded at the storage layer.
--
-- Sections:
--   1. mediator.user_orientation_items — canonical orientation rows
--   2. mediator.user_orientation_item_links — evidence/progress links
--   3. mediator.user_orientation_item_reviews — review/close audit
--   4. Indexes
--   5. RLS: ENABLE + FORCE + REVOKE (anon, authenticated) + deny policies +
--      owner-scoped policies bound directly to user_id (orientation state is
--      per-user, NOT conversation-scoped, so it must never mix participants)
--
-- Allowed kind values (stable literal form for static test extraction):
--   principle, goal, priority, anti_pattern
--
-- Allowed status values:
--   pending, active, completed, retired, superseded, rejected
--   (pending = unreviewed/proposed; active = reviewed and in effect;
--    completed/retired/superseded/rejected are terminal-ish lifecycle states)
--
-- Allowed source values:
--   user_stated, user_confirmed, bot_proposed
--   (bot_proposed rows default to pending review and are excluded from
--    Compass hot context until reviewed)
--
-- Allowed review_verdict values (user_orientation_item_reviews):
--   accepted, corrected, rejected, retired, superseded, completed
--
-- Allowed target_table values (user_orientation_item_links):
--   commitments, events
--   (These are EVIDENCE/PROGRESS links only — they must not duplicate goal
--    lifecycle state and must not point at orientation rows themselves.)
--
-- Allowed relation values (user_orientation_item_links):
--   evidence, progress, supports, contradicts, completes

BEGIN;

-- ===========================================================================
-- 1. mediator.user_orientation_items — canonical orientation rows
-- ===========================================================================

CREATE TABLE mediator.user_orientation_items (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             uuid NOT NULL
        REFERENCES mediator.users(id),
    topic_id            uuid
        REFERENCES mediator.topics(id),
    bot_id              text NOT NULL
        REFERENCES mediator.bots(id),
    created_by_turn_id  uuid
        REFERENCES mediator.bot_turns(id) ON DELETE SET NULL,

    kind                text NOT NULL
        CHECK (kind IN ('principle', 'goal', 'priority', 'anti_pattern')),
    status              text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'completed', 'retired', 'superseded', 'rejected')),
    source              text NOT NULL DEFAULT 'user_stated'
        CHECK (source IN ('user_stated', 'user_confirmed', 'bot_proposed')),
    review_state        text NOT NULL DEFAULT 'unreviewed'
        CHECK (review_state IN ('unreviewed', 'reviewed', 'excluded')),

    -- Human-meaningful content.  label is the short orientation statement;
    -- detail is optional elaboration.  Both are required to be non-blank when
    -- present so Compass rendering never emits an empty line.
    label               text NOT NULL CHECK (length(btrim(label)) > 0),
    detail              text,

    -- Shared lifecycle fields (apply across all kinds).
    started_at          timestamptz,
    effective_at        timestamptz,
    target_date         date,
    completed_at        timestamptz,
    closed_reason       text,
    outcome_note        text,

    -- Supersession chain.  When an item supersedes another, the prior row is
    -- flipped to status='superseded' and this column points at it.  A row may
    -- never supersede itself, and only one active row per chain is expected
    -- (enforced in service code; the storage layer keeps the link structural).
    supersedes_item_id  uuid
        REFERENCES mediator.user_orientation_items(id) ON DELETE SET NULL,

    -- Optional free-form ordering hint for priorities (1 = highest).  Kept
    -- nullable so principles/goals/anti_patterns are not forced to carry one.
    priority_rank       integer CHECK (priority_rank IS NULL OR priority_rank >= 1),

    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- Lifecycle consistency guards.
    -- (a) A completed item must record when it completed.
    -- (b) bot_proposed rows must remain unreviewed/excluded at the storage
    --     layer — they are never 'reviewed' without an explicit review insert
    --     (service code enforces the review row; this keeps the contract honest
    --     and is what lets Compass safely exclude bot-proposed rows by default).
    -- (c) supersedes_item_id cannot reference the row itself.
    -- (Note: status is a single scalar, so 'completed' and 'retired' are
    --  inherently mutually exclusive — no extra guard needed for that.)
    CHECK (
        status <> 'completed'
        OR completed_at IS NOT NULL
    ),
    CHECK (
        source <> 'bot_proposed'
        OR review_state IN ('unreviewed', 'excluded')
    ),
    CHECK (supersedes_item_id IS NULL OR supersedes_item_id <> id)
);

-- ===========================================================================
-- 2. mediator.user_orientation_item_links — evidence/progress links
-- ===========================================================================
-- Links an orientation item (typically a goal) to existing durable execution
-- rows (commitments, events) as EVIDENCE or PROGRESS context.  These links do
-- NOT carry goal lifecycle and must NOT be used to duplicate adherence state;
-- commitments/events remain authoritative for execution.

CREATE TABLE mediator.user_orientation_item_links (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id         uuid NOT NULL
        REFERENCES mediator.user_orientation_items(id) ON DELETE CASCADE,
    -- Ownership is denormalized from the parent item so scope checks and RLS
    -- policies can enforce per-user isolation without an extra hop, and so a
    -- link can never silently drift to a different participant.
    user_id         uuid NOT NULL
        REFERENCES mediator.users(id),
    topic_id        uuid
        REFERENCES mediator.topics(id),

    target_table    text NOT NULL
        CHECK (target_table IN ('commitments', 'events')),
    target_id       uuid NOT NULL,
    relation        text NOT NULL
        CHECK (relation IN ('evidence', 'progress', 'supports', 'contradicts', 'completes')),

    note            text,
    created_at      timestamptz NOT NULL DEFAULT now(),

    UNIQUE (item_id, target_table, target_id, relation)
);

-- ===========================================================================
-- 3. mediator.user_orientation_item_reviews — review/close audit
-- ===========================================================================
-- Append-only audit of every review or lifecycle-close decision so the
-- service can reconstruct who accepted/corrected/rejected/retired/
-- superseded/completed an item and why.  Compass renders based on the
-- resulting item status; this table is the trail, not the source of truth.

CREATE TABLE mediator.user_orientation_item_reviews (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id             uuid NOT NULL
        REFERENCES mediator.user_orientation_items(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL
        REFERENCES mediator.users(id),
    reviewed_by_turn_id uuid
        REFERENCES mediator.bot_turns(id) ON DELETE SET NULL,

    verdict             text NOT NULL
        CHECK (verdict IN ('accepted', 'corrected', 'rejected', 'retired', 'superseded', 'completed')),
    previous_status     text
        CHECK (previous_status IS NULL OR previous_status IN ('pending', 'active', 'completed', 'retired', 'superseded', 'rejected')),
    new_status          text NOT NULL
        CHECK (new_status IN ('pending', 'active', 'completed', 'retired', 'superseded', 'rejected')),

    note                text,
    created_at          timestamptz NOT NULL DEFAULT now()
);

-- ===========================================================================
-- 4. Indexes
-- ===========================================================================

-- Primary Compass lookup: active orientation rows for one user, optionally
-- scoped to a topic.  Partial index keeps it small since Compass only renders
-- active rows by default.
CREATE INDEX idx_user_orientation_items_active_user_topic
    ON mediator.user_orientation_items (user_id, topic_id, kind)
    WHERE status = 'active';

-- Secondary read path: any non-terminal row for a user (pending + active),
-- used by review tooling and by Compass when explicitly asked to surface
-- unreviewed items.
CREATE INDEX idx_user_orientation_items_open_user
    ON mediator.user_orientation_items (user_id, status)
    WHERE status IN ('pending', 'active');

-- Supersession chain traversal.
CREATE INDEX idx_user_orientation_items_supersedes
    ON mediator.user_orientation_items (supersedes_item_id)
    WHERE supersedes_item_id IS NOT NULL;

-- Per-user kind listing (deterministic Compass ordering keys off this).
CREATE INDEX idx_user_orientation_items_user_kind_status
    ON mediator.user_orientation_items (user_id, kind, status, created_at);

-- Reverse link lookup: find orientation items referencing a given durable row.
CREATE INDEX idx_user_orientation_item_links_target
    ON mediator.user_orientation_item_links (target_table, target_id);

-- Forward evidence lookup for an item.
CREATE INDEX idx_user_orientation_item_links_item
    ON mediator.user_orientation_item_links (item_id, relation);

-- Review history for an item (chronological).
CREATE INDEX idx_user_orientation_item_reviews_item_created
    ON mediator.user_orientation_item_reviews (item_id, created_at);

-- ===========================================================================
-- 5. RLS — defense-in-depth, matching 0038 / 0051 conventions
-- ===========================================================================
-- Orientation state is per-user and MUST NOT mix participants.  Unlike
-- conversation_artifacts (which scope through conversations.user_id /
-- partner_user_id), orientation rows are scoped DIRECTLY on user_id because
-- they are not conversation-bound.  This is the storage-level enforcement of
-- "Compass context must not mix one participant's orientation state with
-- another's."

-- user_orientation_items ----------------------------------------------------

ALTER TABLE mediator.user_orientation_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.user_orientation_items FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.user_orientation_items FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_user_orientation_items ON mediator.user_orientation_items
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_user_orientation_items ON mediator.user_orientation_items
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- user_orientation_item_links -----------------------------------------------

ALTER TABLE mediator.user_orientation_item_links ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.user_orientation_item_links FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.user_orientation_item_links FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_user_orientation_item_links ON mediator.user_orientation_item_links
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_user_orientation_item_links ON mediator.user_orientation_item_links
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- user_orientation_item_reviews ---------------------------------------------

ALTER TABLE mediator.user_orientation_item_reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.user_orientation_item_reviews FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.user_orientation_item_reviews FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_user_orientation_item_reviews ON mediator.user_orientation_item_reviews
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_user_orientation_item_reviews ON mediator.user_orientation_item_reviews
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
