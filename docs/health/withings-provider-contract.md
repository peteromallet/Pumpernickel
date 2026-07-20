# Withings Provider Contract

Status: finalized M1 handoff contract
Last updated: 2026-07-20

This document is the implementation handoff for the Withings-specific health
sync foundation in `app/services/health_sync/`. It records the settled M1
contracts that later milestones must preserve, plus the explicit M2 work that
is still out of scope.

## 1. Scope And Status

- Provider slug: `withings`
- Resource types: `measurement`, `workout`, `sleep`
- M1 covers offline fixtures, provider contracts, OAuth callback handling,
  encrypted token storage, webhook queueing, sync/reconciliation, worker
  wiring, and fake-provider end-to-end coverage.
- M1 does not claim live vendor approval, live notification registration, or
  production flag activation.

## 2. Runtime Anchors And Public Surfaces

- Code contract anchors:
  - `app/services/health_sync/models.py`
  - `app/services/health_sync/provider.py`
  - `app/services/health_sync/withings.py`
  - `app/services/health_sync/fake_withings.py`
  - `app/services/health_sync/tokens.py`
  - `app/services/health_sync/notifications.py`
  - `app/services/health_sync/sync.py`
  - `app/services/health_sync/reconciliation.py`
  - `app/services/health_sync/worker.py`
- Public callback routes are fixed:
  - `/api/health/devices/withings/oauth/callback`
  - `/api/health/devices/withings/notifications`
- Public authenticated device routes are fixed:
  - `/api/health/devices/withings/connect`
  - `/api/health/devices/withings/status`
  - `/api/health/devices/withings/resync`
  - `/api/health/devices/withings/disconnect`
  - `/api/health/devices/withings`
- `HEAD` on both public callback endpoints must stay exactly HTTP 200.

## 3. Minimal Provider Interface And Capability Map

The provider interface remains intentionally minimal and Withings-shaped:

- `exchange_code(code, redirect_uri) -> HealthOAuthTokens`
- `refresh_token(refresh_token) -> HealthOAuthTokens`
- `fetch_changes(access_token, resource_type, cursor) -> HealthFetchResult`
- `revoke(access_token, refresh_token=None) -> None`

Capability and scope mapping is category-driven:

- `measurement` -> provider category `measurements` -> scope `user.metrics`
- `workout` -> provider category `workouts` -> scope `user.activity`
- `sleep` -> provider category `sleep` -> scope `user.activity`

Settled capability flags:

- token refresh is supported
- incremental sync is supported
- disconnect is supported as a local connection-state transition
- webhook hints are category-scoped
- tombstones are supported by the project contract even though the live Withings
  API does not expose a first-class delete feed

## 4. OAuth And Connection Lifecycle Contract

Verified OAuth field names and behavior from the official docs:

- Authorization URL query fields: `response_type`, `client_id`, `scope`,
  `redirect_uri`, `state`
- Callback query fields: `code`, `state`
- Authorization code lifetime: 30 seconds
- OAuth exchange endpoint: `POST /v2/oauth2` with `action=requesttoken`
- Authorization-code exchange fields: `grant_type=authorization_code`,
  `client_id`, either `client_secret` or `nonce` plus `signature`, `code`,
  `redirect_uri`
- Refresh exchange fields: `grant_type=refresh_token`, `client_id`, either
  `client_secret` or `nonce` plus `signature`, `refresh_token`
- Token response fields: `userid`, `access_token`, `refresh_token`,
  `expires_in`, `scope`, `csrf_token`, `token_type`

Connection-state decisions already implemented in M1:

- state validation is one-time, user-bound, HMAC-signed, expiring, and
  redirect-bound
- encrypted token persistence happens only after a valid callback exchange
- connection rows may move through `active`, `reauth_required`,
  `disconnected`, and `deleted`
- refresh-token rotation uses optimistic locking
- `reauth_required` clears stored tokens and records only sanitized error
  metadata

## 5. Cursor State And Overlap Policy

Cursor state is stored per resource type as JSON with this shape:

```json
{
  "resource_type": "measurement",
  "last_modified": "2026-07-20T06:05:00Z",
  "page_offset": 75,
  "etag": "etag-1"
}
```

Rules:

- `resource_type` is required and must be one of `measurement`, `workout`, or
  `sleep`
- `last_modified` is optional ISO-8601 UTC text
- `page_offset` is optional and non-negative when present
- `etag` is optional opaque provider metadata
- `overlap_window` defaults to 48 hours and is intentionally not serialized
- persisted cursors must not retain an in-flight `page_offset`; `sync.py`
  treats that as invalid cursor state
