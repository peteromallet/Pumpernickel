# Sprint 1: Live Conversation Artifacts + Non-Chat Turn Contract

## Outcome

Create the storage and execution contract that makes live voice sessions first-class agentic episodes. This sprint should not change the live UI workflow yet; it should create the schema, helper APIs, and tests that later sprints consume.

## Scope

In:
- Add a typed artifact layer under `mediator.conversations`.
- Add provenance links from artifacts to conversation rows and durable records.
- Define the non-chat turn kinds `live_prep` and `live_debrief`.
- Add config/budget surfaces for general non-chat tool caps, default `100`, and live debrief cap, default `500`.
- Add nullable indexed `conversation_id` to `mediator.bot_turns` for live prep/debrief jobs.
- Add helpers to create/list artifacts and artifact links.
- Add tests for migrations, helper behavior, and provenance shape.

Out:
- Do not replace live prep generation yet.
- Do not run live debrief yet.
- Do not change the React UI beyond type compatibility if needed.
- Do not refactor the normal `_run_agentic()` chat lifecycle.

## Locked Decisions

- `mediator.conversations` remains the live episode root.
- `mediator.bot_turns` remains the execution/audit object for agentic work.
- New child table: `mediator.conversation_artifacts`.
- New provenance table: `mediator.artifact_links`.
- `mediator.bot_turns` gains `conversation_id uuid null references mediator.conversations(id)`, indexed. It is populated for live prep/debrief jobs and any future live-specific agentic turns.
- Prep/debrief turn kinds are private non-chat agentic jobs represented through `bot_turns.conversation_id` plus `trigger_metadata.kind`.
- Durable state tables remain the source of truth for memories, observations, distillations, commitments, events, scheduled jobs, topic status, and pregnancy state.
- Conversation artifacts are immutable after creation. Retries create new artifact revisions.
- Current artifact for a `(conversation_id, artifact_type)` is the highest `revision_number`, not the newest timestamp.

## Proposed Schema

`mediator.conversation_artifacts`:
- `id uuid primary key default gen_random_uuid()`
- `conversation_id uuid not null references mediator.conversations(id) on delete cascade`
- `bot_id text not null`
- `user_id uuid not null`
- `artifact_type text not null`
- `payload jsonb not null`
- `payload_version integer not null default 1`
- `revision_number integer not null default 1`
- `created_by_turn_id uuid null references mediator.bot_turns(id)`
- `deleted_at timestamptz null`
- `expires_at timestamptz null`
- `created_at timestamptz not null default now()`
- unique `(conversation_id, artifact_type, revision_number)`

`mediator.artifact_links`:
- `id uuid primary key default gen_random_uuid()`
- `artifact_id uuid not null references mediator.conversation_artifacts(id) on delete cascade`
- `target_table text not null`
- `target_id uuid not null`
- `relation text not null`
- `evidence jsonb`
- `deleted_at timestamptz null`
- `created_at timestamptz not null default now()`
- index `(artifact_id, target_table, target_id, relation)`
- index `(target_table, target_id)`
- `target_table` must be CHECK-constrained to the known durable/session tables.

Allowed `artifact_type` values initially:
- `live_prep_brief`
- `live_debrief`
- `review_summary`
- `agenda_revision`
- `transcript_reflection`

Allowed relation values initially:
- `planned_item`
- `summarized_from`
- `evidence_quote`
- `extracted_memory`
- `extracted_observation`
- `extracted_distillation`
- `created_commitment`
- `logged_event`
- `created_follow_up`
- `updated_topic_status`

## Open Questions

- Should artifact payloads be encrypted at rest when they contain transcript-derived summaries, or is existing DB/storage policy sufficient? Resolve in Sprint 1 with reference to `docs/SECURITY.md`.
- Should durable tables get direct nullable `source_conversation_id` columns in addition to `artifact_links`, or is reverse index on `artifact_links(target_table, target_id)` enough for v1?
- Which existing durable write tables already return created IDs consistently, and which must be fixed in Sprint 4?

## Constraints

- Migrations must be safe against production data and schema-qualified with `mediator.` where the repo convention requires it.
- Existing live sessions without artifacts must continue to load.
- Avoid broad refactors in `app/services/agentic.py`; this sprint defines contracts and helpers.
- Tool caps should be configurable, not constants hidden in the runner.
- Avoid a fully free-form polymorphic link surface: `target_table` must be constrained, and evidence must use a documented JSON shape.

## Done Criteria

- Migrations apply in test DB.
- Helpers can create an artifact and link it to at least one conversation item and one durable record ID.
- Tests cover artifact deletion cascading from conversation deletion.
- Tests cover duplicate artifact link idempotency or expected rejection.
- Tests cover artifact revisions and current-artifact selection by `revision_number`.
- Tests cover `bot_turns.conversation_id` queryability for live prep/debrief turns.
- Documentation in this brief is reflected in code comments or a concise doc file under `docs/`.

## Touchpoints

- `migrations/`
- `app/services/live/`
- `app/services/agentic.py` only if needed for constants/types
- `app/services/turn_context.py` only if needed for `conversation_id`
- `tests/test_live_migrations.py`
- new tests for artifact helper behavior

## Anti-Scope

- Do not implement `submit_live_brief` or `submit_live_debrief` in this sprint unless required to validate schema shape.
- Do not start or auto-run the chain.
- Do not alter user-facing live UI states yet.
