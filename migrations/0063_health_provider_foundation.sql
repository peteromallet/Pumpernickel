-- 0063_health_provider_foundation: Secure provider-ingestion storage contract.
--
-- Establishes the disabled-by-default health provider foundation for Withings.
-- This migration adds the durable connection, source-record, sync-run, webhook,
-- dirty-category, and normalized-table contracts needed by later milestones.
--
-- Locked boundaries for this migration:
--   * Provider support is intentionally narrow: provider = 'withings'.
--   * Raw provider payload storage is OFF by default. No raw-payload column is
--     created in these tables.
--   * Normalized measurement/workout/sleep/projection tables are schema
--     contracts only. This migration does not create user-visible reads or
--     adherence projections.
--   * User scoping is direct on user_id for every table so FORCE RLS can
--     enforce strict participant isolation without conversation/topic joins.
--
-- Tables created:
--   1. mediator.health_connections
--   2. mediator.health_source_records
--   3. mediator.health_sync_runs
--   4. mediator.health_webhook_receipts
--   5. mediator.health_dirty_categories
--   6. mediator.health_normalized_measurements
--   7. mediator.health_normalized_workouts
--   8. mediator.health_normalized_sleep
--   9. mediator.health_source_to_event_projections
--
-- Shared literal domains:
--   provider: withings
--   resource_type: measurement, workout, sleep

BEGIN;

-- ===========================================================================
-- 1. mediator.health_connections
-- ===========================================================================

CREATE TABLE mediator.health_connections (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                     uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    provider                    text NOT NULL
        CHECK (provider IN ('withings')),
    external_user_id            text,
    status                      text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'disconnected', 'revoked', 'reauth_required', 'deleted')),
    granted_scopes              text[] NOT NULL DEFAULT '{}',
    granted_at                  timestamptz,
    consented_measurements_at   timestamptz,
    consented_workouts_at       timestamptz,
    consented_sleep_at          timestamptz,
    access_token_encrypted      bytea,
    refresh_token_encrypted     bytea,
    access_token_expires_at     timestamptz,
    refresh_token_expires_at    timestamptz,
    refresh_token_rotated_at    timestamptz,
    cursor_state                jsonb NOT NULL DEFAULT '{}'::jsonb,
    last_success_at             timestamptz,
    last_error_at               timestamptz,
    last_error_code             text,
    last_error_detail           text,
    disconnected_at             timestamptz,
    revoked_at                  timestamptz,
    deleted_at                  timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CHECK (external_user_id IS NULL OR length(btrim(external_user_id)) > 0),
    CHECK (last_error_code IS NULL OR length(btrim(last_error_code)) > 0),
    CHECK (jsonb_typeof(cursor_state) = 'object'),
    CHECK (status <> 'disconnected' OR disconnected_at IS NOT NULL),
    CHECK (status <> 'revoked' OR revoked_at IS NOT NULL),
    CHECK (status <> 'deleted' OR deleted_at IS NOT NULL)
);

CREATE UNIQUE INDEX idx_health_connections_provider_external_user
    ON mediator.health_connections (provider, external_user_id)
    WHERE external_user_id IS NOT NULL AND deleted_at IS NULL;

CREATE UNIQUE INDEX idx_health_connections_user_provider_active
    ON mediator.health_connections (user_id, provider)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_health_connections_user_updated
    ON mediator.health_connections (user_id, updated_at DESC)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_health_connections_status_updated
    ON mediator.health_connections (status, updated_at DESC);

-- ===========================================================================
-- 2. mediator.health_source_records
-- ===========================================================================
-- This is the durable idempotency boundary for fetched provider records.
-- Deliberately excludes any raw provider payload column; only sanitized
-- metadata/hash material is retained by default.

CREATE TABLE mediator.health_source_records (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id           uuid NOT NULL
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id                 uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    provider                text NOT NULL
        CHECK (provider IN ('withings')),
    resource_type           text NOT NULL
        CHECK (resource_type IN ('measurement', 'workout', 'sleep')),
    external_id             text NOT NULL
        CHECK (length(btrim(external_id)) > 0),
    source_created_at       timestamptz,
    source_modified_at      timestamptz,
    observed_at             timestamptz,
    starts_at               timestamptz,
    ends_at                 timestamptz,
    source_timezone         text,
    source_offset_seconds   integer,
    source_device_id        text,
    source_device_model     text,
    payload_hash            text
        CHECK (payload_hash IS NULL OR length(btrim(payload_hash)) > 0),
    provider_revision       text,
    revision_count          integer NOT NULL DEFAULT 1
        CHECK (revision_count >= 1),
    source_metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    attribution             jsonb NOT NULL DEFAULT '{}'::jsonb,
    is_deleted              boolean NOT NULL DEFAULT false,
    deleted_at              timestamptz,
    imported_at             timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    UNIQUE (connection_id, resource_type, external_id),
    CHECK (jsonb_typeof(source_metadata) = 'object'),
    CHECK (jsonb_typeof(attribution) = 'object'),
    CHECK (ends_at IS NULL OR starts_at IS NULL OR ends_at >= starts_at),
    CHECK (NOT is_deleted OR deleted_at IS NOT NULL)
);

