# Incremental Agent Sending Plan

## Purpose

Design a lightweight architecture for agent-led incremental Discord messaging.
The target behavior is not "split a completed answer into chunks." The target
behavior is that the agent can decide, during the normal Phase A reasoning loop,
to send a user-visible part now, receive an authoritative result describing what
actually happened, continue with that delivered context in view, and stop
sending more parts if the turn has been interrupted.

The core invariant is that delivery state belongs to the runtime, not the model.
The model may choose when a message part is worth sending, but only a durable
outbound row that has passed the normal guardrails and reached provider
acknowledgement counts as sent. That actual-send record is then the source of
truth for `sent_so_far`, onboarding state, audit context, retry recovery, and the
Phase B seed that writes memories, observations, themes, watch items, and other
durable state.

The first transport target is Discord, because Discord already has pacing
infrastructure and does not need the WhatsApp 24-hour/template split. The design
should still be general enough that other providers can opt in later without
changing the agent contract.

## Current Shape

The existing live assistant turn is a two-phase lifecycle in
`app/services/agentic.py`.

Phase A opens a `bot_turns` row, builds hot context, renders the system prompt,
and runs `run_phase(...)` with `READ_PHASE_TOOLS`. The read loop calls Anthropic
with the tool schemas from `to_anthropic_tools(...)`, appends assistant content
blocks to the transcript, executes model-requested tools through
`call_tool(...)`, appends structured `tool_result` blocks back into the
Anthropic transcript, and repeats until the model returns final text rather than
another tool-use stop.

After Phase A, the current implementation treats that final text as one
candidate outbound. It strips user-facing cleanup artifacts, extracts optional
Discord reaction directives, performs an agentic-layer OOB precheck through
`_resolve_outbound_text(...)`, and then calls `send_outbound(...)` for the
single text reply. If a Discord reaction is successfully applied instead of a
text reply, the turn records that as a user-visible action and claims onboarding.

Phase B then switches the same `TurnContext` to `phase="write"`, seeds the model
with the Phase A transcript plus a synthetic "You sent: ..." message, and runs
`run_phase(...)` again with `WRITE_PHASE_TOOLS`. That second phase is where
memory, observation, theme, watch-item, OOB, scheduling, feedback, and partner
escalation writes happen. The ordering is intentional: current tests assert that
the outbound is recorded before Phase B writes, so durable state is informed by
what the assistant actually said rather than by an unsent draft.

`send_outbound(...)` in `app/services/messaging.py` is the normal delivery edge.
It owns global pause and per-user pause handling, final OOB checks, withheld
review rows for block/rewrite verdicts, provider-window behavior for non-Discord
transports, provider retries, outbound `messages` row insertion, provider
message-id capture in `whatsapp_message_id`, `processing_state` transitions, and
onboarding welcome claims after successful provider acknowledgement. Discord is
handled as a first-class provider inside this same edge by calling
`discord.send_text(...)`; the legacy `whatsapp_message_id` column currently
stores provider message ids for both WhatsApp-shaped and Discord sends.

Discord pacing is already a pre-turn concern, documented in
`docs/discord-pacing.md`. Live Discord inbound messages enter the normal inbound
path and then `DiscordPacer` may wait, react, silence, or answer. When it answers
with pacing metadata, the agentic send path suppresses the lower-level Discord
typing indicator so the pre-answer typing pulse is not duplicated. Incremental
sending should build on this transport context: it can make Discord answers feel
more natural, but it must not bypass pacing gates, pause checks, final delivery
guardrails, or provider acknowledgement.

The tool system has two required surfaces. `tool_schemas.py` is the source of
truth for input and output Pydantic models and the `TOOL_REGISTRY` entries that
Anthropic sees. `app/services/tools/registry.py` maps those schema entries to
descriptions, phase allowlists, and implementation functions. A primitive added
only inside `agentic.py` would not be exposed to the model as an Anthropic tool,
would not pass phase gating, and would not get validated tool-result behavior.

## Problem Framing

The current turn shape is safe and simple, but it forces the assistant to wait
until the end of Phase A before any text reaches the user. That is a mismatch for
Discord conversations where users often send short bursts, expect visible
progress, and may interrupt while the assistant is still thinking. It is also a
mismatch for responses that naturally have semantic stages: acknowledgement,
clarifying setup, a concrete suggestion, and a follow-up question are not just
paragraph boundaries; they are conversational moves that may depend on tool
results and user interruption timing.

The architecture needs to let the model make those moves explicitly while
keeping the runtime in charge of truth. After each attempted part, the model
must learn whether the part was sent, rewritten, blocked, withheld, duplicated,
or abandoned because the turn was interrupted. Future reasoning in the same
Phase A loop must be based on that authoritative result, not on the draft the
model hoped to send.

