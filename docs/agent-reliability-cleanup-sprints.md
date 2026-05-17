# Agent Reliability Cleanup Sprints

Date: 2026-05-16

## Why This Exists

The Hector incident was not one isolated failure. It exposed a distributed state machine that has grown across Discord ingestion, message rows, debouncing, agentic turns, model providers, outbound delivery, recovery, and audit/hot-context tools.

The immediate production fixes stopped the bleeding:

- Hector can receive and reply again.
- Failed inbound rows can recover through the correct bot coalescer.
- Read-phase tool loops no longer hard-crash the whole turn.
- DeepSeek failures can fall back to Anthropic.
- Provider-native message blocks are stripped before Anthropic fallback.
- `get_bot_actions` no longer crashes on the new handling metadata columns.

Those fixes are tactical. The structural cleanup should make these classes of bug hard to reintroduce.

## Core Diagnosis

The core system is:

`Discord event -> inbound row -> debouncer -> agentic turn -> tools/model/provider/outbound -> terminal message state -> recovery/audit/hot context`

That chain behaves like one state machine, but it is implemented as several loosely coupled systems:

- `messages.processing_state`
- `messages.handling_result`
- `messages.processing_attempts`
- `messages.handled_by_turn_id`
- `bot_turns.failure_reason`
- `bot_turns.final_output_message_id`
- `tool_calls`
- `turn_audit_events`
- `BurstCoalescer`
- provider-specific message formats
- recovery sweepers
- hot-context and audit SQL

The cleanup goal is not to add more ad hoc guards. The goal is to consolidate lifecycle semantics, make bot scope non-optional, normalize provider boundaries, and test the real integration paths.

## Recommended Shape

This should be more than one 2-week sprint.

One sprint can reduce the highest operational risk, but a clean solution likely needs three 2-week sprints:

1. **Sprint 1: Inbound Lifecycle And Recovery**
2. **Sprint 2: Provider Boundary And Turn Degradation**
3. **Sprint 3: Audit, Hot Context, And Production-Shaped Tests**

Sprint 1 is the most urgent. Sprint 2 prevents model/provider failures from taking down turns. Sprint 3 prevents schema/query/audit regressions from silently breaking future reliability.

## Sprint 1: Inbound Lifecycle And Recovery

### Goal

Turn inbound handling into an explicit, bot-scoped lifecycle with clear retry semantics.

At the end of this sprint, no inbound message should be able to get stranded in a state where it looks retryable but is not actually scheduled for retry.

### Main Problems Addressed

- Recovery was historically mediator-coalescer oriented.
- Failed rows could be reset to `raw` without being re-enqueued.
- Retry eligibility was inferred from overlapping fields.
- Failed turns and failed messages were not treated as one coherent attempt lifecycle.
- Bot scope was not enforced strongly enough in background paths.

### Work Items

1. Define lifecycle invariants in code and docs.

   Required invariants:

   - Every inbound row has a bot scope before it can enter the queue.
   - Every claimed inbound row has exactly one active handling attempt.
   - A failed pre-send attempt is retryable until retry cap.
   - A post-send failure is terminal from the user-visible delivery perspective.
   - A terminal row is never auto-retried.
   - Recovery always routes through the coalescer for the row's `bot_id`.

2. Add an explicit attempt ledger.

   Preferred table: `inbound_handling_attempts`.

   Suggested columns:

   - `id uuid primary key`
   - `message_id uuid not null references messages(id)`
   - `bot_turn_id uuid references bot_turns(id)`
   - `bot_id text not null`
   - `topic_id uuid not null`
   - `attempt_number int not null`
   - `status text not null`
   - `failure_class text`
   - `failure_reason text`
   - `started_at timestamptz not null`
   - `completed_at timestamptz`
   - `next_retry_at timestamptz`
   - `created_by text not null`, e.g. `live`, `catch_up`, `recovery`, `manual`

   Keep `messages.processing_state` as a fast operational summary, but make the attempt table the source of truth for retry logic.

3. Refactor claim/fail/complete helpers around the attempt ledger.

   `claim_messages_for_turn` should create or bind an attempt.

   `complete_messages` should close the active attempt and terminalize the message.

   `fail_messages` should close the active attempt with a failure class and set `next_retry_at` when retryable.

4. Replace broad recovery inference with explicit retry selection.

   Recovery should select:

   - attempts with `status='failed'`
   - `next_retry_at <= now()`
   - `attempt_number < max_retry_attempts`
   - row is not terminal
   - bot coalescer exists

   Recovery should not need to inspect `bot_turns.triggering_message_ids` except for migration/backfill compatibility.

