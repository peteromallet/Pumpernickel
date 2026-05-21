# Sprint 4: Provenance for Debrief Durable Writes

## Outcome

Make transcript-derived durable writes auditable, queryable, and reversible. This sprint takes the debrief runner from "can write scoped durable state behind a feature flag" to "can safely link every debrief-created durable write back to the live conversation, artifact, bot turn, and transcript evidence."

## Scope

In:
- Audit all durable write tools used by selected bots and ensure return payloads expose created/updated record IDs.
- Add systematic artifact-link creation for debrief-created durable writes.
- Add structured evidence payloads on `artifact_links`.
- Add reverse provenance queries for records back to live conversations.
- Add tests for each supported relation type.
- Add rollback/deletion helper for durable records linked to a conversation artifact where table support exists.
- Decide whether direct `source_conversation_id` columns are needed for high-volume durable tables, and implement only if justified by query/test needs.

Out:
- Do not add new durable knowledge primitives.
- Do not redesign the debrief runner.
- Do not build the final UI/retry/debug product surfaces beyond helper/API needs.

## Locked Decisions

- Artifact links are the canonical provenance path for v1.
- Every debrief-created durable write that returns an ID must get an `artifact_links` row.
- `artifact_links.evidence` uses a documented schema:
  - `transcript_turn_ids: list[uuid]`
  - `quotes: list[str]`
  - `confidence: float | null`
  - `reason: str | null`
- Multiple evidence rows per artifact-target relation are allowed via `artifact_links.id`.
- Current artifact resolution uses `revision_number`.

## Relation Coverage

Required relation support:
- memories -> `extracted_memory`
- observations -> `extracted_observation`
- distillations -> `extracted_distillation`
- commitments -> `created_commitment`
- events -> `logged_event`
- scheduled jobs -> `created_follow_up`
- topic status -> `updated_topic_status`

If a selected relation cannot be supported because the underlying tool does not return a stable ID, fix the tool return payload in this sprint.

## Privacy and Reversibility Guardrails

- Partner-share redaction from Sprint 3 remains required.
- Out-of-bounds/sensitive-content hooks must run before transcript-derived durable writes where equivalent chat-side safeguards exist.
- Deleting/discarding a conversation must make linked artifacts and links non-current or soft-deleted. Durable records should be discoverable through reverse provenance for manual or automated cleanup.
- Do not copy long transcript quotes into durable record content when an evidence link can preserve the quote separately.

## Open Questions

- Which durable tables already support soft-delete, and which require manual cleanup or future migration?
- Should rollback be admin-only initially?
- Should automatic durable writes remain feature-flagged until user review UX is complete?

## Constraints

- Keep changes localized to tool return payloads and provenance helpers where possible.
- Do not break existing tool API tests.
- Preserve normal chat behavior.

## Done Criteria

- Debrief-created memories, observations, distillations, commitments, events, scheduled jobs, and topic status updates can be linked to the debrief artifact in tests, or explicitly documented as unsupported with a blocking test skipped/xfail.
- Reverse lookup from a durable record to the source live conversation works.
- Artifact evidence schema is validated.
- Rollback/deletion helper can enumerate all durable writes linked to a conversation.
- Live debrief feature flag can be enabled with provenance linking on.

## Touchpoints

- `app/services/tools/write_tools.py`
- `app/services/tools/registry.py`
- `app/services/live/`
- provenance helper module from Sprint 1
- tests for write tool return payloads and artifact links

## Anti-Scope

- Do not implement broad retention policy automation.
- Do not add user-facing UI.
- Do not change STT/TTS or live turn latency path.