The design also has to preserve the existing separation between user-visible
delivery and durable state writes. Phase B cannot be seeded from the model's
ideal final answer once Phase A can contain multiple delivery attempts. It must
be seeded from the actual delivered ledger: provider-acknowledged text parts and
successful Discord reactions, excluding blocked, withheld, failed, duplicate
without new send, or interrupted parts.

## Why Not Paragraph Splitting

Post-hoc paragraph splitting is the wrong abstraction for this system.

It cannot preserve semantic boundaries. A paragraph may contain several
conversational acts, and a single conversational act may span multiple lines. The
agent needs to decide "send this acknowledgement now" or "wait until the search
result returns before asking the question," not rely on typography after it has
already committed to a complete answer.

It cannot adapt to tool results. In the current Anthropic loop, tool calls and
tool results are part of the reasoning transcript. Incremental sending must fit
inside that loop so the model can send a part, read the actual send result, call
another read tool if needed, and choose the next move. Splitting final text after
the loop is over hides delivery outcomes from the reasoning process.

It cannot provide accurate `sent_so_far` visibility. The assistant must see what
actually reached the user, including rewrites and provider acknowledgements, and
must not treat blocked or withheld drafts as delivered. A final-answer splitter
only sees generated text; it does not know which chunks passed OOB, which chunks
were deduplicated during retry, or whether a newer inbound message arrived
before a provider call.

It cannot make interruption behavior correct. If the user sends a newer inbound
message while the assistant is mid-turn, the runtime must stop further sends.
Paragraph splitting would already have a completed answer queued for delivery,
which encourages draining stale chunks after the conversation has moved on.

It cannot make Phase B correct. Memories, observations, themes, watch items, and
audit records should reflect what the assistant actually did. If Phase B is told
about a complete answer that was later split, partly blocked, or interrupted
midstream, durable state will drift away from user-visible reality.

## `send_message_part` Primitive

Add one new agent-callable primitive: `send_message_part`. It is a Phase A
delivery tool, not a Phase B write tool and not an internal-only helper. The
model should call it only when it has a coherent user-visible conversational
move that is ready to attempt delivery. The primitive is the only way an
incremental text part counts as an attempted send during the read loop.

The primitive should be exposed first for Discord turns. When the active
transport is not Discord, or when incremental sending is not enabled for the
turn, the tool should not be included in the Anthropic tool list. Other
providers stay on the existing final-text path until they receive explicit
product and safety review. This keeps the initial rollout aligned with Discord
pacing while preserving a provider-general contract.

The input should stay intentionally small:

- `content`: required user-visible text to attempt. It must be plain text, not
  scratch reasoning, separators, hidden notes, or a batch of unrelated future
  parts.
- `metadata`: optional structured hints for observability and future transport
  policy, such as `kind`, `tone`, `sequence_role`, or a short
  `reason_for_sending_now`. Metadata is not authoritative delivery state.
- `client_part_key`: optional model-supplied hint for semantic grouping, useful
  for diagnostics and possible de-dupe analysis. It must never be required for
  correctness, retry safety, or database uniqueness.

The runtime must generate the authoritative `part_key`. The model cannot be
trusted to create stable, unique, retry-safe keys, and Anthropic retry behavior
can replay the same tool call without the model understanding that a provider
ack already happened. The generated key should be tied to the bot turn and tool
attempt in a durable way, then reserved before provider delivery. Later ledger
sections define the exact storage shape, but this section sets the contract:
`part_key` is runtime-owned, durable, and the primary idempotency handle for an
incremental part.

Each call returns a structured result that is appended back into the Anthropic
transcript through the existing `tool_result` mechanism. The model should see
the actual outcome before deciding whether to continue, stop, ask another read
tool, or let final-text fallback handle any unsent remainder.

The result should include at least:

- `status`: one of `sent`, `rewritten_and_sent`, `blocked`, `withheld`,
  `duplicate`, `interrupted`, `provider_failed`, or `not_enabled`.
- `part_key`: the runtime-generated key for this attempted part.
- `client_part_key`: the original hint if one was supplied.
- `message_id`: the outbound `messages.id` when a row exists.
- `provider_message_id`: the Discord message id when provider acknowledgement
  exists.
- `delivered_content`: the exact text that counts as sent, present only for
  provider-acknowledged outcomes.
- `visible_to_user`: boolean, true only when the user actually received this
  part.
- `sent_so_far`: ordered delivered-content summary reconstructed from the
  actual-send ledger, not from model drafts.
- `reason`: short runtime explanation for blocked, withheld, interrupted,
  duplicate, failed, or disabled outcomes.

Blocked, withheld, interrupted, disabled, and provider-failed results are still
valid tool results. They should not be represented as Anthropic tool execution
errors unless the primitive itself crashed. The agent needs to reason from these
outcomes normally; for example, after a `blocked` result it should not re-send
the same unsafe content, and after an `interrupted` result it should stop further
user-visible sends for the turn.