5. Make recovery multi-bot by construction.

   Pass a `dict[str, BurstCoalescer]`, not a single coalescer.

   If a bot coalescer is missing, log a structured warning and leave the attempt retryable. Do not silently mark it recovered.

6. Add manual repair commands.

   Add a small script or admin-only function for:

   - show failed retryable inbound rows by bot
   - requeue one message
   - expire one message
   - explain why a message is not retryable

### Acceptance Criteria

- A failed pre-send Hector turn is retried by Hector, not mediator.
- A failed pre-send Tante Rosi turn is retried by Tante Rosi, not mediator.
- A failed post-send turn does not duplicate the reply.
- A row cannot become `raw` while having no route to an active coalescer or retry attempt.
- Recovery logs include bot_id, topic_id, message_id, attempt_number, and action.
- Manual requeue can explain and repair a stranded row without direct SQL.
- Focused tests cover live, catch-up, failed, stale processing, terminal replied, terminal silent, and retry cap cases.

### Tests

Unit tests:

- `inbound_queue` attempt creation and transitions.
- recovery retry selection.
- bot-scoped recovery routing.
- retry cap behavior.
- terminal no-retry behavior.

Integration tests:

- real Postgres migration test for lifecycle constraints.
- fake Discord event -> failed turn -> recovery -> successful outbound.

### Risk

This sprint touches persistence and recovery. It should be deployed with extra logging and a short observation window.

## Sprint 2: Provider Boundary And Turn Degradation

### Goal

Make model/provider failures degrade gracefully instead of causing silent user-facing failures.

At the end of this sprint, a provider failure, read-loop failure, or provider fallback mismatch should not make a normal user message disappear.

### Main Problems Addressed

- DeepSeek and Anthropic formats leaked across provider boundaries.
- Fallback was not tested with provider-native blocks.
- Read-phase tool loops could kill the whole turn.
- Tool iteration caps were uniform failure mechanisms rather than phase-aware degradation mechanisms.
- Provider 400s did not surface enough diagnostics.

### Work Items

1. Define a canonical internal message format.

   The agent runner should not store or pass raw Anthropic/OpenAI/DeepSeek blocks between providers.

   Add conversion boundaries:

   - Anthropic response -> internal blocks
   - DeepSeek response -> internal blocks
   - internal blocks -> Anthropic request
   - internal blocks -> DeepSeek request

2. Make fallback provider-independent.

   Fallback should rebuild the provider request from canonical internal messages, not mutate provider-native request history.

   Fallback policy:

   - Try configured provider.
   - Retry once on transient/provider-class errors.
   - Fall back to Anthropic for user-visible steps.
   - If fallback fails, mark pre-send failure retryable.
   - Do not fallback after user-visible send in a way that might duplicate output.

3. Add phase-aware cap behavior.

   Suggested behavior:

   - `read`: stop reading and advance.
   - `consult`: skip consult and continue.
   - `respond`: if no output yet, retry/fallback; if still failing, mark retryable.
   - `record`: do not affect delivered reply; record failure as non-user-facing.
   - `schedule`: do not affect delivered reply; record failure as non-user-facing.

4. Add visible failure classification.

   Standard classes:

   - `model_provider_bad_request`
   - `model_provider_timeout`
   - `tool_validation_recoverable`
   - `tool_infra_transient`
   - `db_query_bug`
   - `delivery_provider_failure`
   - `post_send_record_failure`

5. Improve provider error logging.

   For provider HTTP errors, log:

   - provider
   - status code
   - request id if available
   - sanitized error body
   - model
   - turn_id
   - step
   - bot_id

   Do not log secrets or full user content.

### Acceptance Criteria

- DeepSeek 400 in read step falls back or advances without losing the turn.
- DeepSeek-native assistant blocks never reach Anthropic.
- Anthropic-native blocks never reach DeepSeek unless explicitly converted.
- Read loop exhaustion does not prevent a response.
- Record/schedule failure after a response does not change the inbound message from `replied`.
- Provider fallback has tests for tool calls, text responses, and mixed tool/text history.

### Tests

Unit tests:

- canonical message conversion round trips.
- DeepSeek -> Anthropic fallback with tool history.
- Anthropic -> DeepSeek request build with tool results.
- phase cap behavior.

Integration tests:

- fake provider 400 during read.
- fake provider 400 during respond before send.
- fake provider 400 during record after send.

## Sprint 3: Audit, Hot Context, And Production-Shaped Tests

### Goal

Make audit and context queries reliable enough that bots can reason about their own actions without crashing turns.

At the end of this sprint, schema additions to messages/turns/tool calls should not break production SQL paths unnoticed.

### Main Problems Addressed

