# Withings Weight, Workout, and Sleep Integration Plan

Status: proposed implementation plan
Last updated: 2026-07-13

## Assumption and Goal

This plan assumes “WeThings” means **Withings**. Confirm the brand and exact
scale/watch models before implementation, because available body-composition,
workout, heart-rate, and sleep fields vary by device.

The goal is to let Hector automatically use three kinds of Withings data:

- scale measurements, beginning with body weight;
- watch activity and workout sessions;
- sleep sessions, nightly totals, quality indicators, and sleep stages when the
  connected device exposes them.

This should remain a conversational fitness tool. The first release does not
need to become a full health dashboard or provide medical interpretation.

## Product Decisions

1. Extend Hector and its existing fitness topic, commitments, events, and hot
   context. Do not create a second fitness system.
2. Treat Withings as a cloud integration. Users first sync their devices to the
   Withings app, then connect their Withings account to Pumpernickel with OAuth.
3. Store imported provider data in a dedicated, idempotent health-data layer.
   Do not write API responses directly into `mediator.events`.
4. Weight, workouts, and sleep are first-class normalized records with source
   provenance and revision history.
5. Device data never creates a commitment. A workout or sleep session may
   satisfy an existing commitment only through an explicit mapping or a safe,
   deterministic match.
6. Imported health data is private by default. Existing Hector partner-sharing
   consent does not implicitly authorize sharing weight, sleep, heart-rate, or
   workout details.
7. Hector discusses trends and adherence, not diagnoses. Body-composition and
   sleep-stage readings are wellness estimates whose precision depends on the
   device.

## Existing Foundation

The repository already contains most of the conversational layer:

- Hector is implemented in `app/bots/hector.py`.
- Generic commitments and events are defined in
  `migrations/0038_commitments_events.sql`.
- `app/services/adherence.py` calculates commitment adherence.
- `app/services/hot_context_solo.py` renders Hector's compact fitness context.
- `app/services/tools/read_tools.py` and `write_tools.py` expose fitness data to
  the agent.
- FastAPI, Postgres, a scheduled-job worker, JWT authentication, encrypted
  fields, and webhook patterns already exist.

The missing capability is a health-provider connection, reliable import and
reconciliation, normalized health read models, and user/operator surfaces.

## Recommended Architecture

```text
Withings devices
      |
      v
Withings mobile app/cloud
      |
      +-- OAuth connection ------------------------+
      |                                            |
      +-- webhook change hint --> sync queue -------+
                                                   v
                                      Withings adapter/fetcher
                                                   |
                                      idempotent source records
                                                   |
                          +------------------------+------------------+
                          v                        v                  v
                  body measurements           workouts        sleep sessions
                          |                        |                  |
                          +---------- derived summaries ------------+
                                                   |
                          optional, traceable commitment projections
                                                   |
                                                   v
                                      Hector hot context/tools
```

Webhooks should only signal that data changed. A background worker then fetches
the authoritative records, normalizes them, and advances a persisted cursor.
A nightly reconciliation run catches notifications that were delayed or lost.

## Data Model

Add a numbered forward migration containing the following tables. Use UUID
primary keys, timestamps, foreign keys, scoped indexes, FORCE RLS, and deny-anon
policies consistent with the existing private-table posture.

### `health_connections`

One row per user and provider account:

- `user_id`, `provider`, `external_user_id`, `status`;
- granted scopes and consent timestamps;
- encrypted access and rotating refresh tokens plus expiries;
- per-resource cursors;
- `last_success_at`, `last_error_at`, sanitized error code;
- connected, disconnected, revoked, and deletion timestamps.

Require `DATA_ENCRYPTION_KEY` whenever this integration is enabled. Serialize
token refresh per connection so concurrent syncs cannot lose a rotated refresh
token.

### `health_source_records`

The replayable import boundary:

- `connection_id`, `resource_type`, `external_id`;
- provider-created/updated timestamps and observed/start/end times;
- source device/model and attribution fields;
- payload hash, provider revision, import timestamp, and tombstone state;
- encrypted raw payload only if replay/debug needs justify retaining it.

Enforce uniqueness on `(connection_id, resource_type, external_id)`. If Withings
does not supply a stable ID for a resource, derive a documented provider-specific
key from stable source fields and cover it with fixture tests.

### `body_measurements`

Store one normalized metric per row, linked to a source record:

