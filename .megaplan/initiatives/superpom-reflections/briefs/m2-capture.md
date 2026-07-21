# M2 — Capture, Processing, and Derivation

## Outcome

Make user-initiated SuperPOM conversations reliably form structured reflection
sessions and entries, then conservatively derive existing knowledge with full
provenance, correction, and retry safety.

## Scope

- Integrate explicit reflection wording, active-session context, message
  semantics, conversational context, and local time using the locked precedence
  policy.
- Resolve day/week/month/custom periods in the user's timezone; content must
  override misleading clock-time hints.
- Attach same-burst and cross-turn messages to one active session.
- Finalize on explicit completion, clear topic transition, or race-safe
  inactivity; handle late messages, abandoned sessions, and competing starts.
- Implement a bounded normalizer that produces the validated shared payload and
  template-specific data without inventing missing fields.
- Implement typed derivation candidates and deterministic eligibility gates for
  memory, observation, distillation, and orientation.
- Reuse existing knowledge services for accepted writes and ledger every
  decision, target, assertion source, confidence, reason, and supporting message.
- Enforce the locked observation reinforcement rule, multi-evidence
  distillations, and existing reviewed/proposed Compass semantics.
- Reconcile derivations when a reflection is corrected without overwriting
  independently edited targets.
- Integrate SuperPOM turn planning so likely reflections cannot take a path that
  skips recording.
- Add list/get/finalize/correct tool contracts and natural SuperPOM prompt
  behavior needed for capture.
- Add classification golden tests and session/routing/inactivity/concurrency
  tests, including voice-message transcripts through the existing ingress path.
- Add derivation semantic-boundary, idempotency, partial-failure, provenance, and
  correction-reconciliation tests.

## Locked Decisions

- Every reflection starts from a user message; no invitations or proactive
  reflection messages exist.
- Time is the weakest classification signal.
- Ambiguity falls back to `freeform` rather than forcing a temporal template.
- Raw messages remain canonical evidence; normalization creates an immutable
  entry through M1 services.
- No feature flags or shadow-only production path.
- Reflections never derive actions, tasks, reminders, or follow-ups.

## Open Questions

- Identify the correct coalescer/inbound seam for attachment without duplicating
  existing message-burst behavior.
- Set the inactivity interval from observed transport behavior and existing
  timing conventions.
- Decide whether normalization completes inside the record phase or through the
  finalized-session worker seam while keeping the user-facing response fast.
- Resolve transaction/compensation behavior when a target service write and its
  ledger row cannot share one transaction.

## Constraints

- Preserve normal non-reflection SuperPOM behavior and pacing.
- Do not expose internal classifications or structured payloads unless asked.
- Classification must not capture routine logistics, jokes, or ordinary
  questions as reflections.
- Do not create or mutate scheduled jobs.

## Done Criteria

- Explicit, implicit, freeform, day/week/month opening and closing examples are
  correctly captured end to end.
- Same-burst and cross-turn rants create one entry with ordered source messages.
- Explicit completion, inactivity, topic transition, late messages, and
  concurrent starts behave deterministically.
- “That wasn't a reflection” and temporal corrections append correct revisions.
- Negative examples do not create sessions.
- No tested path sends or schedules a proactive reflection message.
- Every applied derivation traverses to the reflection and source messages;
  retries do not duplicate targets or ledger decisions.
- Observation, distillation, Compass, and correction cases satisfy their locked
  evidence and lifecycle rules.

## Touchpoints

- inbound/coalescer/debounce services
- turn planning and agentic routing
- `tool_schemas.py` and tool registry/read/write surfaces
- `app/bots/superpom.py` and SuperPOM prompt profile
- M1 reflection services
- memory/observation/distillation/orientation services
- bounded non-chat processing and lifespan worker patterns

## Anti-scope

- Do not implement embeddings, retrieval corpus, hot context, admin operations,
  or production deployment.
- Do not change other bots' reflection or scheduling behavior.
