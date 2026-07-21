# Health Sync — Rollout Runbook

**Purpose:** Step-by-step operator guide for enabling Withings health sync in
production. This runbook captures settled configuration decisions, the required
enable order, migration verification, live smoke tests, canaries, export/delete
procedures, operator checks, alerts, rollback, and limitations.

**Last updated:** 2026-07-21

---

## 1. Prerequisites

All prerequisites are **operator or vendor actions** that must be completed
before any flag is toggled:

| # | Prerequisite | Gap ID | Responsible |
|---|---|---|---|
| 1 | Provision `DATA_ENCRYPTION_KEY` (base64, 32 bytes) | L-001 | Operator |
| 2 | Provision `WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET` | L-002 | Operator |
| 3 | Register HTTPS callback URL at exact path `/api/health/devices/withings/oauth/callback` | L-003 | Operator |
| 4 | Expose HTTPS notification endpoint at `/api/health/devices/withings/notifications` | L-004 | Operator |
| 5 | Obtain live Withings API entitlement & vendor approval | L-005 | Vendor |
| 6 | Verify notification subscribe flow against approved app | L-006 | Live validation |

**Do not proceed past this section until all prerequisites are satisfied.**

---

## 2. Migration Verification

All health-sync database tables must exist and have RLS (Row-Level Security)
enforced before any flag is toggled. The following migrations are required:

| Migration | Tables Created | Test Selector |
|---|---|---|
| `0063_health_provider_foundation` | `health_connections`, `health_source_records`, `health_sync_runs`, `health_webhook_receipts`, `health_dirty_categories`, `health_normalized_measurements`, `health_normalized_workouts`, `health_normalized_sleep`, `health_source_to_event_projections` | `test_0063_up_creates_all_health_tables_in_one_transaction` |
| `0064_health_measurement_fan_out` | (alter only — composite UNIQUE on measurements) | `test_0064_up_replaces_single_unique_with_composite` |
| `0065_workout_projection_contract` | No new tables — constraint and FK additions | (see `test_health_migration.py`) |

### Verification Steps

1. **Confirm migrations applied:** Run the postgres smoke test (`pytest tests/test_postgres_smoke.py -v -m postgres`) and verify all migrations counted.
2. **Confirm health tables exist:** Query `pg_tables` in the `mediator` schema for all 9 health table names listed above.
3. **Confirm RLS enforced:** Every health table must have `rowsecurity` and `forcerowsecurity` enabled. The migration test (`test_0063_apply_and_rollback_catalog_surface`) validates this at the catalog level.
4. **Confirm deny-anon policies:** Every health table must have a `deny_anon_<table>` policy and an `owner_scoped_<table>` policy. No privileges granted to `anon` or `authenticated` roles.
5. **Confirm composite UNIQUE on measurements:** The `health_normalized_measurements` table must use `UNIQUE (source_record_id, metric)`, not single-column `UNIQUE (source_record_id)`. Verify with `test_0064_measurement_fan_out_and_sleep_uniqueness`.

### Automated Migration Validation

The health migration test suite (`tests/test_health_migration.py`, 14+ tests) validates:
- Forward/backward migration pair existence and numbering
- Table creation in transaction, column contracts, constraints, and indexes
- RLS posture (enabled, forced, deny-anon, owner-scoped) on every table
- Unique constraint semantics (fan-out for measurements, 1:1 for sleep)
- Down migration drops policies before tables in reverse dependency order
- Real catalog surface via `postgres` marker (apply → verify RLS/policies/privileges → rollback)

These tests can be run locally with a real database (`TEST_DATABASE_URL`), or relied upon via CI. They do **not** require Withings credentials.

---

## 3. Configuration Reference

All flags are **disabled by default** (`False`). The existing config test
(`tests/test_health_config.py::test_health_sync_defaults_off_and_starts_without_provider_secrets`)
proves that every health and projection flag defaults to `False`, and that
`Settings()` constructs successfully without any Withings credentials or
encryption key. This is contract-tested in CI and must not regress.