## Anthropic Tool Exposure

`send_message_part` must be wired through both existing tool surfaces.

In `tool_schemas.py`, add `SendMessagePartInput` and `SendMessagePartOutput`
Pydantic models plus a `TOOL_REGISTRY` entry named `send_message_part`. This is
what makes the JSON schema visible to Anthropic and lets the orchestrator
validate both model input and runtime output. The schema descriptions should
make clear that `content` is user-visible, `client_part_key` is optional and
non-authoritative, and the runtime returns the authoritative `part_key`.

In `app/services/tools/registry.py`, add the tool description, dispatch entry,
and phase allowlist entry. It belongs in `READ_PHASE_TOOLS`, because the send
decision happens during Phase A while the model is still reading, responding,
and adapting. It must not be added to `WRITE_PHASE_TOOLS`; Phase B should record
state from delivered content, not send new user-visible text.

The implementation function can live beside other tool implementations or in a
small dedicated module, but registry dispatch must call it through the same
`call_tool(...)` path as every other Anthropic tool. That preserves validation,
phase rejection, captured tool-call records, structured `tool_result` blocks,
and existing tests around unknown tools, invalid arguments, and result schema
validation.

Tool availability should be turn-scoped. The Phase A allowed-tools set includes
`send_message_part` only when all of these are true:

- the current provider is Discord;
- incremental sending is enabled by configuration or experiment gate;
- the turn is a live answer path where user-visible text is allowed;
- the runtime can create or recover the durable send ledger needed for
  idempotency.

If those conditions are not met, omit the tool from Anthropic's tool list rather
than exposing a tool that usually returns `not_enabled`. The `not_enabled`
status still exists as a defensive result for stale transcripts, tests, or
misconfigured allowlists.

The tool description should avoid implying a response style. It should not tell
the model to split paragraphs, send every sentence, or always stream. It should
say that the tool sends one coherent user-visible message part when sending now
is conversationally useful, and that the returned `sent_so_far` is the authority
for what the user has actually seen.

## Actual-Send Ledger

Use normal outbound `messages` rows as the source of truth for user-visible
incremental sends. The system already records outbound content, recipient,
provider id, `processing_state`, and `sent_at` in `messages`; audit screens,
feedback, reactions, hot context, and `bot_turns.final_output_message_id` already
understand that table. Incremental sending should extend that path rather than
invent a separate primary message history.

The preferred schema is minimal `messages` extensions:

- `bot_turn_id`: nullable reference to `bot_turns.id`, set for assistant sends
  created by a turn.
- `part_key`: nullable text or uuid, set for incremental parts and unique when
  present.
- `client_part_key`: nullable text, storing the optional model hint for
  diagnostics only.
- `part_index`: nullable integer, assigned by the runtime to order delivered
  parts inside a turn.
- `delivery_kind`: nullable text, for values such as `final`, `incremental`,
  `reaction`, or future transport-specific categories if needed.
- `delivery_metadata`: nullable jsonb for non-authoritative metadata such as
  sequence role, runtime attempt counters, or OOB rewrite notes.

If schema review finds that adding these columns would overload `messages`, use
a small linked ledger table instead:

- `message_parts.id`
- `message_parts.bot_turn_id`
- `message_parts.message_id` referencing `messages.id`
- `message_parts.part_key`
- `message_parts.client_part_key`
- `message_parts.part_index`
- `message_parts.status`
- `message_parts.delivery_metadata`
- timestamps for reservation, provider acknowledgement, and terminal failure

Even with a linked table, the normal outbound `messages` row remains the source
of delivered content. The ledger table may track incremental metadata, but it
must not become a second message transcript with divergent content.

Only provider-acknowledged outbound rows count as sent. For text parts, that
means `direction='outbound'`, the relevant turn or part metadata is present,
`processing_state='processed'`, and the provider message id is populated. For
Discord reactions, the equivalent delivered ledger entry is a successful
reaction operation associated with the triggering inbound message; if reactions
are represented through `messages`, they need an explicit `delivery_kind` rather
than pretending to be text.

Blocked, withheld, interrupted, and provider-failed attempts may create rows for
audit or review, but they are not delivered rows. They must be excluded from
`sent_so_far`, onboarding claims, Phase B seeding, and any "the user saw this"
reasoning.

## Idempotency And Retry Semantics

Idempotency must be enforced before provider delivery. The runtime should create
or reserve the authoritative `part_key` in durable storage before it calls
Discord. A unique constraint on `(bot_turn_id, part_key)` or globally unique
`part_key` prevents a replayed tool call, process retry, or Anthropic retry from
sending the same part twice.

The reservation flow should be:

1. Receive a validated `send_message_part` tool call.
2. Generate the runtime `part_key` and assign the next runtime `part_index`.
3. Insert or reserve the durable row under a uniqueness constraint.
4. Run interruption and delivery guard checks.
5. Call the provider only if the reserved row is still sendable.
6. Update the row with provider acknowledgement and `processing_state` after
   the provider returns success.

If insertion hits an existing `part_key`, the primitive returns `status:
duplicate` and does not call Discord again. The result should include the
existing `message_id`, `provider_message_id`, `delivered_content`, and
`sent_so_far` if the prior attempt was acknowledged. If the existing row is
withheld, blocked, interrupted, or failed, the duplicate result should report
that terminal state rather than retrying blindly under the same key.

Retry-after-ack recovery should be deterministic. If the process sends to
Discord, receives a provider id, and crashes before the Anthropic loop sees the
tool result, recovery reconstructs the acknowledged row from the ledger and
returns a duplicate-style result to any replayed tool call. The user should not
receive a second Discord message just because the model or process retries.

Provider failure is terminal for that reserved attempt unless a human or a
separate recovery job explicitly decides otherwise. If all provider retries fail,
mark the row `expired` or a future terminal failure state, record the failure
reason in metadata or turn reasoning, and return `status: provider_failed` with
`visible_to_user: false`. Do not include the failed content in `sent_so_far`, do
not claim onboarding, and do not seed Phase B as if the user saw it.

The runtime may still allow the model to send a revised follow-up part after a
failure, but that must use a new runtime `part_key`. Reusing a failed key must
report the existing failure; it must not create a second provider attempt that
breaks retry safety.

## Reconstructing `sent_so_far`

`sent_so_far` should always be reconstructed from acknowledged ledger rows, not
kept as mutable conversation state inside the model loop. The query should order
by runtime `part_index` and then by acknowledgement timestamp or message
creation timestamp as a tie-breaker. It should include only rows that are known
visible to the user.

For the initial Discord text path, `sent_so_far` can be a compact ordered list
of delivered parts:

- `part_key`
- `message_id`
- `provider_message_id`
- `delivered_content`
- `sent_at` or provider acknowledgement time

The `send_message_part` tool result can include both that structured list and a
short human-readable summary if needed for the model. The structured list is the
contract; summaries are convenience text and must not become the source of truth.

Reconstruction also handles turn recovery. If Phase A partially delivered two
parts and then crashed or was interrupted, the next lifecycle step can inspect
the ledger and know exactly what the user saw. That same reconstructed view is
what later sections should use for final-text fallback de-duping, audit context,
onboarding decisions, and Phase B write seeding.

## Safety Guardrails Per Part

Every `send_message_part` call must route through the same final delivery
guardrail as ordinary outbound text. The primitive may do lightweight validation
before it reserves a `part_key`, but it must not bypass `send_outbound(...)` or
an equivalent shared delivery function for OOB, pause, provider retry, provider
acknowledgement, and withheld-review behavior.

The OOB check must run for each user-visible part using the same protected-owner
scope as the final text path. For a dyad turn, that means the current user and
partner ids remain protected when checking text intended for the current user.
The model should not be allowed to argue that a part is "only an intro" or "only
temporary" and skip the final OOB check; if the user can see it, it goes through
the delivery guardrail.

OOB outcomes should be represented as normal structured tool results:

- `sent`: OOB allowed the original text and provider acknowledgement succeeded.
- `rewritten_and_sent`: the delivery guardrail produced a safe rewrite and that
  exact rewrite was provider-acknowledged.
- `blocked`: OOB blocked the part; the row may exist for audit or review, but
  `visible_to_user` is false and `delivered_content` is absent.
- `withheld`: the guardrail requires manual review, template deferral, or pause
  withholding; it is not included in delivered state.

For rewrite outcomes, `delivered_content` must be the rewritten text, not the
model's original draft. The tool result should include a short reason and enough
metadata for audit, but it must not expose protected OOB contents back into the
model transcript.

## Interruption And Pause Handling

Incremental sending needs an explicit stop condition because the assistant may
be mid-Phase A while the user sends another message. Before every provider call,
the runtime should check:

- global pause through `system_state.is_paused(pool)`;
- per-user pause through the same hook path used by `send_outbound(...)`;
- whether a newer inbound message for the user arrived after the triggering
  message set or after the turn started;
- whether the reserved part row is still in a sendable state.

If any stop condition is true before provider delivery, the primitive returns
`status: interrupted` or `status: withheld` as appropriate and does not call
Discord. The result must make `visible_to_user: false`, preserve the current
`sent_so_far` reconstructed from prior acknowledged parts, and instruct the
agent loop to stop further user-visible sends for this turn.