- cursor advancement happens only after the full page set commits
- backfill seeding uses a 30-day window in reconciliation when no cursor exists

## 6. External Keys And Idempotent Source-Record Rules

Source-record uniqueness is fixed at:

```text
(connection_id, resource_type, external_id)
```

When the provider does not return a stable native identifier, the fallback key
format is:

```text
<resource_type>:fallback:<canonical-json>
```

Rules:

- only immutable provider fields may contribute to fallback components
- canonical JSON sorts object keys and emits ASCII JSON without extra spacing
- datetimes serialize as UTC `Z` timestamps
- dates serialize as ISO dates
- enums serialize to their value
- UUIDs serialize to strings
- native provider ids always win over fallback material when present
- tombstone/upsert replay must preserve the same `external_id` so revisions stay
  idempotent

## 7. Fetch Shapes And Normalization Boundaries

Withings fetch surfaces used by the real adapter:

- measurements: `POST /measure` with `action=getmeas`
- workouts: `POST /v2/measure` with `action=getworkouts`
- sleep summary: `POST /v2/sleep` with `action=getsummary`
- sleep detail fanout: `POST /v2/sleep` with `action=get`

Confirmed request fields:

- measurement fetch accepts `meastype`, `meastypes`, `category`, `startdate`,
  `enddate`, `lastupdate`, `offset`
- workout fetch accepts `startdateymd`, `enddateymd`, `lastupdate`, `offset`,
  `data_fields`
- sleep summary fetch accepts `startdateymd`, `enddateymd`, `lastupdate`,
  `data_fields`
- sleep detail fetch accepts `startdate`, `enddate`, `data_fields`,
  `meastypes`

Normalization boundaries:

- measurement records are built from `body.measuregrps[]`
- workout records are built from `body.series[]`
- sleep sync stores both summary-derived records and detail-derived records from
  the same summary window
- the provider surface returns normalized metadata plus timestamps; raw HTTP
  payloads are not persisted
- measurement deletions are modeled through project tombstone fixtures and
  source-record state, not a live Withings delete endpoint

## 8. Notification And Dirty-Category Contract

Webhook intake is queue-only. It must never fetch provider data inline.

Settled rules:

- expected content type is `application/x-www-form-urlencoded`
- required fields for M1 routing are `userid` and `appli`
- category mapping is:
  - `1` -> `measurement`
  - `16` -> `workout`
  - `44` -> `sleep`
  - `50`, `51`, `52` -> `sleep`
- incoming payloads are deduplicated by SHA-256 of canonicalized form fields
- unknown connections are recorded as ignored receipts without raising provider
  fetch work
- known connections mark exactly one dirty category per
  `(connection_id, resource_type)` and return fast HTTP 200
- missed notifications are recovered by reconciliation plus `lastupdate`
  backfill, not by expecting vendor replay

## 9. Error Taxonomy And Retry Behavior

Normalized error kinds are fixed to:

- `authentication`
- `rate_limit`
- `transient`
- `permanent`
- `malformed_response`
- `invalid_cursor_state`

Retry decisions:

- `rate_limit` and `transient` are retryable
- `authentication`, `permanent`, `malformed_response`, and
  `invalid_cursor_state` are not blind-retry classes
- worker settings are bounded by config:
  - `HEALTH_SYNC_REQUEST_TIMEOUT_S`
  - `HEALTH_SYNC_MAX_ATTEMPTS`
  - `HEALTH_SYNC_RETRY_AFTER_CAP_SECONDS`
- auth failures may trigger one refresh path; revoked or invalid refresh tokens
  move the connection to `reauth_required`
- error messages exposed through routes, logs, and stored diagnostics stay
  metadata-only and sanitized

## 10. Privacy, Logging, And Payload Retention Rules

Privacy posture for this project is strict by default:

- never expose access tokens, refresh tokens, OAuth codes, or exact health
  values in route responses, logs, fixtures, or test assertions
- never persist raw provider payloads in `health_source_records`
- diagnostics may include provider, resource type, connection id, status,
  sanitized error code, attempt counts, and timestamps
- webhook receipts store hashes and metadata, not full request bodies
- authenticated health-device routes stay separate from general health liveness
  routes
- any future raw-payload retention must be explicit, encrypted, access-scoped,
  and approved outside this milestone

## 11. Fixture Catalog And Offline Replay Requirements

Fixture catalog source: `tests/fixtures/withings/catalog.json`

Catalog coverage:

- `oauth_token_exchange_success.json`: authorization-code exchange success
- `oauth_token_refresh_rotated.json`: refresh rotation success
- `measurements_page_1.json`: first measurement page with `more=1`
- `measurements_page_2.json`: final measurement page with `more=0`
- `measurements_revision.json`: later replay of a changed measurement
- `measurements_tombstones.json`: synthetic measurement tombstones
- `workouts_page_1.json`: workout summary page
- `sleep_summary_page_1.json`: sleep summary page
- `sleep_detail_page_1.json`: sleep detail page
- `rate_limit_retry_after.json`: HTTP 429 with `Retry-After` and status `601`
- `malformed_measurements_body.json`: malformed JSON/body scenario
- `transient_service_unavailable.json`: HTTP 503 transient failure
- `request_timeout.json`: synthetic timeout with no payload

Replay requirements:

- fixtures remain synthetic and sanitized
- callback URLs use `example.test`
- tokens use synthetic prefixes only
- fake provider and real adapter tests must consume the same cursor, fallback
  key, pagination, and error contracts documented here

## 12. Verified Vendor Fields, Spec Corrections, And Assumptions

Official sources verified on 2026-07-20 without live credentials:

- OAuth web flow:
  `https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/oauth-web-flow/`
- Authorization URL and scopes:
  `https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/oauth-authorization-url/`
- Access/refresh token lifecycle:
  `https://developer.withings.com/developer-guide/v3/integration-guide/public-health-data-api/get-access/access-and-refresh-tokens-no-recover/`
- Notification overview:
  `https://developer.withings.com/developer-guide/v3/data-api/notifications/notification-overview/`
- Notification payload/categories:
  `https://developer.withings.com/developer-guide/v3/data-api/notifications/notification-content/`
- Notification subscribe guide:
  `https://developer.withings.com/developer-guide/v3/data-api/notifications/notification-subscribe/`
- `lastupdate` tutorial:
  `https://developer.withings.com/developer-guide/v3/tutorials/how-to-compute-lastupdate/`
- OpenAPI:
  `https://developer.withings.com/openapi.yaml`

Spec corrections and blockers:

- trust `notification-content/` over `llms.md` for `appli` category mappings
- the OpenAPI `Notify - Subscribe` shape conflicts with the human subscribe
  guide; notification subscription must be re-verified against a demo or
  partner app before hard-coding a live request shape
- no unreachable-spec blocker remains for M1 field verification

Assumptions carried forward into later milestones:

- `status` is the stable machine-readable response field across endpoints; do
  not depend on a universal top-level `error` object
- `lastupdate` backfill from the maximum stored modified timestamp remains the
  safe live strategy and is compatible with the 48-hour overlap policy
- sleep event categories `50` through `52` can continue to map to the broad
  `sleep` resource type until a future milestone needs sub-category fanout
- Withings revoke behavior stays local-only until the call site can provide the
  external user identifier required by the live revoke endpoint

## 13. Operator Prerequisites Before Live Activation

The following remain operator prerequisites and are not satisfied by M1 code
alone:

- provision `DATA_ENCRYPTION_KEY`
- provision `WITHINGS_CLIENT_ID` and `WITHINGS_CLIENT_SECRET`
- register the exact HTTPS `WITHINGS_CALLBACK_URL` ending in
  `/api/health/devices/withings/oauth/callback` with no query or fragment
- expose a real HTTPS notification endpoint at
  `/api/health/devices/withings/notifications`
- obtain live Withings API entitlement and any required vendor approval for
  public-health-data access and notification subscription
- verify the live notification subscribe flow against the approved app because
  the docs disagree on the exact request shape
- keep all health flags off until credentials, approval, and endpoint
  registration are complete:
  - `HEALTH_SYNC_ENABLED`
  - `HEALTH_SYNC_MEASUREMENTS_ENABLED`
  - `HEALTH_SYNC_WORKOUTS_ENABLED`
  - `HEALTH_SYNC_SLEEP_ENABLED`

## 14. Explicit M2 Boundaries And Handoff Checklist

M2 or later work may extend behavior, but it must preserve the contracts above.

Explicit out-of-scope items for M1:

- live Withings credential rollout
- live notification subscription registration
- production flag activation
- any new provider-general abstraction beyond the minimal Withings-shaped
  interface
- raw provider payload retention
- user-facing normalized read APIs built on top of the new health tables
- any change to public route paths

Before closing future milestone work, verify that it still preserves:

- the fixed callback and device route paths
- the four-method provider interface
- the cursor JSON shape and 48-hour overlap rule
- deterministic fallback external-id derivation
- metadata-only privacy posture
- deduplicated dirty-category queueing and post-commit cursor advancement