### 3.1 Feature Flags

| Env Var | Default | Description |
|---|---|---|
| `HEALTH_SYNC_ENABLED` | `false` | Master gate — when off, all health routes return 503 |
| `HEALTH_SYNC_MEASUREMENTS_ENABLED` | `false` | Weight, body composition, heart-rate category sync |
| `HEALTH_SYNC_WORKOUTS_ENABLED` | `false` | Workout/activity category sync |
| `HEALTH_SYNC_SLEEP_ENABLED` | `false` | Sleep summary category sync |
| `HEALTH_WORKOUT_PROJECTION_ENABLED` | `false` | Project imported workouts to explicit commitments |
| `HEALTH_WEEKLY_SUMMARY_ENABLED` | `false` | Weekly digest generator (pure read-only, no side effects) |

### 3.2 Provider Credentials

| Env Var | Default | Required When |
|---|---|---|
| `DATA_ENCRYPTION_KEY` | (unset) | `HEALTH_SYNC_ENABLED=true` |
| `WITHINGS_CLIENT_ID` | (unset) | `HEALTH_SYNC_ENABLED=true` |
| `WITHINGS_CLIENT_SECRET` | (unset) | `HEALTH_SYNC_ENABLED=true` |
| `WITHINGS_CALLBACK_URL` | `""` | `HEALTH_SYNC_ENABLED=true` |

The callback URL must be an absolute HTTPS URL with the exact path
`/api/health/devices/withings/oauth/callback` and no query string or fragment.
This is enforced at config load time (`test_health_sync_enabled_requires_exact_callback_url`).

### 3.3 Operational Tuning

| Env Var | Default | Range | Description |
|---|---|---|---|
| `HEALTH_SYNC_POLL_INTERVAL_S` | `30.0` | `(0, 3600]` | Seconds between worker poll cycles |
| `HEALTH_SYNC_BATCH_SIZE` | `25` | `[1, 500]` | Max records per Withings API page |
| `HEALTH_SYNC_REQUEST_TIMEOUT_S` | `10.0` | `(0, 60]` | HTTP timeout per Withings API call |
| `HEALTH_SYNC_MAX_ATTEMPTS` | `3` | `[1, 10]` | Max retry attempts per dirty category |
| `HEALTH_SYNC_RETRY_AFTER_CAP_SECONDS` | `30` | `[0, 300]` | If Withings `Retry-After` exceeds this, fail immediately (rate-limit cap) |
| `HEALTH_SYNC_RECONCILIATION_INTERVAL_S` | `900.0` | `(0, 86400]` | Seconds between full reconciliation sweeps |

---

## 4. Callback & Webhook Verification

Before enabling any flags, verify that the OAuth callback and webhook
notification endpoints are reachable and correctly configured.

### 4.1 OAuth Callback

1. **HEAD request:** `HEAD /api/health/devices/withings/oauth/callback` must return HTTP 200.
2. **Exact path match:** The callback URL in Withings' developer portal must match `WITHINGS_CALLBACK_URL` exactly — no trailing slash, no query params.
3. **HTTPS only:** Withings rejects plain HTTP callbacks. The callback URL must use `https://`.

### 4.2 Webhook Notifications

1. **HEAD request:** `HEAD /api/health/devices/withings/notifications` must return HTTP 200.
2. **Notification intake is queue-only:** The notification handler marks dirty categories for the matching connection's resource types. It does **not** perform inline data fetches. This is validated by `test_health_notifications.py`.
3. **Deduplication:** Incoming notifications are deduplicated by SHA-256 hash of canonicalized form fields. Duplicate notifications are logged as ignored receipts.
4. **Subscribe flow:** After flags are enabled (section 5), trigger the subscribe flow from Withings' developer portal and confirm the notification endpoint receives a verification challenge.

---

## 5. Settled Enable Order

The enable order follows the **reverse of the documented rollback order**.
Enable flags in this exact sequence, validating each step before proceeding:

### Step 1 — Master Connection Flag

```
HEALTH_SYNC_ENABLED=true
```

