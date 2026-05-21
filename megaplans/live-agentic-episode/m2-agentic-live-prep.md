# Sprint 2: Agentic Live Prep

## Outcome

Replace shallow live agenda generation with a private selected-bot agentic prep turn. The selected bot should use its normal persona, hot context, scoped read tools, provider chain, spend tracking, and audit trail, then submit a structured live brief instead of sending a reply.

## Scope

In:
- Add `run_live_prep_agentic_job(conversation_id, user, scope, steering_text)`.
- Build normal selected-bot context: `BotSpec`, primary topic, participant shape, partner sharing state, hot context, rendered system prompt.
- Add terminal tool `submit_live_brief` that accepts the existing `Agenda` schema.
- Run a bounded non-chat agentic loop that allows read tools, `update_turn_plan`, and optionally consult tools.
- Disallow outbound, write, and schedule tools during prep.
- Persist submitted brief to `conversation_artifacts(type=live_prep_brief)`.
- Persist `conversation_items` and artifact links with relation `planned_item`.
- Update `conversations.status` from `preparing` to `ready`.
- Add minimal status API behavior for `preparing` and `prep_failed`.
- Add a prep retry endpoint or internal retry helper so failed prep is recoverable before later sprints.

Out:
- Do not implement post-session debrief.
- Do not allow prep to mutate durable user state.
- Do not make live in-call turns use the full agentic loop.

## Locked Decisions

- Prep is a private non-chat `bot_turn` with `trigger_metadata.kind = "live_prep"` and `conversation_id`.
- Prep must use selected bot identity and topic; no mediator fallback unless the selected bot is mediator.
- Required finalization gate: `submit_live_brief`.
- Plain final text without `submit_live_brief` is a failure or retryable correction path, not a valid agenda.
- Prep tool cap default is `100`.
- Prep runs asynchronously: `POST /api/live/sessions` creates a session in `preparing` status and returns promptly. The client polls session/card status or receives status events. Synchronous prep is out of scope for the agentic path.
- `submit_live_brief` returns data to the runner; persistence stays centralized.

## Implementation Shape

Add a reusable lower-level helper rather than calling `_run_agentic()` directly:

```python
run_agentic_nonchat_job(
    kind="live_prep",
    user=user,
    scope=scope,
    conversation_id=conversation_id,
    system_task=...,
    allowed_tools_policy=...,
    required_submit_tool="submit_live_brief",
    max_tool_iterations=settings.live_prep_tool_cap,
)
```

It should reuse:
- hot context builders
- `TurnContext`
- `run_step()`
- provider chain resolution
- tool registry/audit
- spend tracking

It should not reuse chat-specific behavior:
- claiming inbound messages
- sending outbound messages
- finalizing inbound queue rows
- normal respond/reaction handling
- `_run_agentic()` wholesale
- `_allowed_tools_for_step()` as the source of truth for prep permissions

Non-chat runner contract:
- Open turns through `_open_nonchat_turn(kind, conversation_id, ...)`, not `_open_turn()` if `_open_turn()` remains message-trigger oriented.
- Use a flat policy-filtered tool set; do not infer permissions from chat step names.
- Ensure `required_submit_tool` is present in the allowed tool set.
- If the model exhausts the tool cap without `submit_live_brief`, synthesize orphan tool-result stubs if needed, mark the turn with a submit-missing failure reason, and leave the conversation in `prep_failed`.
- Complete the `bot_turn` without touching inbound queue message lifecycle.

## Open Questions

- Should consult tools be enabled in prep from the first version? Prefer enabled only if the selected bot already allows consult in its normal flow and the tool cannot send outbound.

## Constraints

- Existing `Agenda` validation must remain the gate.
- Existing tests using `StubAgendaProducer` should either be adapted to the new path or kept only for explicit fallback tests.
- No outbound tools in prep, even if the bot prompt tries to reply.
- No durable writes in prep.

## Done Criteria

- Creating a live session uses agentic prep by default.
- Fallback/stub mode remains available for local no-key/dev use if needed.
- Tests prove a non-mediator bot gets non-mediator prep context and tools.
- Tests prove outbound/write/schedule tools are not exposed during prep.
- Tests prove missing `submit_live_brief` fails visibly.
- Tests prove failed prep enters `prep_failed` and can be retried.
- Prep artifacts and planned item links are persisted.

## Touchpoints

- `app/services/live/prep.py`
- `app/services/live/bot_profile.py`
- `app/services/agentic.py` or new `app/services/nonchat_agentic.py`
- `app/services/tools/registry.py`
- `app/services/live/schemas.py`
- `app/routers/live_voice.py`
- `tests/test_live_prep.py`
- new tests for non-chat runner/tool gating

## Anti-Scope

- Do not rewrite live STT/TTS.
- Do not alter during-call turn latency path.
- Do not add user-facing review UX.