CREATE INDEX idx_health_source_records_conn_resource_modified
    ON mediator.health_source_records (connection_id, resource_type, source_modified_at DESC);

CREATE INDEX idx_health_source_records_user_resource_observed
    ON mediator.health_source_records (user_id, resource_type, observed_at DESC);

CREATE INDEX idx_health_source_records_conn_deleted
    ON mediator.health_source_records (connection_id, is_deleted, updated_at DESC);

-- ===========================================================================
-- 3. mediator.health_sync_runs
-- ===========================================================================

CREATE TABLE mediator.health_sync_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id       uuid NOT NULL
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    provider            text NOT NULL
        CHECK (provider IN ('withings')),
    resource_type       text NOT NULL
        CHECK (resource_type IN ('measurement', 'workout', 'sleep')),
    trigger_reason      text NOT NULL DEFAULT 'dirty'
        CHECK (trigger_reason IN ('dirty', 'manual', 'reconcile', 'initial_backfill', 'disconnect_cleanup')),
    status              text NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed', 'partial', 'cancelled')),
    cursor_before       jsonb NOT NULL DEFAULT '{}'::jsonb,
    cursor_after        jsonb NOT NULL DEFAULT '{}'::jsonb,
    range_started_at    timestamptz,
    range_ended_at      timestamptz,
    page_count          integer NOT NULL DEFAULT 0
        CHECK (page_count >= 0),
    fetched_count       integer NOT NULL DEFAULT 0
        CHECK (fetched_count >= 0),
    inserted_count      integer NOT NULL DEFAULT 0
        CHECK (inserted_count >= 0),
    updated_count       integer NOT NULL DEFAULT 0
        CHECK (updated_count >= 0),
    deleted_count       integer NOT NULL DEFAULT 0
        CHECK (deleted_count >= 0),
    duplicate_count     integer NOT NULL DEFAULT 0
        CHECK (duplicate_count >= 0),
    duration_ms         integer
        CHECK (duration_ms IS NULL OR duration_ms >= 0),
    error_code          text,
    error_detail        text,
    started_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz,

    CHECK (jsonb_typeof(cursor_before) = 'object'),
    CHECK (jsonb_typeof(cursor_after) = 'object'),
    CHECK (
        (status = 'running' AND completed_at IS NULL)
        OR (status <> 'running' AND completed_at IS NOT NULL)
    ),
    CHECK (range_ended_at IS NULL OR range_started_at IS NULL OR range_ended_at >= range_started_at)
);

CREATE INDEX idx_health_sync_runs_conn_resource_started
    ON mediator.health_sync_runs (connection_id, resource_type, started_at DESC);

CREATE INDEX idx_health_sync_runs_completed_lookup
    ON mediator.health_sync_runs (connection_id, resource_type, completed_at DESC)
    WHERE status = 'completed';

-- ===========================================================================
-- 4. mediator.health_webhook_receipts
-- ===========================================================================

CREATE TABLE mediator.health_webhook_receipts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id       uuid
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id             uuid
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    provider            text NOT NULL
        CHECK (provider IN ('withings')),
    provider_user_id    text NOT NULL
        CHECK (length(btrim(provider_user_id)) > 0),
    resource_type       text NOT NULL
        CHECK (resource_type IN ('measurement', 'workout', 'sleep')),
    payload_hash        text NOT NULL
        CHECK (length(btrim(payload_hash)) > 0),
    content_type        text,
    status              text NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'deduplicated', 'queued', 'ignored', 'rejected')),
    error_code          text,
    note                text,
    received_at         timestamptz NOT NULL DEFAULT now(),
    processed_at        timestamptz,

    UNIQUE (provider, payload_hash)
);

CREATE INDEX idx_health_webhook_receipts_conn_received
    ON mediator.health_webhook_receipts (connection_id, received_at DESC);

CREATE INDEX idx_health_webhook_receipts_provider_resource_received
    ON mediator.health_webhook_receipts (provider, resource_type, received_at DESC);

-- ===========================================================================
-- 5. mediator.health_dirty_categories
-- ===========================================================================