**Verification:** Deploy. Confirm the app starts (no `ValidationError` —
all required secrets must be present). Verify `/admin/health` shows the
connection flag as enabled but no connections exist yet. Confirm
`/api/health/devices/withings/*` returns 503 when no connection is
present for the user (not a configuration error).

### Step 2 — Per-Category Flags

Enable only the categories you have tested and are ready to support:

```
HEALTH_SYNC_MEASUREMENTS_ENABLED=true   # weight, body composition
HEALTH_SYNC_WORKOUTS_ENABLED=true       # activity/workout import
HEALTH_SYNC_SLEEP_ENABLED=true          # sleep summaries
```

**Verification:** For each category enabled, perform an OAuth connect
flow with a test Withings account. Verify dirty categories are marked
(`/api/health/devices/withings/resync`). Confirm the worker picks up
dirty categories and syncs data. Validate normalized rows appear in the
database. Run the offline synthetic canaries
(`tests/test_health_synthetic_canary.py`) against a staging database.

### Step 3 — Workout Projection Flag

```
HEALTH_WORKOUT_PROJECTION_ENABLED=true
```

**Verification:** After workouts have been synced (step 2), verify that
workout projection creates exactly one adherence event per compatible
explicit commitment. Confirm projection ledger rows are created and
linked. Validate that manual adherence events are untouched. Run
`tests/test_health_synthetic_canary.py` workout canaries in staging.

### Step 4 — Data-Handling Steps

Only after all flags are verified:

- Confirm `/api/health/devices/withings/export` returns complete
  user data with no token/OAuth/raw-payload leakage.
- Confirm `/api/health/devices/withings/disconnect` performs best-effort
  revoke and complete local cleanup.
- Enable webhook notifications (subscribe flow) and confirm
  notification intake creates dirty categories without inline fetch.

---

## 6. Canary Verification

The offline synthetic canary suite (`tests/test_health_synthetic_canary.py`)
provides end-to-end confidence that the full pipeline works from fake OAuth
through to read models. All canaries use `FakeWithingsProvider` and `FakePool`
only — no live network calls, tokens, or provider secrets are required.

### 6.1 Weight Canary (5 tests, `TestWeightSyntheticCanary`)

- Fake OAuth → reconciliation → sync produces a weigh-in in `health_normalized_measurements`
- `get_weight()` read model returns correct latest weight and trends
- `get_weight_trend` health read tool returns valid output via `TurnContext`
- Empty/no-data user returns empty results
- User scoping: other user's weight is not returned

### 6.2 Sleep Canary (6 tests, `TestSleepSyntheticCanary`)

- Late sleep revision (incomplete → completed) updates the rolling summary without stale duplicate rows
- `get_sleep_rolling_7d()` returns updated data after revision
- Tombstone deletes normalized sleep rows
- Cross-midnight sleep sessions
- User scoping: other user's sleep is not returned
- Empty/no-data user returns empty results

### 6.3 Workout Canary (7 tests, `TestWorkoutSyntheticCanary`)

- Workout sync produces normalized rows in `health_normalized_workouts`
- Exactly-once projection to one compatible Hector fitness commitment (one event + one ledger row)
- Idempotent replay returns existing projection with no duplicates
- Tombstone reverses projection (removes event, marks projection `removed`, detaches event link)
- Manual events survive tombstone unscathed
- Cross-user isolation: `find_active_projection` and tombstone are user-scoped
- Geometry of projection pool stays clean after full create → replay → tombstone cycle

### Running Canaries

```bash
# All synthetic canaries (18 tests)
pytest tests/test_health_synthetic_canary.py -v

# Weight only
pytest tests/test_health_synthetic_canary.py::TestWeightSyntheticCanary -v

# Sleep only
pytest tests/test_health_synthetic_canary.py::TestSleepSyntheticCanary -v

# Workout only
pytest tests/test_health_synthetic_canary.py::TestWorkoutSyntheticCanary -v
```