- `measured_at`, `metric` (`weight`, `fat_ratio`, `muscle_mass`, etc.);
- canonical value and unit (`kg`, `%`, or another documented canonical unit);
- display/source unit, device ID, and attribution metadata.

Start the user experience with weight. Keep other body-composition fields behind
capability flags until their model support and usefulness are confirmed.

### `workouts`

Store a session summary linked to a source record:

- start/end, timezone/offset, type, duration, pause duration;
- distance, steps, energy, elevation;
- optional heart-rate summary/zones and sport-specific fields;
- device/source attribution.

Keep imported sessions independent of adherence. A separate projection may map
a workout to a commitment after matching rules have run.

### `sleep_sessions`

Sleep needs its own model rather than one generic numeric event:

- sleep start/end and local sleep date;
- timezone/offset and source device;
- time in bed, time asleep, wake time, sleep latency, wake-after-sleep onset;
- light, deep, REM, and awake durations when supplied;
- interruptions/wakeups and provider sleep score when supplied;
- optional heart-rate, breathing, snoring, or other device-supported summaries;
- revision timestamp and completeness state.

The same night may be updated after the first sync. Upsert provider revisions
instead of treating the first record as final. Define the local sleep date by
the user's timezone and the session's wake date, not by UTC midnight.

### `sleep_segments` (optional in the first release)

If stage-level charts or questions are required, store stage intervals linked
to a sleep session: start/end and normalized stage (`awake`, `light`, `deep`,
`rem`, `unknown`). Otherwise retain only nightly aggregates initially and add
segments later.

### Projection and operations tables

- `health_event_projections`: traceable mapping from a source record to an
  existing `mediator.events` row and optional commitment, including projection
  version/status.
- `health_sync_runs`: category, cursor/range, counts, status, duration, and
  sanitized error information.
- `health_webhook_receipts`: receipt/category/timestamps and processing status,
  without measurements or credentials.

## Provider and Sync Layer

Create `app/services/health_sync/` with a provider-neutral interface and a
Withings adapter. The service should expose operations such as authorize,
exchange/refresh token, subscribe/unsubscribe, fetch changes, normalize,
upsert, reconcile, and delete connection data.

Add:

- authenticated connect, status, resync, disconnect, export, and delete routes
  in `app/routers/health_devices.py`, while evolving the deployed OAuth callback
  in `app/routers/withings.py`;
- a separate public webhook route with strict verification and request limits;
- a dedicated sync worker/queue using the existing lifespan worker pattern;
- Withings settings in `app/config.py` and `.env.example`;
- feature flags for connection, each data category, and adherence projection.

The production registration contract is fixed to these exact public URLs:

- OAuth redirect: `https://veas-production.up.railway.app/api/health/devices/withings/oauth/callback`;
- data notifications: `https://veas-production.up.railway.app/api/health/devices/withings/notifications`.

`WITHINGS_CALLBACK_URL` must exactly equal the OAuth redirect URL in every
OAuth authorization and token-exchange request. Both routes must answer
`HEAD` with exactly `200 OK`: the Withings Partner portal rejected `204`
during production registration even though the general notification contract
allows a broader successful response. Registration-safe fail-closed handlers
already exist in `app/routers/withings.py`; M1 replaces their OAuth `GET` and
notification `POST` `503` responses only when state validation, encrypted
token exchange, and durable notification ingestion are complete.

OAuth state must be short-lived, signed, one-time, and bound to the authenticated
user. Redirect URIs must be allowlisted exactly. Request only the scopes needed
for selected categories.

For Withings specifically:

- request `user.metrics` for scale measurements and `user.activity` for
  activity, workouts, and ordinary nightly sleep data;
- fetch measurements with `action=getmeas`, workouts/activity with the v2
  measure endpoints, and sleep with `POST /v2/sleep`;
- use sleep `action=getsummary` for nightly sessions and `action=get` for stage
  intervals or timestamped streams;
- subscribe to the relevant change categories, including `appli=44` for sleep
  summaries, and follow every notification with an authoritative API fetch;
- paginate summary pulls using `more`/`offset` and compute `lastupdate` from the
  maximum provider `modified` value;
- fetch raw sleep stages in windows no longer than 24 hours, the safest limit
  given the current endpoint documentation;
- upsert sleep by provider session `id`; when `modified` advances or `completed`
  changes, replace its normalized summary and, if enabled, re-fetch its stage
  window.

