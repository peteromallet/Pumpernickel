# Sprint 3: Agentic Live Debrief

## Outcome

After a live conversation ends, run a long private selected-bot debrief turn that reads the full transcript and updates durable state through the existing tool system. The debrief should submit a structured review artifact. Systematic provenance-linking for every durable write type lands in Sprint 4, so production triggering remains feature-flagged until that work is complete.

## Scope

In:
- Add `run_live_debrief_agentic_job(conversation_id, user, scope)`.
- Feed the job prep artifact, agenda/coverage, full transcript, live notes, and normal hot context.
- Add terminal tool `submit_live_debrief`.
- Allow read, record/write, and schedule tools according to selected bot scope.
- Disallow outbound tools.
- Set debrief tool cap default to `500`.
- Persist `conversation_artifacts(type=live_debrief)` and optional `review_summary`.
- Update conversation status to `debriefing`, then `review_pending` or `completed` depending on existing review flow.
- Add `debrief_failed` status and retry path or internal retry helper.
- Keep production triggering behind a feature flag until Sprint 4 provenance-linking is complete.

Out:
- Do not require user approval before durable writes in this sprint, unless existing tool semantics already require it.
- Do not change normal chat memory behavior.
- Do not make the live in-call loop do durable extraction.

## Locked Decisions

- Debrief is a private non-chat `bot_turn` with `trigger_metadata.kind = "live_debrief"` and `conversation_id`.
- Debrief may mutate durable state through existing scoped tools.
- Debrief may schedule follow-ups if the selected bot's tool scope allows it.
- Debrief may have many tool calls; cap target is `500`.
- Required finalization gate: `submit_live_debrief`.
- Outbound messaging stays disabled.
- Partner/dyad privacy rules must be applied to transcript input. Partner turns that are not shareable under existing partner-sharing policy must be redacted from the debrief input bundle.
- Debrief-created durable writes are allowed in this sprint, but systematic artifact-to-durable provenance for every write type is Sprint 4. This sprint should not ship automatic production debrief writes without the feature flag.

## Debrief Input Bundle

The model should receive:
- selected bot profile/persona
- rendered normal hot context
- prep brief artifact payload
- agenda items and coverage state
- full `transcript_turns`
- `conversation_notes`
- explicit instruction to cite transcript evidence for durable claims
- structured evidence references where possible: `{transcript_turn_id, quote, confidence}`
- current tool cap and no-outbound constraint

## Submit Payload Shape

`submit_live_debrief` should include:
- `review_summary`
- `what_heard`
- `what_decided`
- `still_open`
- `what_to_remember`
- `durable_write_summary`
- `open_questions`
- optional references to transcript turn IDs or quotes

Exact schema should be Pydantic-backed and versioned.

## Open Questions

- Should debrief run synchronously when the user hits Stop, or in background while UI shows `debriefing`? Prefer background/polling; synchronous debrief is out of scope for 500-tool-cap jobs.
- Should review screen block until debrief completes, or show transcript immediately and update when debrief finishes?
- How should failed durable writes be represented in `submit_live_debrief`?
- Should durable writes be staged for user review in v1, or written automatically with rollback/provenance? If automatic writes remain, Sprint 4 must deliver rollback/debug tooling before production enablement.

## Constraints

- Existing live review endpoints must remain backward-compatible.
- A debrief failure must leave the conversation recoverable (`debrief_failed`) and retryable.
- Do not let failed debrief mark the whole live session as lost.
- Cost/spend recording must include this non-chat job.
- Out-of-bounds/sensitive-content guardrails must be evaluated before durable transcript-derived writes.
- The non-chat runner must not use chat step-derived tool gating. It must use an explicit flat debrief tool policy.
- Missing `submit_live_debrief` after the tool cap must be a distinct retryable failure reason, not a silent success.
- Defer or batch spend/audit writes if 500-tool debrief jobs create excessive bookkeeping pressure.

## Done Criteria

- Ending a live conversation triggers or queues debrief.
- Debrief can write at least one memory/observation in tests behind the feature flag.
- Debrief cannot send outbound messages.
- Tool cap `500` is enforced/configurable.
- Failed debrief is visible and retryable.
- Review payload can be read by existing or updated review endpoint.

## Touchpoints

- `app/services/live/synthesis.py`
- `app/services/live/`
- `app/services/agentic.py` or `app/services/nonchat_agentic.py`
- `app/services/tools/registry.py`
- durable write tool return payloads as needed
- `app/routers/live_voice.py`
- tests for debrief happy/failure/retry paths

## Anti-Scope

- Do not add new durable knowledge primitives.
- Do not rebuild the whole review UI.
- Do not change normal inbound agentic turn behavior.
