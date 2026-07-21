# SuperPOM Reflections — Full Build-and-Ship Brief

Status: implementation brief
Owner: SuperPOM
Initial consumer: SuperPOM
Scope: complete production shipment

## Settled Decisions

- **SD-001** — Reflections begin only from ordinary user-initiated text or
  voice messages; the feature sends no scheduled invitations or proactive
  reflection messages. _load_bearing: true_
- **SD-002** — Reflection templates are registered in code and combine an
  extensible `template_key` with independent temporal scope, phase, and period
  fields; new templates do not require schema changes. _load_bearing: true_
- **SD-003** — The durable core uses three domain tables:
  `reflection_sessions`, `reflection_entries`, and `reflection_derivations`.
  Ordered source message IDs live on the session/entry rather than in a fourth
  association table. _load_bearing: true_
- **SD-004** — Raw messages remain canonical evidence. Normalized reflection
  entries are immutable; corrections append a superseding revision.
  _load_bearing: true_
- **SD-005** — Finalized sessions are the durable processing queue; do not add
  a separate reflection-processing-jobs table. _load_bearing: true_
- **SD-006** — Reflection storage and knowledge derivation are distinct stages.
  Existing memory, observation, distillation, and Compass services retain their
  current semantics and validation boundaries. _load_bearing: true_
- **SD-007** — Reflections become a first-class retrieval source, but hot context
  receives only an active-session marker and a compact recent digest; historical
  entries are retrieved on demand. _load_bearing: true_
- **SD-008** — Ship one coherent system with no feature flags, pilot-only schema,
  shadow-only classifier, or deliberately dormant core path. Staging validation
  precedes production deployment. _load_bearing: true_
- **SD-009** — Voice scope means ordinary transport voice notes that already
  become `messages` through the transcription path. Live-voice
  `transcript_turns` are outside this feature. _load_bearing: true_
- **SD-010** — Calendar weeks are Monday through Sunday in the user's timezone,
  matching the repository's existing adherence convention. A session keeps the
  SuperPOM topic established by its opening message. _load_bearing: true_
- **SD-011** — Explicit wording and an active session are deterministic capture
  signals. Semantic recognition uses the configured SuperPOM planning/model
  path, not a new provider. Low-confidence content remains an ordinary message;
  clearly reflective content with ambiguous temporal meaning becomes
  `freeform`. _load_bearing: true_
- **SD-012** — Inactivity finalizes after 15 minutes by default. Explicit
  completion and clear topic transition may finalize earlier; transactional
  attachment/finalization resolves late-message races. _load_bearing: false_
- **SD-013** — A dedicated reflection worker claims finalized session rows and
  is started/recovered with the application's existing lifespan worker pattern.
  It does not introduce a new queue table. _load_bearing: true_
- **SD-014** — Reflections never derive tasks, reminders, follow-ups, or action
  rows. Open loops remain part of the reflection payload unless the user makes a
  separate explicit request through existing behavior. _load_bearing: true_
- **SD-015** — An observation requires either explicit recurring-pattern
  language from the user or support from at least two independent reflection
  entries. Corrections must not overwrite a derived target that was later edited
  independently. _load_bearing: true_
- **SD-016** — Only the current reflection revision is searchable. Superseding a
  revision enqueues removal of its old embedding and indexing of the new current
  revision. Source message order is deterministic by message time and ID.
  _load_bearing: true_

## Open Questions For Planning

The milestone planners must resolve these against repository evidence before
execution; they must not silently invent incompatible contracts:

1. Which existing inbound coalescing and turn-planning seam should own opening,
   attaching, and finalizing a cross-turn reflection session?
2. Which minimal canonical plaintext representation can safely participate in
   keyword/vector retrieval while the full payload and summary remain encrypted?
3. What atomicity/compensation boundary should apply when an existing knowledge
   service and its reflection derivation ledger cannot share one transaction?
4. Which authenticated operator command should retry a failed session while the
   existing admin pages remain read-only?