Newer inbound detection should be conservative. The simplest initial check is a
query for inbound `messages` from the same user with `sent_at` later than the
turn start or with ids outside the triggering set that are still raw or newer
than the latest trigger. If that query finds a message, the current turn should
finish without additional sends; the coalescer or recovery path can schedule a
fresh turn around the newer user input.

Pause handling must stay effective even after a `part_key` is reserved. A global
or per-user pause that appears between reservation and provider delivery should
leave an audit trail but not deliver the part. Paused or interrupted reserved
rows are not retries waiting to happen; replaying the same key should report the
same terminal non-visible outcome unless a deliberate operator path reopens it.

## Onboarding And Phase B Guarantees

Onboarding may be claimed only after actual user-visible delivery. For text
parts, that means provider acknowledgement through the delivery edge. For
Discord reaction-only handling, it means the reaction API call succeeded. A
blocked, withheld, interrupted, disabled, duplicate-without-new-ack, or
provider-failed result must not move a user from `pending` to `welcomed`.

This requirement should also remove any double-claim ambiguity in the current
agentic path. Today `send_outbound(...)` claims onboarding after provider
acknowledgement, and `agentic.py` also claims after final send/reaction success.
The incremental design should keep the semantic rule simple: the component that
knows the provider-visible action succeeded may claim onboarding, and callers
should not claim based on a draft or merely on the existence of an outbound row.

Phase B must be seeded from the delivered ledger only. After Phase A ends,
whether it ended naturally, after partial sends, after interruption, or after a
provider failure, the Phase B seed should say what the user actually saw:
acknowledged text parts in order and successful reactions. It should exclude
blocked drafts, withheld review content, failed provider attempts, disabled tool
calls, and interrupted reservations.

The recommended default is to still run Phase B after partial successful sends,
using the delivered ledger as the source of truth. That lets the assistant
record memories, observations, themes, watch items, and audit context based on
what it actually communicated. If no user-visible part was delivered, Phase B
may still run for non-message state updates only when the existing lifecycle
would have done so; it must not be told that a blocked or failed draft was sent.

If the user interrupts after one or more delivered parts, Phase B should receive
both facts: the delivered content so far and the interruption state. That lets
it avoid writing overconfident conclusions from a half-finished response while
still preserving useful audit and memory context from the parts the user did
see.

## Agent Loop Integration

Keep the existing two-phase lifecycle. Incremental sending changes what Phase A
can do while it is reasoning; it does not turn the application into a streaming
pipeline and it does not move durable write tools into Phase A.

Phase A should compute its allowed tools dynamically. Start from
`READ_PHASE_TOOLS`, then add `send_message_part` only when the turn satisfies
the Discord-first enablement conditions. Pass that enabled set into
`run_phase(...)` so `to_anthropic_tools(...)` renders the tool schema only for
eligible turns. Phase B continues to receive only `WRITE_PHASE_TOOLS`.

The existing `run_phase(...)` loop is already the right shape for Anthropic
tool-use integration. When the model calls `send_message_part`, `call_tool(...)`
validates the input, dispatches the implementation, validates the structured
output, records the tool call, and appends a `tool_result` block into the
Anthropic transcript. The next model iteration sees the authoritative outcome,
including `sent_so_far`, and can decide whether to stop, call another read tool,
send another coherent part, or leave remaining text for final fallback.

The Phase A prompt should be adjusted from "produce only the user-facing reply
as plain text" to explain the new contract when the tool is enabled: send a
coherent part through `send_message_part` when it is useful for the user to see
it now, then continue only from the returned actual-delivery state. The prompt
must not ask for paragraphs, chunks, or streaming cadence. When the tool is not
enabled, the current single final-text behavior remains the contract.

The agent loop should track an interruption flag returned by the primitive. If
any `send_message_part` result reports `interrupted` or another terminal
stop-sending condition, Phase A should stop additional user-visible delivery for
that turn. It may finish the current model loop only as needed to complete turn
bookkeeping, but it should not expose `send_message_part` again or send final
fallback text after the interruption.

## Final-Text Fallback

Keep a final-text fallback initially. The model may still finish Phase A with
plain text because the tool was disabled, because no incremental send was
needed, or because it sent some parts and then returned a short remaining close.
Fallback preserves compatibility with current response styles and reduces
rollout risk.

When incremental sending was enabled, final fallback must use the same delivery
primitive rather than calling a separate untracked send path. Treat the final
text as a final part with `delivery_kind='final'`, run it through the same OOB,
pause, interruption, idempotency, and provider-acknowledgement path, and append
the same structured outcome into the turn's delivery ledger.

Fallback also needs de-duping against `sent_so_far`. Before attempting final
delivery, reconstruct acknowledged delivered content for the turn. If the final
text is empty, entirely covered by previously delivered parts, or only repeats a
previously acknowledged close, do not send it. Return or record a duplicate/no-op
outcome and seed Phase B from the delivered ledger.