Basic sleep data and advanced metrics must be capability-driven. Current
Withings documentation lists duration, awake/light/deep/REM totals, and wakeups
for several watches, while HRV, respiratory data, out-of-bed state, apnea, and
other detailed signals depend on device, region, API pack, or a separate
contract. The implementation should render only fields actually returned and
must not promise advanced recovery metrics during the MVP.

For each webhook/category:

1. Validate the request and map it to an existing connection.
2. Record a receipt and mark that connection/category dirty.
3. Return success quickly without doing a full provider fetch.
4. Fetch from the saved cursor with a 24–48 hour overlap.
5. Follow pagination, normalize records, and upsert in a transaction.
6. Advance the cursor only after all records commit successfully.
7. Rebuild derived summaries/projections affected by changed or deleted data.

Retry rate limits and transient failures with jitter. Refresh once after an
authentication failure; if that fails, mark the connection `reauth_required`
and tell the user without exposing provider details.

## Hector Behavior

Add read tools rather than exposing raw provider payloads:

- `get_health_connection_status`;
- `get_weight_trend` for latest weight and configurable weekly/monthly change;
- `list_workouts` for recent normalized sessions;
- `get_sleep_summary` for recent nights, rolling averages, and consistency;
- `get_recovery_summary` only after its product definition is explicit.

Update Hector's hot context with compact derived information, for example:

```text
Connected health data: Withings, last synced 18 min ago.
Weight: 84.2 kg; 7-day average 84.6 kg; 30-day change -1.1 kg.
Workouts: 3 this week; 2 matched to the current lifting commitment.
Sleep: last night 7h 18m; 7-day average 6h 54m; bedtime varied by 1h 22m.
```

Do not include raw stage timelines, detailed heart-rate series, or full history
in every LLM prompt. The current fitness block also needs two corrections: it
should render health summaries when no active commitment exists, and it should
not discard numeric values and units.

Recommended conversational rules:

- Ask which imported workout satisfies a commitment when matching is ambiguous.
- Never mark a workout done because a weight or sleep metric happened; the
  current adherence logic treats linked numeric events as completion, so
  projections must be type-safe.
- Use sleep as context for supportive planning, not as an excuse generator or a
  medical conclusion.
- Prefer multi-night sleep trends over reacting strongly to one noisy night.
- Do not notify after every weigh-in, workout, or wake-up. Default to on-demand
  answers and a weekly digest; make proactive cadence configurable.
- Preserve manual reports. If a manual and device workout describe the same
  session, link them in the read model rather than deleting user testimony.

## User Experience

1. The user asks Hector to connect Withings and receives an authenticated link.
2. A consent screen explains weight/body composition, workouts/activity, and
   sleep separately. The user selects categories, history window, display units,
   and coaching cadence.
3. After OAuth, run a backfill (recommended default: 30 days) and show the last
   sync time plus counts by category.
4. Hector asks whether imported workouts or sleep sessions should satisfy any
   existing explicit commitments. It does not silently create commitments.
5. A connection settings surface supports pause, resync, reconnect, disconnect,
   export, and deletion. On disconnect, unsubscribe/revoke with the provider and
   let the user choose whether local history is retained or deleted.

A chat-first MVP can skip charts. Add read-only operator pages for connection and
sync health first. A later authenticated fitness screen can display weight trend,
workout history, sleep duration/consistency, and connection status.

## Delivery Plan

### Phase 0 — Provider spike and decisions (2–3 days)

- Confirm Withings and exact scale/watch models.
- Create a Withings developer application and demo/test account.
- Verify required OAuth scopes, production promotion requirements, rate limits,
  webhook subscriptions, and device-specific metric availability.
- Capture sanitized fixtures for measurements, workouts, sleep summaries, sleep
  stages, corrections, deletions, pagination, and token refresh.
- Lock canonical units, initial 30-day history, retention, sharing, and automatic
  commitment-matching policy.

Exit: a command-line spike imports one sample of each selected resource and all
required fields are represented by frozen contract fixtures.

### Phase 1 — Secure connection and ingestion foundation (4–6 days)

- Add migrations, encryption requirements, RLS, settings, and feature flags.
- Implement OAuth connect/callback/status/disconnect.
- Implement adapter, cursors, source upserts, sync runs, webhook receipts, and
  reconciliation worker.
- Add an operator-only sync status page without measurements or tokens.

Exit: a dogfood user can connect, backfill, receive webhook-triggered updates,
disconnect, and delete imported data without duplicates.

### Phase 2 — Weight and sleep read models (4–6 days)