## Objective

Build and ship a first-class reflections system for SuperPOM. A user should be
able to speak or write naturally, on their own initiative, and have SuperPOM
recognize the reflection, collect the full
train of thought, turn it into a faithful structured record, derive appropriate
long-term knowledge from it, and use that history in future reasoning.

This is one complete feature. Do not ship a prompt-only approximation, a storage
layer that nothing consumes, a classifier that cannot be corrected, or a
reflection feed that bypasses the existing memory and Compass boundaries. Do
not add feature flags, shadow modes, pilot-only schemas, or deliberately defer
core pieces. Staging verification is required, but production receives the
whole coherent system once it passes.

## Definition of done

The feature is done only when all of the following are true:

1. SuperPOM can recognize start-of-day, end-of-day, start-of-week, end-of-week,
   start-of-month, end-of-month, and freeform reflections initiated by the user.
2. A user can begin a reflection through an ordinary text or
   voice message, without entering a special interface or command mode.
3. Explicit wording, an active reflection session, message content, conversation
   context, and local time are combined through a documented classification
   policy. Time is a weak hint, never the deciding signal when content disagrees.
4. Multi-message and cross-turn rants are collected into one bounded reflection
   session and finalized safely on explicit completion, clear topic transition,
   or inactivity.
5. The raw messages remain canonical evidence. The system creates a separate,
   immutable, versioned reflection entry rather than rewriting the messages.
6. Structured reflections support daily, weekly, monthly, freeform, and future
   templates without requiring a database migration for every new template.
7. Reflection processing can create or reinforce memories, observations,
   distillations, and Compass orientation items according to conservative,
   deterministic eligibility rules.
8. Every derived row is traceable to the reflection and exact source messages
   that support it. SuperPOM can distinguish explicit user statements from its
   own inferences.
9. Users can correct classification or content conversationally. Corrections
   create a new immutable revision and supersede the earlier entry; history is
   not destructively overwritten.
10. SuperPOM can list, retrieve, semantically search, compare, and reason over
    reflections without dumping the entire history into every prompt.
11. Reflection content follows the repository's strongest privacy, encryption,
    deletion, topic-scope, and user-isolation conventions.
12. Migrations, unit tests, integration tests, agentic evaluations, operational
    visibility, staging deployment, production deployment, and post-deploy
    verification are complete.

## Product behavior

### Ordinary messages, not a new transport type

A reflection begins as one or more ordinary inbound messages. `messages`
remains the source of what the user actually sent. “Reflection” is an episode
and a structured interpretation layered over those messages; it is not a new
WhatsApp or Discord message type.

The user may start explicitly:

- “Morning reflection.”
- “End-of-week thoughts.”
- “Reflection: I think I keep adding scope because finishing is scary.”
- “I want to think out loud for a minute.”

The user may also begin implicitly. SuperPOM should recognize substantial
self-reflection, planning, review, sense-making, or emotional processing while
avoiding routine questions, jokes, logistics, and passing remarks.

### Classification precedence

Classification uses the following precedence, from strongest to weakest:

1. **Explicit user wording** — authoritative.
2. **Active reflection session** — continuing messages attach to the open
   session unless the user clearly changes topic.
3. **Message semantics and conversation context** — prospective language suggests an opening phase;
   retrospective language suggests a closing or retrospective phase.
4. **Local time** — a tie-breaker only.

Persist the selected classification, its source, confidence, considered
alternatives, user timezone, and temporal period. When classification remains
ambiguous, prefer a faithful `freeform` reflection over forcing a daily or
weekly category.

### Session lifecycle

A reflection session has the following lifecycle:

```text
collecting -> finalizing -> processed
     |             |
     +-> abandoned +-> processing_failed -> retry -> processed
```

- A reflection opens a session after explicit or inferred classification of a
  user-initiated message.
- Same-burst messages attach automatically.
- Cross-turn messages attach while the session remains active and the topic is
  coherent.