The de-dupe policy should start conservative:

- exact normalized match against any delivered part: do not send again;
- final text equal to the concatenation of delivered parts: do not send again;
- final text with a delivered prefix and a genuinely new suffix: send only the
  suffix if it is coherent on its own;
- uncertain overlap: prefer not sending and rely on Phase B/audit to record the
  partial delivered state.

This fallback is a compatibility bridge, not the primary abstraction. The model
should learn from `sent_so_far` during Phase A and avoid producing a final answer
that restates already delivered parts.

## Transport Boundary

Keep provider behavior at the delivery edge. The `send_message_part` tool should
not know how to format Discord REST payloads, WhatsApp templates, typing
indicators, or provider retry loops. It should reserve delivery state, run
turn-level interruption checks, and then call the shared outbound edge that owns
provider behavior.

For the first implementation, Discord is the only provider that receives
incremental text sends. Discord pacing remains pre-turn and transport-facing:
the incremental primitive can pass through the same `send_typing_indicator`
behavior used by paced final sends, but it should not implement pacing policy
itself. WhatsApp/Meta/Twilio remain on the existing single-send path until
explicitly enabled.

Provider acknowledgement should flow back into the same ledger fields regardless
of transport. The current column name `whatsapp_message_id` is a legacy
provider-id field; the implementation may keep using it initially for Discord
ids, but the architecture should treat it as "provider message id" and avoid
hard-coding WhatsApp semantics into the incremental primitive.

## Implementation Touch Points

Future implementation should touch these areas:

- `tool_schemas.py`: add `SendMessagePartInput`, `SendMessagePartOutput`,
  status literals/enums if useful, delivered-part result shapes, and the
  `TOOL_REGISTRY` entry.
- `app/services/tools/registry.py`: add the description, `TOOL_DISPATCH` entry,
  and read-phase allowlist behavior. If the existing static `READ_PHASE_TOOLS`
  set stays static, add a small helper that returns per-turn read tools with
  `send_message_part` included only when enabled.
- `app/services/tools/write_tools.py` or a new
  `app/services/tools/send_message_part.py`: implement the tool function. A
  dedicated module is cleaner if the logic owns ledger reservation,
  interruption checks, and `sent_so_far` reconstruction.
- `app/services/agentic.py`: compute turn-scoped Phase A tools, update the Phase
  A prompt when incremental sending is enabled, preserve the existing
  `run_phase(...)` transcript behavior, route final-text fallback through the
  same primitive, and seed Phase B from the delivered ledger instead of the
  current synthetic `You sent: ...` text.
- `app/services/messaging.py`: factor or extend `send_outbound(...)` so
  incremental parts can reserve idempotency state before provider delivery while
  still using the same OOB, pause, provider retry, provider id update, withheld
  review, and onboarding semantics.
- `app/services/discord.py`: no policy ownership; only provider delivery and
  reaction calls should remain here.
- `app/services/turn_context.py`: add any minimal turn-scoped fields needed for
  incremental enablement, interruption state, or delivery helper access.
- `migrations/`: add the minimal `messages` columns and indexes, or the linked
  ledger table if schema review chooses that path. Include uniqueness for the
  runtime `part_key`.
- `tests/conftest.py`: update the fake pool to understand the new columns,
  uniqueness behavior, provider acknowledgement states, and `sent_so_far`
  queries.
- `tests/test_agentic.py`, `tests/test_agentic_lifecycle.py`,
  `tests/test_send_outbound.py`, and `tests/test_tool_schemas_importable.py`:
  add focused coverage around tool exposure, lifecycle ordering, duplicate
  sends, and final fallback.

The implementation should avoid putting the primitive only in `agentic.py`.
That would make it invisible to Anthropic's tool schema renderer and bypass the
registry validation path. It should also avoid creating a provider-specific
Discord send tool; Discord is the first enabled transport, not the conceptual
owner of the architecture.

## Validation Strategy

Validation should start with focused tests around the new contract before
running the broader suite. The risky behavior is not text formatting; it is
whether the runtime records actual delivery state correctly and keeps later
reasoning tied to that state.

Add lifecycle-ordering tests in `tests/test_agentic_lifecycle.py` and
`tests/test_agentic.py`:

- Phase A exposes `send_message_part` only for enabled Discord turns.
- Phase B does not expose `send_message_part` and rejects a stale call with a
  phase error if one appears.
- A successful part is recorded and provider-acknowledged before Phase B writes
  memories, observations, themes, or watch items.
- Phase B seed text/list is built from the delivered ledger, not from model
  drafts or the final assistant text.
- Final-text fallback runs through the same primitive and does not send content
  already present in `sent_so_far`.

Add interruption tests:

- If a newer inbound message exists before provider delivery, the tool returns
  `interrupted`, makes no Discord API call, and stops later user-visible sends
  in the turn.