Run these against a staging database after each enable step to validate the
pipeline is wired correctly. They are fast, offline, and safe to run repeatedly.

---

## 7. Live Smoke Tests

After flags are enabled and canaries pass, execute these live smoke tests with a
real test Withings account:

### 7.1 Connect Flow

1. `POST /api/health/devices/withings/connect` with JSON such as
   `{"redirect_uri":"https://<app>/settings/health","resource_types":["measurement","workout","sleep"]}` — returns a Withings OAuth authorization URL.
2. Follow the URL in a browser, authorize the test account.
3. Confirm the callback redirects back to the app and the connection appears in the database with `status='active'`.
4. `GET /api/health/devices/withings/status` — returns
   `feature_enabled: true` and `connection.status: "active"` plus metadata
   (no tokens, no external user ID, no device IDs, no health values).

### 7.2 Sync Flow

1. `POST /api/health/devices/withings/resync` — marks dirty categories for enabled resource types. Response is metadata-only (no cursor state, no tokens, no raw payloads).
2. Wait for the worker to pick up the dirty categories (check `/admin/health` for last sync times).
3. Confirm normalized rows appear in the database for each enabled category.
4. Verify `/admin/health` shows connection status as `fresh`.

### 7.3 Projection Flow (if `HEALTH_WORKOUT_PROJECTION_ENABLED=true`)

1. Ensure a test Hector user has at least one explicit fitness commitment.
2. After workout sync, confirm a projection ledger row is created and linked to an adherence event.
3. Verify manual adherence events for the same commitment are untouched.

### 7.4 Disconnect/Delete Flow

1. `DELETE /api/health/devices/withings` — performs best-effort revoke + complete local cleanup.
2. Confirm all local data is removed (source records, normalized rows, dirty categories, webhook receipts, projection ledger, projection-owned events).
3. Confirm manual adherence events survive.
4. Confirm another user's data is untouched.
5. `GET /api/health/devices/withings/status` — returns
   `connection.status: "not_connected"`.

### 7.5 Export Flow

1. `GET /api/health/devices/withings/export` — returns JSON with:
   - Connection metadata (id, provider, status, timestamps — no tokens)
   - Source-record provenance metadata
   - Normalized measurement/sleep/workout rows (including health values — this is the explicit authenticated export surface)
   - Projection ledger rows
   - Deletion/tombstone state
2. Confirm the response excludes: encrypted tokens, OAuth state, raw provider payloads, webhook form payloads, cursor state, external user IDs, and cross-user rows.

---

## 8. Export & Data Portability

The authenticated export endpoint (`GET /api/health/devices/withings/export`)
returns the current user's Withings-scoped data for portability and audit.

### Included

- **Connection metadata:** `id`, `provider`, `status`, `granted_scopes`, `last_success_at`, `last_failure_at`, `created_at`, `deleted_at`
- **Source records:** `id`, `resource_type`, `external_id`, `provider_revision`, `is_deleted`, `attribution` (provenance metadata — no raw payloads)
- **Normalized measurements:** `metric`, `value_numeric`, `canonical_unit`, `measured_at`
- **Normalized sleep:** `started_at`, `ended_at`, `local_sleep_date`, `deep_sleep_duration_s`, `rem_sleep_duration_s`, `light_sleep_duration_s`, `wake_count`, `completeness`, `revision_count`
- **Normalized workouts:** `local_date`, `hector_workout_type`, `duration_s`, `calories`, `distance_m`, `heart_rate_avg`, `heart_rate_max`, `steps`, `elevation_gain_m`, `pool_laps`
- **Projection ledger:** `source_record_id`, `version`, `event_id`, `commitment_id`, `projection_status`, `projection_reason`
- **Dirty categories:** `resource_type`, `reason`, `attempts`, `marked_at`

### Explicitly Excluded

- `access_token_encrypted`, `refresh_token_encrypted`, `access_token_expires_at`, `refresh_token_expires_at`, `refresh_token_rotated_at`
- `cursor_state`, `external_user_id`
- Raw provider payloads, webhook form payloads, OAuth state
- Rows from other users