CREATE TABLE mediator.health_dirty_categories (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    connection_id       uuid NOT NULL
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    provider            text NOT NULL
        CHECK (provider IN ('withings')),
    resource_type       text NOT NULL
        CHECK (resource_type IN ('measurement', 'workout', 'sleep')),
    reason              text NOT NULL DEFAULT 'webhook'
        CHECK (reason IN ('webhook', 'manual', 'reconcile', 'initial_backfill')),
    source_receipt_id   uuid
        REFERENCES mediator.health_webhook_receipts(id) ON DELETE SET NULL,
    last_sync_run_id    uuid
        REFERENCES mediator.health_sync_runs(id) ON DELETE SET NULL,
    attempts            integer NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    marked_at           timestamptz NOT NULL DEFAULT now(),
    claimed_at          timestamptz,
    claimed_by          text,
    cleared_at          timestamptz
);

CREATE UNIQUE INDEX idx_health_dirty_categories_open_unique
    ON mediator.health_dirty_categories (connection_id, resource_type)
    WHERE cleared_at IS NULL;

CREATE INDEX idx_health_dirty_categories_open_claim
    ON mediator.health_dirty_categories (claimed_at, marked_at)
    WHERE cleared_at IS NULL;

CREATE INDEX idx_health_dirty_categories_open_marked
    ON mediator.health_dirty_categories (connection_id, resource_type, marked_at)
    WHERE cleared_at IS NULL;

-- ===========================================================================
-- 6. mediator.health_normalized_measurements
-- ===========================================================================

CREATE TABLE mediator.health_normalized_measurements (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_record_id    uuid NOT NULL UNIQUE
        REFERENCES mediator.health_source_records(id) ON DELETE CASCADE,
    connection_id       uuid NOT NULL
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    metric              text NOT NULL
        CHECK (length(btrim(metric)) > 0),
    measured_at         timestamptz NOT NULL,
    value_numeric       numeric NOT NULL,
    canonical_unit      text NOT NULL
        CHECK (length(btrim(canonical_unit)) > 0),
    source_unit         text,
    source_device_id    text,
    source_device_model text,
    attribution         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CHECK (jsonb_typeof(attribution) = 'object')
);

CREATE INDEX idx_health_normalized_measurements_user_metric_measured
    ON mediator.health_normalized_measurements (user_id, metric, measured_at DESC);

-- ===========================================================================
-- 7. mediator.health_normalized_workouts
-- ===========================================================================