- Normalize weight and sleep sessions/aggregates in shadow mode.
- Implement weight trend and sleep summary queries/tools.
- Update Hector hot context and conversation rules.
- Add user-visible connection status and last-sync freshness.

Exit: Hector accurately answers latest/trend questions from fixture and dogfood
data, handles late sleep revisions, and leaks no data across user/topic scope.

### Phase 3 — Workouts and adherence projection (4–6 days)

- Normalize workout sessions and expose workout history.
- Implement explicit mapping plus conservative deterministic matching.
- Add versioned, idempotent projections into `mediator.events`.
- Recompute projections when a provider record changes or is deleted.
- Consolidate or retire the duplicate adherence implementation before rollout.

Exit: an imported workout can satisfy the correct commitment exactly once, while
ambiguous and unmatched sessions remain visible but do not alter adherence.

### Phase 4 — Product polish and gradual rollout (3–5 days)

- Add configurable weekly summaries and optional relevant nudges.
- Add export/deletion UX and documented privacy behavior.
- Roll out to 2–5 dogfood users, then cohorts by category flag.
- Add a user dashboard only if chat and compact summaries prove insufficient.

Exit: freshness, duplicate, privacy, and correction gates remain healthy through
at least two weeks of dogfood use.

Estimated engineering time: roughly **3–4 weeks** for one engineer including
dogfood hardening, excluding vendor approval delays and a polished dashboard.

## Validation

Automate at least these cases:

- OAuth state replay, wrong-user callback, expired code, revoked access, and
  concurrent rotating-token refresh;
- webhook verification/validation plus duplicate, delayed, reordered, missing,
  and retried notifications;
- pagination, rate limiting, cursor crash atomicity, nightly reconciliation, and
  provider update/deletion propagation;
- provider exponent/unit conversion and scale groups with multiple metrics;
- workout timezone/DST boundaries and manual/device duplicates;
- sleep sessions crossing midnight, naps, split sleep, DST, missing stages,
  overlapping/revised sessions, and local sleep-date assignment;
- commitment projection type safety, ambiguity, idempotency, and reversal;
- multiple household scale users and source attribution;
- strict user/bot/topic isolation, separate partner-sharing consent, and absence
  of raw health values from logs, audit arguments, and default LLM prompts;
- end-to-end weigh-in to trend, workout to adherence, and sleep revision to
  rolling summary.

The existing focused Hector migration/tool/adherence/hot-context suite currently
passes 108 tests and provides a useful regression baseline.

## Observability and Rollout Gates

Track:

- connection status and last successful sync by category;
- webhook-to-visible latency and cursor age;
- OAuth refresh/reauth failures, provider errors, and rate limits;
- records fetched, inserted, updated, duplicated, and tombstoned;
- incomplete/revised sleep-session counts;
- projection lag, failure, match rate, and user correction rate;
- user-reported wrong-person, missing, or duplicate data.

Alert when a connected category is stale for more than 24 hours, a webhook is not
followed by a successful fetch, authentication failures persist, or projection
counts drift from source records. Admin pages should show identifiers, status,
counts, and timestamps by default—not tokens or health measurements.

## Open Decisions Before Implementation

1. Is the provider definitely Withings, and what are the exact scale/watch models?
2. Which first-release fields matter beyond weight, workouts, and total sleep?
   In particular: body composition, steps, heart-rate summaries, sleep score, and
   sleep stages.
3. Should sleep be informational only, or can users create explicit sleep
   commitments such as bedtime consistency or nightly duration?
4. What history, retention, export, deletion, and EU health-data legal policies
   apply?
5. What exact rule, if any, permits automatic workout-to-commitment matching?
6. Should derived summaries ever be partner-shareable, and under what separate
   consent?
7. Is a minimal web settings page sufficient, or is an authenticated fitness
   dashboard part of the first release?

## Official Withings References

- Public Health Data API overview:
  https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/public-health-data-api-overview/
- OAuth web flow:
  https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/oauth-web-flow/
- Available health-data/device matrix:
  https://developer.withings.com/developer-guide/v3/data-api/all-available-health-data
- OpenAPI specification:
  https://developer.withings.com/openapi.yaml
- OAuth scopes:
  https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/oauth-authorization-url/
- Notification overview:
  https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/data-api/notifications/notification-overview/
- Notification categories:
  https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/data-api/notifications/notification-content/
- `lastupdate` synchronization:
  https://developer.withings.com/developer-guide/v3/tutorials/how-to-compute-lastupdate/
