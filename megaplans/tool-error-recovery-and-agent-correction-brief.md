# Tool error recovery + agent correction — brief

**Profile**: `thoughtful//medium` (standard robustness, medium planner depth)

**Recommended invocation**:

```bash
megaplan init --profile thoughtful --robustness standard --depth medium
```

**Mode**: code

**Purpose**: prevent malformed model tool calls from crashing turns, and give agents a structured path to correct recoverable tool mistakes inside the same turn.

---

## Rubric Fit

This is `thoughtful//medium` work.

- **Tier**: `thoughtful`
  - Cross-cutting implementation across prompts, turn planning, tool schemas, tool execution, database boundaries, and tests.
  - Requires judgment about which errors are recoverable versus terminal.
  - Does not need `premium` because the core architecture is already known: validate before SQL, return structured tool errors, retry or fail explicitly.
- **Robustness**: `standard`
  - Production reliability matters, but this is not a data migration or public API contract sprint by itself.
  - Needs prep/plan/critique/gate/revise/finalize/execute/review because several subsystems interact.
- **Depth**: `medium`
  - Planner needs more than intuition to map the existing turn runner and tool stack.
  - No need for `high` unless inspection reveals hidden complexity in the agent loop or tool registry.

---

## Incident Context

On 2026-05-15, Peter confirmed Hector's proposed weekly fitness plan:

> Yeah, let's do it please.

The message was successfully ingested:

- Discord message id: `1504838075405041825`
- DB message id: `ddd562d1-e502-4e88-81f5-a9c7bbc741ee`
- `bot_id`: `hector`
- `processing_state`: `raw`

It triggered Hector turn:

- Turn id: `af0673bb-fb12-471e-bc40-bfda95c97146`
- Skeleton: `quick_reply`
- Final output: none
- Failure: crashed

Railway logs showed the concrete failure:

```text
ValueError: invalid UUID 'pending': length must be between 32..36 characters, got 7
asyncpg.exceptions.DataError: invalid input for query argument $1: 'pending'
/app/app/services/tools/write_tools.py ... in log_event
```

Hector attempted to call `log_event` with `commitment_id='pending'`. The tool allowed that placeholder string to reach a SQL query expecting a UUID. The database binding error escaped the tool boundary and crashed the whole turn.

---

## Goal

Make model/tool interaction resilient enough that malformed, recoverable tool calls:

- do not reach SQL when they can be validated earlier,
- do not crash the whole turn,
- return clear model-visible correction guidance,
- allow the agent to retry with the right tool sequence when safe,
- and leave durable failure metadata for the inbound queue sweeper when recovery is not possible.

For the Hector incident specifically, the correct behavior should have been:

1. Recognize "Yeah, let's do it please" as acceptance of a plan that needs recording.
2. Use a record-capable path, not a pure quick reply.
3. Search existing commitments.
4. Create or update the commitment with a real tool-returned UUID.
5. Acknowledge succinctly.

If Hector still called `log_event(commitment_id='pending')`, the tool should have rejected the call with a recoverable validation error and explicit guidance.

---

## Relationship To The Other Reliability Sprints

This sprint complements, but does not replace, the two earlier reliability briefs.

- `discord-reconnect-catchup-brief.md`
  - Recovers provider messages missed during Discord gateway disconnect windows.
  - Would not have fixed this Hector incident by itself because the message was already ingested.
- `inbound-queue-hardening-brief.md`
  - Recovers local rows that remain `raw`, stale `processing`, or retryable `failed`.
  - Would retry this incident after a cooldown, but could repeat the same crash unless this sprint also lands.
- This sprint
  - Prevents and contains the bad tool call class directly.
  - Gives the model a chance to self-correct before durable retry is needed.

The three sprints together should provide:

1. Provider-level replay for missed Discord events.
2. Local durable retry for stored but unhandled messages.
3. Safe tool boundaries and agent correction for malformed tool calls.

---

## Settled Decisions

- **SD-001** — Validate tool inputs before database calls. _load_bearing: true_
  Rationale: User/model-provided strings must not reach UUID casts, foreign-key lookups, or other strict database bindings without validation.

- **SD-002** — Recoverable validation errors should be model-visible tool results, not uncaught exceptions. _load_bearing: true_
  Rationale: The model can often fix bad arguments if the tool explains what was wrong and what to do next.

- **SD-003** — Never allow placeholder IDs. _load_bearing: true_
  Rationale: Values such as `pending`, `unknown`, `todo`, `new`, `none`, and natural-language labels are not IDs. If an ID is required, it must come from a previous tool result.

- **SD-004** — Confirmation of a proposed plan is record/update intent. _load_bearing: true_
  Rationale: "Yes, let's do it", "log it", "sounds good", and similar confirmations after a proposed routine should route to a record-capable turn, not a quick reply that lacks the right recording workflow.

- **SD-005** — Tool correction guidance should be specific, not generic. _load_bearing: true_
  Rationale: "Invalid UUID" is not enough. The agent needs guidance like "call `list_commitments`; if no match exists, call `create_commitment`; only then pass the returned commitment id."

- **SD-006** — Permanent failures still need terminal state. _load_bearing: true_
  Rationale: Some failures cannot be fixed inside the turn. They should be marked for the durable queue policy rather than retried forever.

- **SD-007** — This sprint should not build the whole durable queue sweeper. _load_bearing: true_
  Rationale: Retry scheduling and handled-state metadata are owned by `inbound-queue-hardening`. This sprint prepares clean failure classes and metadata for that system.

---

## Implementation Shape