CREATE TABLE mediator.health_normalized_workouts (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_record_id            uuid NOT NULL UNIQUE
        REFERENCES mediator.health_source_records(id) ON DELETE CASCADE,
    connection_id               uuid NOT NULL
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id                     uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    started_at                  timestamptz NOT NULL,
    ended_at                    timestamptz,
    local_timezone              text,
    local_offset_seconds        integer,
    workout_type                text NOT NULL
        CHECK (length(btrim(workout_type)) > 0),
    duration_seconds            integer
        CHECK (duration_seconds IS NULL OR duration_seconds >= 0),
    pause_duration_seconds      integer
        CHECK (pause_duration_seconds IS NULL OR pause_duration_seconds >= 0),
    distance_meters             numeric,
    steps                       integer
        CHECK (steps IS NULL OR steps >= 0),
    energy_kcal                 numeric,
    elevation_gain_meters       numeric,
    average_heart_rate_bpm      numeric,
    max_heart_rate_bpm          numeric,
    source_device_id            text,
    source_device_model         text,
    attribution                 jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CHECK (jsonb_typeof(attribution) = 'object'),
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX idx_health_normalized_workouts_user_started
    ON mediator.health_normalized_workouts (user_id, started_at DESC);

-- ===========================================================================
-- 8. mediator.health_normalized_sleep
-- ===========================================================================

CREATE TABLE mediator.health_normalized_sleep (
    id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_record_id                uuid NOT NULL UNIQUE
        REFERENCES mediator.health_source_records(id) ON DELETE CASCADE,
    connection_id                   uuid NOT NULL
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id                         uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    started_at                      timestamptz NOT NULL,
    ended_at                        timestamptz NOT NULL,
    local_sleep_date                date NOT NULL,
    local_timezone                  text,
    local_offset_seconds            integer,
    completeness_state              text NOT NULL DEFAULT 'partial'
        CHECK (completeness_state IN ('partial', 'complete', 'revised')),
    total_in_bed_seconds            integer
        CHECK (total_in_bed_seconds IS NULL OR total_in_bed_seconds >= 0),
    total_asleep_seconds            integer
        CHECK (total_asleep_seconds IS NULL OR total_asleep_seconds >= 0),
    awake_seconds                   integer
        CHECK (awake_seconds IS NULL OR awake_seconds >= 0),
    light_sleep_seconds             integer
        CHECK (light_sleep_seconds IS NULL OR light_sleep_seconds >= 0),
    deep_sleep_seconds              integer
        CHECK (deep_sleep_seconds IS NULL OR deep_sleep_seconds >= 0),
    rem_sleep_seconds               integer
        CHECK (rem_sleep_seconds IS NULL OR rem_sleep_seconds >= 0),
    sleep_latency_seconds           integer
        CHECK (sleep_latency_seconds IS NULL OR sleep_latency_seconds >= 0),
    wake_after_sleep_onset_seconds  integer
        CHECK (wake_after_sleep_onset_seconds IS NULL OR wake_after_sleep_onset_seconds >= 0),
    wakeups                         integer
        CHECK (wakeups IS NULL OR wakeups >= 0),
    sleep_score                     integer
        CHECK (sleep_score IS NULL OR (sleep_score >= 0 AND sleep_score <= 100)),
    source_device_id                text,
    source_device_model             text,
    attribution                     jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at                      timestamptz NOT NULL DEFAULT now(),
    updated_at                      timestamptz NOT NULL DEFAULT now(),

    CHECK (jsonb_typeof(attribution) = 'object'),
    CHECK (ended_at >= started_at)
);

CREATE INDEX idx_health_normalized_sleep_user_local_date
    ON mediator.health_normalized_sleep (user_id, local_sleep_date DESC);

-- ===========================================================================
-- 9. mediator.health_source_to_event_projections
-- ===========================================================================

CREATE TABLE mediator.health_source_to_event_projections (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_record_id    uuid NOT NULL UNIQUE
        REFERENCES mediator.health_source_records(id) ON DELETE CASCADE,
    connection_id       uuid NOT NULL
        REFERENCES mediator.health_connections(id) ON DELETE CASCADE,
    user_id             uuid NOT NULL
        REFERENCES mediator.users(id) ON DELETE CASCADE,
    event_id            uuid
        REFERENCES mediator.events(id) ON DELETE SET NULL,
    commitment_id       uuid
        REFERENCES mediator.commitments(id) ON DELETE SET NULL,
    projection_version  integer NOT NULL DEFAULT 1
        CHECK (projection_version >= 1),
    projection_status   text NOT NULL DEFAULT 'pending'
        CHECK (projection_status IN ('pending', 'projected', 'superseded', 'removed')),
    match_rule          text,
    note                text,
    projected_at        timestamptz,
    removed_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CHECK (projection_status <> 'removed' OR removed_at IS NOT NULL)
);

CREATE INDEX idx_health_source_to_event_projections_event
    ON mediator.health_source_to_event_projections (event_id)
    WHERE event_id IS NOT NULL;

CREATE INDEX idx_health_source_to_event_projections_commitment
    ON mediator.health_source_to_event_projections (commitment_id)
    WHERE commitment_id IS NOT NULL;

-- ===========================================================================
-- 10. RLS posture — strict private-table defaults on every health table
-- ===========================================================================

ALTER TABLE mediator.health_connections ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_connections FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_connections FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_connections ON mediator.health_connections
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_connections ON mediator.health_connections
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_source_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_source_records FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_source_records FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_source_records ON mediator.health_source_records
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_source_records ON mediator.health_source_records
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_sync_runs FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_sync_runs FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_sync_runs ON mediator.health_sync_runs
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_sync_runs ON mediator.health_sync_runs
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_webhook_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_webhook_receipts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_webhook_receipts FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_webhook_receipts ON mediator.health_webhook_receipts
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_webhook_receipts ON mediator.health_webhook_receipts
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_dirty_categories ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_dirty_categories FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_dirty_categories FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_dirty_categories ON mediator.health_dirty_categories
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_dirty_categories ON mediator.health_dirty_categories
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_normalized_measurements ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_normalized_measurements FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_normalized_measurements FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_normalized_measurements ON mediator.health_normalized_measurements
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_normalized_measurements ON mediator.health_normalized_measurements
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_normalized_workouts ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_normalized_workouts FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_normalized_workouts FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_normalized_workouts ON mediator.health_normalized_workouts
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_normalized_workouts ON mediator.health_normalized_workouts
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_normalized_sleep ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_normalized_sleep FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_normalized_sleep FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_normalized_sleep ON mediator.health_normalized_sleep
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_normalized_sleep ON mediator.health_normalized_sleep
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

ALTER TABLE mediator.health_source_to_event_projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE mediator.health_source_to_event_projections FORCE ROW LEVEL SECURITY;
REVOKE ALL ON TABLE mediator.health_source_to_event_projections FROM anon, authenticated;

DO $$
BEGIN
    CREATE POLICY deny_anon_health_source_to_event_projections ON mediator.health_source_to_event_projections
        FOR ALL TO anon, authenticated USING (false) WITH CHECK (false);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    CREATE POLICY owner_scoped_health_source_to_event_projections ON mediator.health_source_to_event_projections
        FOR ALL
        USING (user_id = auth.uid())
        WITH CHECK (user_id = auth.uid());
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

COMMIT;