- If global pause or per-user pause is enabled after reservation but before
  provider delivery, the result is non-visible and no provider call happens.
- If one part was acknowledged before interruption, Phase B receives only that
  delivered part plus interruption state.

Add OOB and withheld-delivery tests in `tests/test_send_outbound.py`,
`tests/test_agentic.py`, or a dedicated send-part test module:

- OOB `block` produces a structured `blocked` tool result, creates only the
  intended audit/review state, excludes the draft from `sent_so_far`, and does
  not seed Phase B with the blocked content.
- OOB rewrite produces `rewritten_and_sent` only when the rewritten content is
  provider-acknowledged, and `delivered_content` equals the rewrite.
- Withheld review or template deferral returns `withheld`, does not claim
  onboarding, and is excluded from delivered ledger reconstruction.

Add tool schema and registry tests:

- `tests/test_tool_schemas_importable.py` imports the new input/output models
  and confirms `send_message_part` appears in `TOOL_REGISTRY`.
- `tests/test_llm_phase.py` or `tests/test_tools.py` confirms the tool appears
  in Phase A only when the enabled tool set includes it.
- The same phase-gating tests confirm Phase B rejects `send_message_part`.
- Result validation fails closed if the implementation returns a shape that
  omits required fields such as `status`, `part_key`, or `sent_so_far`.

Add idempotency and retry tests:

- A duplicate runtime `part_key` returns the existing acknowledged result and
  does not make another Discord API call.
- A retry after provider acknowledgement reconstructs the existing row and
  returns the acknowledged `message_id`, provider id, delivered content, and
  `sent_so_far`.
- A duplicate key for a failed, blocked, withheld, or interrupted attempt
  reports that terminal state and does not silently retry provider delivery.

Add onboarding tests:

- Provider-acknowledged text advances `onboarding_state` from `pending` to
  `welcomed`.
- Successful Discord reaction can advance onboarding.
- `blocked`, `withheld`, `interrupted`, `provider_failed`, and duplicate
  non-visible outcomes do not advance onboarding.
- Duplicate replay of an already acknowledged part does not create a second
  claim path or require another provider call.

Add migration and fake-pool support:

- Migration tests or schema smoke tests cover the new `messages` columns or the
  linked ledger table, including the unique idempotency constraint.
- `tests/conftest.py` fake pool supports inserts, updates, uniqueness errors,
  provider acknowledgement updates, and `sent_so_far` reconstruction queries.
- Existing fake-pool behavior for ordinary `send_outbound(...)` remains
  compatible with non-incremental sends.

Run targeted tests first:

```bash
pytest tests/test_tool_schemas_importable.py tests/test_tools.py tests/test_llm_phase.py
pytest tests/test_send_outbound.py tests/test_agentic_lifecycle.py tests/test_agentic.py
pytest tests/test_discord.py tests/test_pacer.py
```

After the focused set passes, run the broader suite:

```bash
pytest
```

The implementation should not rely on broad-suite success alone. A broad suite
can pass while missing retry-after-ack, duplicate-key, or Phase B seeding
regressions, so the focused tests above are part of the architecture contract.

## Completeness Review

This plan stays at architecture scope. It names implementation files and test
areas so the next phase can execute, but it does not prescribe a code patch or
replace detailed schema review.

The success criteria are covered as follows:

- Natural agent-led sends: `send_message_part` is a Phase A tool the model can
  call during reasoning for coherent conversational moves.
- Actual sent-so-far visibility: every tool result returns `sent_so_far`
  reconstructed from acknowledged delivered rows.
- Interruption stop behavior: pause and newer-inbound checks run before provider
  delivery, and interrupted results stop later user-visible sends for the turn.
- OOB preservation: every user-visible part routes through the final outbound
  guardrail, with block/rewrite/withheld represented as structured outcomes.
- Runtime idempotency: authoritative `part_key` values are generated and
  reserved by the runtime before provider delivery.
- Retry safety: duplicate keys and retry-after-ack recovery return the existing
  ledger state without another Discord API call.
- Onboarding correctness: onboarding advances only after provider-acknowledged
  text or a successful Discord reaction.
- Phase B correctness: Phase B is seeded from delivered ledger content only,
  including partial-send and interruption cases.
- Discord-first boundary: Discord is the first enabled transport while
  WhatsApp/Meta/Twilio remain on the existing single-send path.
- Repo touch points: `tool_schemas.py`, `app/services/tools/registry.py`,
  `app/services/agentic.py`, `app/services/messaging.py`,
  `app/services/discord.py`, `app/services/turn_context.py`, `migrations/`,
  `tests/conftest.py`, and the targeted test files are named explicitly.

The design is intentionally not a paragraph splitter. Paragraphs, chunks, and
streaming cadence are implementation smells for this feature. The invariant is
agent-led delivery intent plus runtime-owned actual-send state.