1. **Audit tool execution boundaries**
   - Find where tool calls are parsed, validated, executed, and reported back to the model.
   - Identify which exceptions currently crash a turn versus become model-visible errors.
   - Classify existing error types into recoverable validation errors, transient infrastructure errors, and terminal failures.

2. **Add shared validation helpers**
   - Add strict UUID validation for tool parameters that reference existing DB rows.
   - Reject placeholder-like strings before database access.
   - Prefer central helpers over one-off checks inside each SQL function.

3. **Harden commitment/event tools**
   - `log_event` must not accept invalid `commitment_id`.
   - If a commitment id is supplied, it must be UUID-shaped and correspond to an accessible commitment.
   - If no commitment exists yet, the tool should tell the model to use `create_commitment` instead of inventing an id.
   - Review `create_commitment`, `close_commitment`, `get_adherence`, and related Hector tools for the same class of issue.

4. **Return structured recoverable tool errors**
   - Define a stable tool-error shape or exception class for validation failures.
   - Include:
     - machine-readable error code,
     - field name,
     - short human-readable explanation,
     - model correction hint,
     - whether retrying inside the same turn is allowed.
   - Ensure these errors are written to audit logs without marking the whole turn as crashed.

5. **Let the agent correct safe validation failures**
   - If a tool call fails with a recoverable validation error, continue the step where safe.
   - Add guardrails to avoid infinite correction loops.
   - Suggested initial cap: 2 validation-correction attempts per step, then fail the turn cleanly with metadata.

6. **Fix intent routing for confirmation turns**
   - Teach turn planning that confirmations after a proposed commitment/routine require a record-capable path.
   - Examples:
     - "Yeah, let's do it please."
     - "Yes, log that."
     - "Sounds good, make that the plan."
     - "Let's start Monday."
   - These should route to the flow that can search/create/update commitments, not only respond.

7. **Update Hector instructions**
   - Add explicit rules:
     - Never invent IDs.
     - Search before linking to an existing commitment.
     - Create a commitment when the user accepts a new plan.
     - Use `log_event` only for actual completed/missed/excused events against a real commitment.
   - Keep the prompt succinct; rely on tool validation for enforcement.

8. **Durable queue handoff metadata**
   - When a turn still fails, record enough error metadata for `inbound-queue-hardening` to decide whether retry is useful.
   - Distinguish:
     - `tool_validation_recoverable_exhausted`
     - `tool_infra_transient`
     - `model_policy_or_instruction_failure`
     - `database_unexpected`

---

## Files Expected To Change

- `app/services/tools/write_tools.py`
  - Validate UUID inputs before SQL.
  - Harden `log_event` and related commitment tools.
- `app/services/tools/`
  - Add or reuse shared validation/error helpers if the package has a natural home.
- `app/services/agentic.py`
  - Ensure recoverable tool errors can be returned to the model without crashing the turn.
  - Add retry/correction caps if this is the right layer.
- `app/services/turn_plan.py`
  - Route plan-confirmation language to a record-capable skeleton.
- `app/bots/hector.py`
  - Add concise phase-level guidance for accepted plan recording.
- `app/bots/prompts/hector.py`
  - Add high-signal tool-use primitive if needed.
- `tests/`
  - Add regression coverage for invalid placeholder IDs, recoverable tool errors, and Hector plan-confirmation routing.

Planner must inspect the current tool registry and turn runner before finalizing exact file placement.

---

## Invariants

1. **Bad tool arguments do not reach SQL.**
2. **Recoverable validation errors do not crash the turn.**
3. **The model gets actionable correction guidance.**
4. **The system does not retry validation errors forever inside one turn.**
5. **Permanent failures become explicit failure metadata for the durable queue layer.**
6. **A confirmation of a proposed plan can create/update durable records.**
7. **No duplicate commitments or duplicate replies are created by correction attempts.**
8. **Tool validation is general enough to protect all bots, not just Hector.**

---

## Edge Cases To Test

- Hector calls `log_event(commitment_id='pending')`; tool returns a recoverable validation error and no SQL UUID cast occurs.
- Hector calls `log_event(commitment_id='unknown')`; same behavior.
- Hector calls `log_event` with a valid UUID that does not exist or is not accessible; tool returns a clean not-found/tool error.
- User confirms a proposed weekly fitness routine; planner chooses a record-capable path and Hector creates the commitment.
- User reports an actual completed workout for an existing commitment; Hector can use `log_event` with a real commitment id.
- Recoverable validation error happens once, model corrects by searching/creating, turn completes.
- Recoverable validation error repeats past the cap; turn fails cleanly with retry metadata, not an uncaught exception.
- Non-validation database outage remains a transient infrastructure failure, not a prompt-correction loop.
- Same protections work for future bots and any shared commitment tools.

---

## Success Criteria

**MUST**

- Invalid UUID-like tool parameters are rejected before SQL.
- Placeholder IDs such as `pending` cannot crash a turn.
- Tool validation failures are visible to the model with concrete correction instructions.
- Hector plan-confirmation turns route to commitment creation/update behavior.
- Tests cover the exact 2026-05-15 `commitment_id='pending'` failure mode.
- Failed turns that cannot recover include structured failure metadata suitable for durable queue retry policy.

**SHOULD**

- Shared validation helpers are reused across tools that accept IDs.
- Tool-error audit events include error code, field, retryability, and correction hint.
- Correction attempts are capped and observable.
- The final implementation notes explicitly describe how this interacts with reconnect catch-up and inbound queue hardening.

**INFO**

- This sprint does not need to implement the durable queue sweeper.
- This sprint does not need to replay old Discord history.
- This sprint does not need a user-facing retry UI.

