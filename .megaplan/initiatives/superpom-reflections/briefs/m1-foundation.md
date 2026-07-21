# M1 — Reflection Foundation

## Outcome

Land the durable reflection data contract, template registry, and storage
services that every later milestone can depend on. Produce a reviewed schema and
service handoff; do not yet alter inbound routing or SuperPOM behavior.

## Scope

- Add forward and down migrations for `reflection_sessions`,
  `reflection_entries`, and `reflection_derivations`.
- Implement encryption, RLS/FORCE RLS, user/bot/topic scoping, indexes,
  immutable revision semantics, one-active-session concurrency, session claim
  and retry state, and idempotency constraints.
- Implement the extensible reflection-template registry and normalized payload
  validators.
- Implement storage/service APIs for opening or attaching to a session,
  finalizing/claiming/recovering a session, appending an immutable entry
  revision, listing/getting visible entries, and recording derivation decisions.
- Add static migration, live database, concurrency, service, privacy, and
  correction tests.
- Write a short schema/service handoff consumed by M2 and M3.

## Locked Decisions

- The epic North Star and `SD-001` through `SD-008` in
  `docs/superpom-reflections-full-build.md` are binding.
- Use exactly three reflection domain tables; source message IDs are ordered
  arrays on the session/entry.
- Finalized sessions are the durable processing queue.
- Entries are immutable revisions; mutable session coordination is separate.
- No `scheduled_jobs` integration and no feature flags.

## Open Questions

- Resolve the exact encrypted/plaintext column convention from existing private
  searchable sources.
- Resolve the cleanest transaction boundary for claiming a session and creating
  one current entry revision.
- Resolve correction reconciliation fields required by M3 without implementing
  M3 policy prematurely.

## Constraints

- Preserve all existing migrations and application data.
- Follow current migration numbering, down-migration, RLS, encryption, and
  validation-test conventions.
- Do not bypass existing user/topic/bot identity contracts.
- No production migration is applied in this milestone.

## Done Criteria

- The real migration chain applies to a scratch Postgres database and rolls back
  through the new down migration.
- Cross-user, cross-topic, and cross-bot access is rejected.
- Concurrent session opens cannot create two collecting sessions for one
  `(user_id, bot_id)`.
- Entry correction produces a new current revision without mutating history.
- Retry/claim tests prove idempotent recovery.
- Focused and relevant existing tests pass.

## Touchpoints

- `migrations/`
- new reflection service/template modules under `app/services/`
- encryption and database helper conventions
- migration/service tests

## Anti-scope

- Do not change inbound routing, SuperPOM prompts, retrieval views, embeddings,
  hot context, admin UI, or scheduling.
- Do not create a generic longitudinal-state framework.
- Do not implement a fourth association or processing-job table.