- “Done,” “that's it,” and equivalent explicit completion finalize immediately.
- A clear conversational topic transition finalizes the reflection before the
  new topic is handled.
- Inactivity finalization uses a race-safe deadline. A late message must either
  join before finalization commits or begin a new session; it must never be lost
  or attached nondeterministically.
- Only one collecting reflection session may exist for a given user and bot.
  Competing starts resolve transactionally.

SuperPOM responds naturally during collection. It must not expose storage
phases, repeatedly announce classification, or force the user through a form.

## General reflection model

Do not encode a closed `day_start | day_end | week_start | week_end` database
enum. Temporal scope, conversational phase, and semantic template are separate
concepts.

Every reflection has:

- `template_key` — for example `daily_open`, `daily_close`, `weekly_open`,
  `weekly_close`, `monthly_open`, `monthly_close`, `freeform`,
  `decision_debrief`, or a future registered template.
- `temporal_scope` — `instant`, `day`, `week`, `month`, `custom`, or `none`.
- `phase` — `opening`, `closing`, `checkpoint`, `prospective`,
  `retrospective`, or `freeform`.
- `period_start` and `period_end` — nullable for timeless/freeform entries.
- `timezone` — the timezone used to interpret the period.
- `schema_version` and `processor_version`.

Templates live in a Python registry. Each registry entry defines the template's
prompt, allowed temporal scopes and phases, payload validator, normalizer
instructions, summary renderer, and comparison behavior. Adding a template
requires code and tests but not schema alteration.

Unstructured describes the input experience, not the stored kind. Every
template accepts natural free-form text or voice.

## Storage contract

Add an additive forward migration and matching down migration after the current
migration head. Follow existing `mediator` schema, RLS, encryption, indexing,
audit, and migration-validation conventions.

### `mediator.reflection_sessions`

Mutable coordination state for collection and processing:

- `id`
- `user_id`, `topic_id`, `bot_id`
- `opened_by_message_id` and optional `opened_by_turn_id`
- `template_key`, `temporal_scope`, `phase`
- `period_start`, `period_end`, `timezone`
- `classification_source`, `classification_confidence`
- `classification_metadata jsonb`
- `status`
- `idle_finalize_at`
- `finalized_at`, `processed_at`, `abandoned_at`
- failure classification, retry count, and last error metadata
- `created_at`, `updated_at`

Enforce one collecting session per `(user_id, bot_id)`. Add indexes for due idle
finalization, processing recovery, and recent user history.

### `mediator.reflection_entries`

Immutable normalized reflection documents:

- `id`, `session_id`
- `user_id`, `topic_id`, `bot_id`
- `template_key`, `temporal_scope`, `phase`
- `period_start`, `period_end`, `timezone`
- ordered `source_message_ids`
- encrypted structured payload plus only the minimum safe plaintext needed by
  established retrieval conventions
- encrypted human-readable summary
- `schema_version`, `processor_version`, `revision_number`
- optional `supersedes_entry_id`
- `created_by_turn_id`
- `created_at`

There must be exactly one current revision per reflection session. Corrections
append a revision and supersede the prior entry. Application code must reject
in-place content mutation.

### `mediator.reflection_derivations`

Auditable candidate and application ledger for downstream knowledge:

- `id`, `reflection_entry_id`
- `derivation_kind` (`memory`, `observation`, `distillation`, `orientation`)
- encrypted candidate payload
- `assertion_source` (`user_explicit`, `user_implied`, `agent_inferred`)
- confidence and deterministic eligibility reasons
- exact supporting message IDs
- decision (`applied`, `reinforced`, `deferred`, `rejected`, `superseded`)
- applied target table and target ID when applicable
- processor/tool-call provenance
- `created_at`, `decided_at`

Idempotency must prevent retries from creating duplicate knowledge rows or
duplicate derivation decisions.

