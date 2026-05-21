# Sprint 5: Productization, Recovery, and UX

## Outcome

Make the live agentic episode flow operationally safe and user-visible end-to-end: clear statuses, retry paths, inspection endpoints, review UI integration, metrics, and compatibility with old live conversations.

## Scope

In:
- Add/standardize conversation statuses: `preparing`, `ready`, `active`, `debriefing`, `review_pending`, `completed`, `prep_failed`, `debrief_failed`.
- Update live UI to show prep/debrief progress and failures.
- Finalize retry UX for failed prep and debrief, building on minimal retry endpoints/helpers from Sprints 2 and 3.
- Add operator/debug endpoint for a session's artifacts, bot turns, tool counts, and provenance links.
- Add metrics/logging for prep/debrief duration, tool counts, submit missing/failure, durable write counts, and retry counts.
- Backward-compatible loading for conversations without artifacts.
- Update docs/runbook.

Out:
- Do not change schema contracts from Sprint 1 except additive compatibility fixes.
- Do not change prep/debrief core agent behavior except bug fixes.
- Do not redesign the entire live UI visual system.

## Locked Decisions

- User-facing status is derived from `mediator.conversations.status`.
- Prep/debrief artifacts are the source for rich card/review data where available.
- Existing session card endpoints remain available and return compatible shapes.
- Retrying prep/debrief creates a new bot turn and a new artifact revision; old artifacts remain auditable.
- Current artifact per type is selected by highest `revision_number`, not `created_at`.

## UX Requirements

- Persona pick -> preparing -> agenda card -> consent -> active call -> debriefing -> review.
- If prep fails, show retry/back options.
- If debrief fails, keep transcript accessible and show retry.
- Review screen should prefer debrief artifact but gracefully fall back to existing synthesis.

## Ops Requirements

- Debug endpoint should show:
  - conversation id/status/bot_id/user_id
  - related bot turns by `conversation_id` or metadata
  - artifacts by type/version
  - artifact links
  - tool call counts and failure classes
- Metrics should be structured logs if no metrics backend exists.

## Open Questions

- Should retry endpoints be admin-only initially?
- Should failed prep leave an empty conversation row, or delete it on user cancel?
- Should old conversations get synthetic artifacts on read? Prefer no; use fallback views.

## Constraints

- Do not break existing `/api/live/sessions/{id}/card`, `/end`, `/review`, or `/review/save`.
- UI must handle long debrief latency.
- No destructive cleanup of old sessions.

## Done Criteria

- Full happy path works from persona selection through review.
- Prep failure and debrief failure are visible and retryable.
- Existing sessions without artifacts still load.
- Debug endpoint helps answer "what did the agent read/write for this conversation?"
- Tests cover status transitions, retries, fallback reads, and metrics emission.

## Touchpoints

- `app/routers/live_voice.py`
- `app/services/live/`
- `web/live-voice/src/`
- `tests/test_live_*`
- `docs/live-conversation-mode.md`
- deployment/runbook docs if present

## Anti-Scope

- Do not introduce OAuth/auth changes.
- Do not change STT/TTS providers.
- Do not auto-delete failed or abandoned conversations.