- `get_bot_actions` broke because selected fields were not in `GROUP BY`.
- Fake DB tests did not catch Postgres grouping rules.
- Hot context and audit tools are schema-sensitive.
- Bots depend on audit tools to answer “what did you do?” and “did you set that up?”

### Work Items

1. Identify all production SQL paths that aggregate turns, messages, and tool calls.

   Key areas:

   - `get_bot_actions`
   - hot context recent turns
   - silent turns since last message
   - tool call drilldown
   - admin audit routes

2. Add real Postgres integration tests for query-heavy tools.

   Use a local test Postgres, container, or Railway-compatible ephemeral database.

   FakePool remains useful for fast unit tests, but it is not enough for SQL correctness.

3. Create query fixtures.

   Fixture scenarios:

   - successful replied turn
   - silent turn
   - failed pre-send turn
   - failed post-send turn
   - turn with multiple tool calls
   - turn with no tool calls
   - turn with inbound message handling metadata
   - multi-bot turns in same user thread

4. Standardize audit outputs.

   `get_bot_actions` should expose:

   - triggering content
   - final outbound content
   - handling result
   - processing error
   - failure reason
   - tool calls
   - audit events
   - delivery status

   It should not require the model to infer failure state from scattered fields.

5. Add a “why no reply?” diagnostic.

   Build a read/admin tool that takes a message id or provider message id and returns:

   - inbound row state
   - current/last attempt
   - bot turn ids
   - tool calls
   - final outbound id if any
   - retry eligibility
   - next retry time
   - recommended repair action

   This would have shortened the Hector investigation substantially.

### Acceptance Criteria

- `get_bot_actions` passes against real Postgres with all fixture shapes.
- Hot context rendering passes against real Postgres with all fixture shapes.
- “Why no reply?” diagnostic explains the Hector-class incident without hand-written SQL.
- Adding a selected column to an aggregate query fails tests unless grouped/aggregated correctly.
- Audit tools are bot-scoped and cannot leak another bot’s internal actions unless explicitly allowed.

## Sequencing

Recommended order:

1. Sprint 1 first.
2. Sprint 2 second.
3. Sprint 3 third.

Reasoning:

Sprint 1 addresses message loss and retry correctness. Without that, provider and audit improvements still leave messages strandable.

Sprint 2 addresses user-facing degradation. Without that, the system can still technically recover while repeatedly failing user turns.

Sprint 3 hardens the observability and reasoning surface. It is important, but it should not block the lifecycle and fallback fixes.

## What A Single 2-Week Sprint Could Do

If only one sprint is available, do a compressed “Reliability Core” sprint:

1. Make recovery fully bot-aware.
2. Add attempt ledger or at least attempt-like structured retry rows.
3. Make failed pre-send rows retryable and re-enqueued.
4. Make read/consult caps degrade.
5. Add provider fallback sanitization.
6. Add real Postgres tests for `get_bot_actions`.
7. Add a basic “why no reply?” admin query.

This is enough to reduce operational risk, but it will leave cleanup debt around canonical provider message formats and full audit-query coverage.

## What Not To Do

- Do not add another loose sweeper that scans `messages` with different inference rules.
- Do not rely on `processing_state='raw'` alone as proof that a row will be retried.
- Do not make recovery single-bot or mediator-special again.
- Do not pass provider-native message blocks across fallback boundaries.
- Do not treat all tool caps as fatal.
- Do not rely only on FakePool for SQL-heavy tools.
- Do not silently repair rows without an audit trail.

## Suggested Megaplan Profiles

Use `Thoughtful` for all three sprints.

Recommended settings:

- Sprint 1: `Thoughtful`, robustness `high`, Codex primary, DeepSeek secondary review.
- Sprint 2: `Thoughtful`, robustness `standard-high`, Codex primary, DeepSeek provider-boundary review.
- Sprint 3: `Thoughtful`, robustness `standard`, Codex primary, Postgres-focused review gate.

Sprint 1 deserves the strongest robustness because it touches persistence, retries, and production recovery. Sprint 2 is also important but can be kept smaller if the canonical message boundary is well scoped. Sprint 3 is query/test heavy and should be strict on evidence rather than broad on implementation.

## Definition Of Done For The Whole Program

The cleanup is done when these are all true:

- For any inbound Discord message id, an operator can answer “why did or didn’t the bot reply?” in one command.
- A failed pre-send Hector turn retries through Hector and either sends or reaches an explicit retry cap.
- A failed post-send turn never duplicates a user-visible reply.
- A DeepSeek failure cannot strand a message if Anthropic is available.
- A read-phase loop cannot suppress a response.
- Audit tools do not crash under production Postgres.
- Every background path is bot-scoped.
- Production logs contain structured lifecycle events, not just stack traces.