Finalized sessions are themselves the durable processing queue. Workers claim
eligible sessions with `FOR UPDATE SKIP LOCKED`; session status, claim
timestamps, retry count, failure metadata, entry revision, and processor version
provide bounded retries, stale-claim recovery, explicit terminal failure, and
idempotency without another queue table.

## Normalized payload

All templates share a small envelope while allowing validated template-specific
data:

```json
{
  "summary": "",
  "facts": [],
  "events": [],
  "decisions": [],
  "priorities": [],
  "wins": [],
  "blockers": [],
  "open_loops": [],
  "questions": [],
  "signals": {},
  "template_data": {}
}
```

Fields may be empty or absent. The processor must preserve uncertainty and must
not invent content to make the document appear complete. Where practical,
individual extracted items carry their supporting message IDs inside the
encrypted payload.

## Knowledge derivation policy

Reflection storage and long-term knowledge writes are two separate stages. The
immutable reflection is what the user reported at a moment in time. Memories,
observations, distillations, and Compass items are what SuperPOM has responsibly
learned from one or more reports.

### Memories

Create or update memory only for stable concrete facts, constraints,
preferences, durable decisions, schedule facts, or support arrangements likely
to matter later. Do not turn transient mood, one-day context, speculation, or
every named person into memory. Read before writing, reinforce or supersede when
appropriate, and preserve message/reflection provenance.

### Observations

Observations describe recurring patterns, blockers, and helpful or harmful
tactics. A single reflection may create a deferred observation candidate, but
an active observation normally requires independent reinforcement across
multiple episodes or strong explicit user framing of a recurring pattern.
Store the evidence set, confidence, significance, and reinforcement history.

### Distillations

Distillations are provisional explanations connecting multiple concrete facts,
observations, themes, messages, or reflections. They must cite multiple pieces
of evidence, remain explicitly tentative, and use the existing revision and
retirement semantics. A compelling sentence in one rant is not automatically a
settled explanation.

### Compass orientation

Explicit or clearly implied principles, manifestations, goals, priorities, and
anti-patterns may be captured as `user_stated` according to SuperPOM's existing
contract. Genuine inference must remain `bot_proposed` and pass through the
existing review lifecycle. Reflections must not turn temporary urgency into a
permanent Compass heading without evidence.

### Follow-ups and actions

The reflection system must not proactively schedule a message or follow-up. An
`open_loop` is descriptive, not permission to create a task or contact the user
later. If the user separately and explicitly requests a reminder, that remains
ordinary existing reminder behavior outside the reflection pipeline. SuperPOM
must continue respecting the existing domain boundary: commitment and adherence
tracking belongs to the appropriate specialist bot.

## Processing architecture

Implement reflection normalization and derivation as a bounded non-chat agentic
processor, following the repository's strongest existing non-chat,
provenance-gate, and retry patterns.

The processor must:

1. Load the session, ordered source messages, Compass, and only the relevant
   existing knowledge needed for deduplication.
2. Produce and validate one normalized reflection payload.
3. Persist the immutable entry before applying downstream derivations.
4. Produce typed derivation candidates with evidence and assertion source.
5. Apply deterministic eligibility gates.
6. Use existing domain services for accepted writes rather than duplicating
   their SQL or bypassing their validation.
7. Write the derivation ledger in the same transaction as each accepted target
   write where practical.
8. Complete idempotently under retries and worker restarts.
9. Record actionable failure classes and recover without user-visible duplicate
   messages.

The ordinary SuperPOM response remains conversational. Durable reflection
processing must not delay or contaminate the user-facing response unnecessarily.

## No proactive scheduling

The reflection system sends no scheduled invitations, recurring check-ins, or
automatic follow-up messages. It must not seed `scheduled_jobs`, create
reflection recurrence rules, or contact a user because a day, week, or month is
starting or ending. Every reflection begins with a user-initiated message.

Calendar semantics still matter for classification and comparison. Resolve day,
week, and month boundaries from the user's timezone, including DST transitions,
short months, month ends, leap years, and timezone changes. These calculations
identify the period a reflection describes; they never trigger outbound
messaging.