### Verification

The export test suite (`tests/test_health_export.py`, 14+ tests) validates:
- Authenticated access control (401 when unauthenticated)
- Token/cursor/external-id exclusion
- Cross-user isolation
- Complete data shape for all expected tables
- Health values are present (this is the explicit exception surface)

---

## 9. Delete & Local Cleanup

The delete endpoint (`DELETE /api/health/devices/withings`) performs a
complete local cleanup of the authenticated user's Withings connection data.

### What Gets Deleted

| Data | Table | Scope |
|---|---|---|
| Connection metadata (marked deleted, tokens cleared) | `health_connections` | `connection_id` + `user_id` |
| Source records | `health_source_records` | `connection_id` + `user_id` |
| Normalized measurements | `health_normalized_measurements` | `connection_id` + `user_id` |
| Normalized sleep | `health_normalized_sleep` | `connection_id` + `user_id` |
| Normalized workouts | `health_normalized_workouts` | `connection_id` + `user_id` |
| Dirty categories | `health_dirty_categories` | `connection_id` + `user_id` |
| Webhook receipts | `health_webhook_receipts` | `connection_id` + `user_id` |
| Projection ledger rows | `health_source_to_event_projections` | `connection_id` + `user_id` |
| Projection-owned adherence events | `events` | ledger-subquery-scoped |

### What Survives

- **Manual adherence events:** Events created by the user (not via projection) are never deleted.
- **Other users' data:** All operations are double-scoped by `connection_id` and `user_id`.
- **Other connections:** Only the specific Withings connection is deleted.

### Best-Effort Revoke

Before local teardown, the endpoint attempts a best-effort revoke call to
Withings. If the revoke fails (e.g., token already expired, network error),
local cleanup still proceeds. This is gap L-010 in the proof-map — the live
Withings `revoke` endpoint integration requires an external user identifier
not yet available.

### Verification

The export-delete test suite (`tests/test_health_export_delete.py`, 11 tests) validates:
- Auth required (401 unauthenticated)
- 503 when `HEALTH_SYNC_ENABLED=false`
- 404 when no connection exists
- Metadata-only response
- All health/projection records removed
- Projection-owned events deleted
- Manual events survive
- Cross-user deletion rejected
- Token clearing
- Idempotency (repeated delete is safe)
- Other user's data preserved

---

## 10. Operator Health Checks

### 10.1 Admin Diagnostics Page

`/admin/health` (HTTP Basic Auth, HTML/Pico CSS) provides a metadata-only
operator dashboard. It follows existing admin conventions.

**Displayed:**
- Config flags (enabled/disabled status for all health env vars)
- Connection list: `connection_id`, `user_id`, `provider`, `status`, timestamps (`last_success_at`, `last_failure_at`, `created_at`), stale classification (`fresh`/`stale`/`never_synced`)
- Summary counts: total connections, active connections, stale connections, never-synced connections
- Best-effort sync run and projection totals (graceful `FakePool` fallback)

**Explicitly excluded:**
- Health values (weight, sleep duration, workout metrics, etc.)
- Tokens (access, refresh, encrypted or not)
- `external_user_id`, `cursor_state`, `device_ids`
- OAuth timestamps, raw payloads

### 10.2 Metrics to Monitor

All health metrics use the existing log-based `app/services/metrics.py` layer
with sanitized labels only (provider, resource_type, status, error_kind,
retryable). No health values, tokens, user IDs, or device IDs appear in metric
labels.