## Settled Decisions

- **SD-001** — Preserve the current two-phase lifecycle. _load_bearing: true_
  Rationale: Phase A already owns read/tool reasoning and user-facing response
  selection, while Phase B owns durable state writes after delivery. Incremental
  sending should fit into that shape rather than replace it.

- **SD-002** — Treat actual provider-acknowledged outbound rows as the source of
  truth for sent content. _load_bearing: true_
  Rationale: The runtime must own delivery state for safety, auditability,
  retry recovery, onboarding claims, and Phase B correctness.

- **SD-003** — Reject post-hoc paragraph splitting as the core design.
  _load_bearing: true_
  Rationale: Paragraph splitting cannot handle semantic send decisions,
  tool-result adaptation, interruption stops, accurate `sent_so_far`, or Phase B
  seeding from delivered content.

- **SD-004** — Make Discord the first incremental transport while keeping the
  contract provider-general. _load_bearing: false_
  Rationale: Discord has pacing context and fewer template-window constraints;
  WhatsApp/Meta/Twilio can stay on the existing single-send path until they are
  explicitly enabled.

- **SD-005** — Expose `send_message_part` as a Phase A/read-loop Anthropic tool.
  _load_bearing: true_
  Rationale: The model must receive authoritative send outcomes while it is
  still reasoning; an internal helper or post-loop chunker cannot provide that
  feedback.

- **SD-006** — Require `send_message_part` wiring in both `tool_schemas.py` and
  `app/services/tools/registry.py`. _load_bearing: true_
  Rationale: Anthropic tool visibility, schema validation, dispatch, phase
  gating, and structured tool results all depend on the existing two-surface
  registry pattern.

- **SD-007** — Make `part_key` runtime-generated and treat `client_part_key` as
  an optional hint only. _load_bearing: true_
  Rationale: Idempotency and retry safety cannot depend on model-generated
  strings; the runtime must reserve the authoritative key before provider
  delivery.

- **SD-008** — Use outbound `messages` rows as the delivered-content source of
  truth, with minimal message columns preferred over a separate primary ledger.
  _load_bearing: true_
  Rationale: Existing audit, feedback, provider id, and turn-linking behavior
  already centers on `messages`; incremental metadata should not create a second
  divergent transcript.

- **SD-009** — Reserve a unique runtime `part_key` before provider delivery.
  _load_bearing: true_
  Rationale: Duplicate tool calls and retry-after-ack recovery must never cause
  a second Discord API call for the same delivered part.

- **SD-010** — Reconstruct `sent_so_far` only from acknowledged delivered rows.
  _load_bearing: true_
  Rationale: Blocked, withheld, failed, duplicate-without-new-send, or
  interrupted drafts are not user-visible reality and must not drive later
  reasoning, onboarding, or Phase B writes.

- **SD-011** — Route every incremental text part through the final outbound
  guardrail. _load_bearing: true_
  Rationale: OOB checks, withheld reviews, provider retries, and acknowledgement
  semantics must be identical for incremental parts and ordinary outbound text.

- **SD-012** — Stop further sends when pause state or newer inbound input is
  detected before provider delivery. _load_bearing: true_
  Rationale: Incremental delivery must not drain stale queued parts after the
  conversation has moved on or operators have paused user-facing work.

- **SD-013** — Claim onboarding only after actual provider-visible action.
  _load_bearing: true_
  Rationale: A user is not welcomed by a blocked, withheld, interrupted, or
  failed draft; only provider-acknowledged text or a successful Discord reaction
  should advance onboarding.

- **SD-014** — Seed Phase B from the delivered ledger only. _load_bearing: true_
  Rationale: Durable memories and audit state must reflect what the user
  actually saw, including partial-send and interruption cases.

- **SD-015** — Integrate incremental sending through the existing `run_phase`
  tool loop. _load_bearing: true_
  Rationale: The current Anthropic loop already appends validated tool results
  back into the transcript; incremental delivery should use that feedback path
  instead of a separate streaming mechanism.

- **SD-016** — Route final-text fallback through the same primitive with
  delivered-ledger de-duping. _load_bearing: true_
  Rationale: Compatibility fallback must not create an untracked second delivery
  path or repeat content the user already received.

- **SD-017** — Keep transport-specific behavior at the outbound edge.
  _load_bearing: true_
  Rationale: The model-facing primitive should own delivery intent and runtime
  state, while Discord/WhatsApp mechanics remain behind shared provider
  delivery functions.

- **SD-018** — Validate with focused lifecycle, safety, idempotency, onboarding,
  migration, and fake-pool tests before the broad suite. _load_bearing: true_
  Rationale: Incremental sending can fail in narrow ordering and recovery cases
  that broad regression tests may not exercise directly.