## Retrieval and context

Make reflections a first-class searchable source throughout the existing
retrieval corpus:

- extend source-type constraints, searchable-content views, embedding jobs,
  lifecycle handling, hydration, labels, filters, and migration parity tests;
- embed only the approved plaintext/canonical representation under the same
  sensitivity policy as other private knowledge;
- support filtering by user, bot, topic, template, temporal scope, phase, and
  reporting period;
- add `list_reflections`, `get_reflection`, and reflection-aware search tools;
- support comparisons such as day opening versus day closing, week opening
  versus week closing, repeated blockers, carried open loops, and changes in
  direction;
- put only an active session and a compact recent-reflection digest into hot
  context; retrieve older history on demand.

SuperPOM must be able to answer “why do you believe that?” by traversing from a
memory, observation, distillation, or Compass change back to its reflection and
source messages.

## Corrections, deletion, and privacy

- “That wasn't a reflection” abandons an unprocessed session or supersedes the
  resulting entry without deleting the original message.
- “That was my evening reflection, not tomorrow's plan” creates a corrected
  revision with corrected temporal metadata.
- “You misunderstood this part” creates a new normalized revision and re-runs
  derivation reconciliation. Derived rows that no longer have support are
  contradicted, retired, invalidated, or superseded through their existing
  lifecycle; they are never left silently stale.
- User deletion must remove or tombstone reflection payloads, embeddings,
  processing state, and derivation evidence consistently with existing message
  retention policy.
- Apply `ENABLE ROW LEVEL SECURITY`, `FORCE ROW LEVEL SECURITY`, least-privilege
  grants, explicit owner/topic/bot scoping, and cross-user negative tests.
- Treat reflection payloads as highly sensitive. Follow the repository's
  encrypted-content conventions and ensure logs, audit summaries, exceptions,
  metrics, and admin pages do not leak the content.

## SuperPOM integration

Update SuperPOM's BotSpec, prompt profile, tool allowlist, read/record
instructions, hot-context assembly, and turn planning so that:

- likely reflections cannot fall into a quick-reply path that skips recording;
- SuperPOM reads active/relevant reflection context without abandoning its
  Compass-first contract;
- reflection capture does not blur the existing meanings of memory,
  observation, distillation, orientation, and commitment/event state;
- explicit and implicitly recognized reflections share one processing path;
- corrections are natural conversational actions;
- the user sees a concise response, not a dump of the structured payload;
- automatic capture is quiet but inspectable through ordinary questions such
  as “what did you take from that?” or “show me this week's reflections.”

## Operational visibility

Add a focused admin/operator surface and structured logs for:

- active and stuck sessions;
- due idle finalizations;
- processed and failed entries;
- retry counts and terminal failures;
- classification source/confidence without exposing sensitive content;
- derivation counts by kind and decision;
- retrieval/embedding coverage;
- end-to-end latency from final source message to processed reflection.

Operators must be able to diagnose and retry a failed session safely
without editing application tables manually or duplicating derivations.

## Required implementation surfaces

The implementer must inspect and modify all relevant contracts rather than
assuming this list is exhaustive:

- a new forward/down migration after the current migration head;
- `tool_schemas.py`;
- `app/services/tools/registry.py`, `read_tools.py`, and `write_tools.py`;
- new reflection template, storage, classification, processing, and rendering
  services;
- `app/services/inbound.py`, burst/debounce behavior, turn planning, and normal
  agentic routing;
- non-chat agentic/provenance infrastructure;
- embedding worker, searchable corpus, retrieval, and hot-context services;
- `app/bots/superpom.py` and `app/bots/prompts/profiles/superpom.py`;
- admin views and operational scripts;
- migration, service, tool, retrieval, prompt, privacy, and agentic
  evaluation tests;

Preserve the repository rule that root-level `tool_schemas.py` remains at the
repository root.

## Test and evaluation contract

At minimum, cover:

### Storage and privacy

- migration up/down and static parity;
- RLS and cross-user/cross-topic/cross-bot denial;
- encryption and log-redaction behavior;
- immutable entries and correction/supersession;
- deletion and embedding cleanup;
- processing claim recovery and idempotency.

### Classification and sessions

- explicit daily/weekly/monthly/freeform examples;
- explicit and implicitly recognized starts;
- content overriding time-of-day hints;
- ambiguous content falling back to freeform;
- routine non-reflection negatives;
- same-burst and cross-turn grouping;
- explicit completion, topic transition, inactivity, late messages, ignored
  concurrent starts, and attempts to start a second reflection while one is open.

### Temporal classification

- correct local day/week/month boundaries across both DST transitions;
- all supported daily/weekly/monthly opening and closing classifications;
- month ends, February, leap years, timezone changes, and content overriding
  misleading time-of-day hints;
- proof that no reflection path seeds a job or sends a proactive message.

### Normalization and derivation

- faithful payload generation without fabricated fields;
- stable memory extraction and deduplication;
- observation candidates requiring reinforcement;
- provisional multi-evidence distillation;
- explicit versus inferred Compass handling;
- open loops not becoming tasks without intent;
- retry after partial failure without duplicate writes;
- correction reconciling previous derivations.

### Retrieval and UX

- list/get/search and filter behavior;
- embeddings and corpus hydration;
- hot-context token bounds;
- start/end period comparisons;
- natural correction dialogue;
- SuperPOM retaining Compass-first and domain-boundary behavior;
- agentic evaluations for false-positive capture, false-negative capture,
  memory pollution, overconfident interpretation, and sensitive-content leaks.

Run the complete existing test suite as well as the new focused suite. Database
tests must run against a scratch Postgres instance with the real migration chain,
not only fake pools.

## Build and ship sequence

This is sequencing for safe execution, not a partial product rollout:

1. Lock the storage, template, classification, derivation, privacy, and
   correction contracts in tests.
2. Implement migrations and core services.
3. Implement temporal classification, session capture, normalization, and derivation.
4. Implement retrieval, hot context, SuperPOM tools/prompts, and operational
   visibility.
5. Run focused tests, the full suite, database migration validation, frontend or
   admin checks, and agentic evaluations.
6. Apply the full migration chain to staging and deploy the complete service.
7. Exercise explicit and implicitly recognized text and voice reflections end
   to end, including corrections, derived knowledge, search, comparison,
   DST/month-boundary classification, worker restart, and retry recovery.
8. Fix every discovered defect at its source and repeat the full verification.
9. Apply the production migration, deploy the complete build, and verify health,
   worker processing, reflection creation, derivations,
   retrieval, admin visibility, and absence of duplicate or cross-scope writes.
10. Observe at least one real processing/retry cycle and verify that no
    reflection-created scheduled job or proactive message exists. Do not declare
    completion merely because deployment succeeded.

There are no feature flags or intentionally dormant production paths. Staging
must prove the complete behavior before production deployment.

## Handoff requirements

The final implementation handoff must include:

- migration identifiers and apply/rollback commands;
- files and architectural contracts changed;
- test and evaluation commands with results;
- staging and production deployment identifiers;
- evidence from one explicitly labelled and one implicitly recognized reflection;
- evidence that a correction supersedes and reconciles derivations;
- evidence that derived knowledge links back to source messages;
- evidence of correct temporal classification across representative DST and
  month-end cases;
- operational queries/runbook for failed or stuck processing;
- known residual limitations, if any, stated concretely rather than hidden
  behind “future work.”

## Final instruction

Build the whole loop. Preserve the user's raw voice, structure it faithfully,
derive knowledge cautiously, connect every conclusion to evidence, make the
history retrievable, make mistakes correctable, keep the system entirely
user-initiated, make failures recoverable, and ship it fully integrated into SuperPOM. Stop only
when the production system demonstrates the complete behavior end to end.