| Metric | Labels | What It Signals |
|---|---|---|
| `health_sync_attempts_started` | `provider`, `resource_type` | Sync attempt started |
| `health_sync_attempts_completed` | `provider`, `resource_type`, `status` | Sync succeeded / failed |
| `health_sync_duration_seconds` | `provider`, `resource_type` | Sync wall-clock duration (seconds) |
| `health_sync_records_fetched` | `provider`, `resource_type` | Records fetched from provider |
| `health_sync_records_deleted` | `provider`, `resource_type` | Tombstone records processed |
| `health_sync_retry` | `provider`, `resource_type`, `retryable` | Retry event emitted |
| `health_sync_cursor_errors` | `provider`, `resource_type`, `error_kind` | Cursor state error |
| `health_sync_stale_freshness` | `provider`, `resource_type` | Connection classified as stale |
| `health_sync_projection_outcome` | `provider`, `resource_type`, `status` | Projection matched / no_match / removed |
| `health_sync_worker_claimed` | `provider` | Connections claimed in a worker scan cycle |
| `health_sync_worker_synced` | `provider` | Connections synced in a worker scan cycle |
| `health_sync_worker_failed` | `provider` | Connections that failed in a worker scan cycle |
| `health_sync_worker_skipped_disabled` | `provider` | Connections skipped (disabled) in a scan cycle |
| `health_sync_worker_reconciliation_outcomes` | `provider` | Reconciliation outcomes in a scan cycle |
| `health_sync_worker_skipped_connections` | `provider` | Connections skipped in a scan cycle |
| `health_sync_worker_scanned_connections` | `provider` | Connections scanned in a scan cycle |

These names match the metric constants in
`app/services/health_sync/metrics.py` exactly. The single `record_worker_scan`
helper emits the seven `health_sync_worker_*` gauges above (it is not one
metric).

**Alerting guidance:**
- `health_sync_outcome{status="permanent_failure"}`: investigate immediately — may indicate reauthorization required or API entitlement issue.
- `health_sync_stale_freshness`: non-zero on enabled categories indicates >24h without successful sync.
- `health_sync_retry{retryable="true"}`: high rates may indicate rate-limiting; check `Retry-After` values and adjust `HEALTH_SYNC_RETRY_AFTER_CAP_SECONDS` if needed.

### 10.3 Privacy Surface Check

Run the privacy surface regression suite before any production deployment:

```bash
pytest tests/test_health_privacy_surfaces.py -v
```

This 42-test suite scans route responses, admin HTML, metric logs, tool
descriptions, hot context, and prompt fixtures for token/secret/health-value
leakage. All must pass.

---

## 11. Stale Threshold Policy

### Default Alert Gate: 24 Hours

The connection freshness read model (`get_connection_freshness`)
classifies a connection as **stale** when `last_success_at` is more than
24 hours in the past or has never been set. This is the default alert
gate for enabled connected categories.

### Classification

| Condition | Classification |
|---|---|
| `last_success_at` within last 24 hours | `fresh` |
| `last_success_at` older than 24 hours | `stale` |
| `last_success_at` is `NULL` (never synced) | `stale` (class: `never_synced`) |

### Policy

- **Enabled categories with stale data for >24h**: operator should
  investigate. Check worker logs for errors, rate-limit responses, or
  reauthorization-required states.
- **Sub-threshold (<24h)**: not yet justified by test evidence. The
  24-hour gate is the settled default; any tighter threshold requires
  documented test evidence and a plan update.
- **No alert on disabled categories**: if a category flag is `false`,
  staleness is expected and should not generate alerts.
- This is documented in the proof-map as gap L-009 (procedural).

### Monitoring

The `health_sync_stale_freshness` metric (emitted via
`app/services/health_sync/metrics.py`) records stale classifications
with `provider` and `resource_type` labels. Monitor this metric for any
non-zero counts on **enabled** categories.

---

## 12. Failure Drill Reference

The failure drill suite (`tests/test_health_failure_drills.py`, 27 tests)
covers the following scenarios. Operators should be familiar with these
failure modes and their expected system behavior:

| Scenario | Expected Behavior | Test Class |
|---|---|---|
| Stale freshness classification | Never synced → stale; successful sync → fresh; old sync → stale; failed sync doesn't update freshness; user-scoped | `TestStaleFreshness` |
| Reauthorization required | Permanent failure recorded; existing records preserved; connection marked `reauth_required` | `TestReauthorizationRequired` |
| Rate-limiting | Below cap: retries up to `HEALTH_SYNC_MAX_ATTEMPTS`; above cap: fails immediately | `TestRateLimitRetryAfter` |
| Webhook without fetch | Transient failure → retry succeeds; records synced after recovery; duplicate webhook deduplicated | `TestWebhookWithoutFetch` |
| Duplicate records | Duplicate source record ignored; existing normalized rows preserved; no double-counting | `TestDuplicateRecords` |
| Cursor crash rollback | Transaction rolled back; cursor not advanced; dirty category remains for retry | `TestCursorCrashTransactionRollback` |
| Projection drift | Revision supersedes; rematch to different commitment; tombstone cleanup; manual events survive all | `TestProjectionDrift` |

These drills can be run offline against staging:

```bash
pytest tests/test_health_failure_drills.py -v
```

---

## 13. Rollback

If any category exhibits data corruption, unexpected projection
behavior, or excessive API errors, roll back in this order:

1. **Projection flag off:** `HEALTH_WORKOUT_PROJECTION_ENABLED=false`
   (stops new projections; existing events remain)
2. **Category flags off:** set affected category flags to `false`
   (stops sync for that category; existing rows remain)
3. **Connection flag off:** `HEALTH_SYNC_ENABLED=false`
   (all health routes return 503; worker stops)
4. **Data-handling steps:** if needed, use the admin surface to review
   connection state, or run `DELETE /api/health/devices/withings` for
   individual users to trigger complete local cleanup.

---

## 14. Limitations & Pending Items

### Automated Test Gaps

| Gap ID | Description | Severity |
|---|---|---|
| G-001 | Missing `value` or `unit` fields raise `KeyError` (defensive — model dataclass enforces required fields at construction) | Low |
| G-002 | Route `HEAD` endpoint contract test (covered implicitly by route registration) | Low |

### Pending Live Validation

| Gap ID | Prerequisite | Blocks Rollout? | Responsible |
|---|---|---|---|
| L-001 | `DATA_ENCRYPTION_KEY` provisioned | **Yes** | Operator |
| L-002 | `WITHINGS_CLIENT_ID` / `WITHINGS_CLIENT_SECRET` provisioned | **Yes** | Operator |
| L-003 | HTTPS callback URL registered at exact endpoint | **Yes** | Operator |
| L-004 | HTTPS notification endpoint exposed | **Yes** | Operator |
| L-005 | Live Withings API entitlement & vendor approval | **Yes** | Vendor |
| L-006 | Notification subscribe flow verified | **Yes** | Live validation |
| L-007 | All health flags off until credentials ready | Procedural | Operator |
| L-008 | `HEALTH_WORKOUT_PROJECTION_ENABLED` must stay `False` until vendor live | Procedural | Operator |
| L-009 | 24-hour staleness threshold policy | Low | Operator |
| L-010 | Live Withings `revoke` endpoint integration | Low | Pending external user identifier |

### Deferred Items

| Item | Status | Reference |
|---|---|---|
| Weekly digest generator | Implemented as pure default-off generator (`HEALTH_WEEKLY_SUMMARY_ENABLED=false`); no scheduler/prompt integration yet | Plan task T14 |
| Full production dogfood | Not completed — this runbook enables staged rollout only | Proof-map §4.2 |
| Sub-24h staleness threshold | Not justified by test evidence | Proof-map L-009 |

---

## 15. Evidence Ledger

This runbook is traceable to the release-readiness proof-map
(`docs/health/release-readiness-proof.md`) and the three upstream handoff
contracts:

- `docs/health/withings-provider-contract.md` — OAuth, provider interface, cursor, capabilities, notifications
- `docs/health/weight-sleep-read-model-contract.md` — measurement decode, sleep normalization, read models, repository
- `docs/health/workout-projection-contract.md` — workout normalization, pure matcher, applicator, projection lifecycle

All automated evidence in the proof-map maps to CI-runnable test selectors.
Pending live validation items are clearly labeled and none are claimed as
complete. This runbook does **not** claim live rollout, production
enablement, vendor approval, legal review completion, or completed dogfood.
