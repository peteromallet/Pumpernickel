# Mind/Mouth Compose Boundary Brief

## Outcome

Implement a minimal, production-safe split between internal agent cognition and user-facing speech in the existing agentic turn flow. The agent may think, plan, inspect context, and decide state updates internally, but the text delivered to Discord/WhatsApp must come from an explicit compose boundary whose only job is to produce user-facing message content.

## Problem

The current runner treats freeform assistant text from the `respond` phase as the candidate outbound message. In practice the model sometimes mixes phase/process narration with the actual user-facing reply, for example:

```text
Here's the read step done.

Today's a good day for something that doesn't load that foot...
```

The sanitizer catches phrases such as `read step`, but today that means user speech can be withheld or over-repaired downstream. The deeper issue is that internal cognition and user speech share the same text channel.

## Current Shape

The agentic runner uses turn skeletons such as:

```text
quick_reply: respond -> done
standard: read -> respond -> record -> schedule -> done
```

Tool allowlists are phase-specific:

- `read`: context reads only, no user-facing delivery.
- `respond`: may send text/reactions and currently may call a small set of tools such as `log_event`.
- `record`: durable writes, no user-facing delivery.
- `schedule`: scheduling tools, no user-facing delivery.

The final outbound send path currently receives `assistant_text` from `respond`, cleans it, checks newer inbound/safety, then sends it.

## Desired Shape

Preserve the existing skeleton/phase architecture where possible, but introduce a clear "mouth" boundary:

- Internal phases can produce notes, decisions, and tool calls.
- A compose operation receives the relevant context and an internal reply brief.
- The compose operation returns only user-facing message text or message parts.
- Runtime sends only that compose output.
- Sanitization remains a final leak guard, not the normal mechanism for shaping replies.

The exact implementation is open. Acceptable approaches include:

- Add a constrained compose sub-step inside `respond`.
- Add a structured response contract for `respond` that separates `reply_brief`/internal notes from `user_message`.
- Add a lightweight `compose_user_message` helper/tool/function that is available only at the right point and has no durable-write tools.

Prefer the smallest design that makes internal process text structurally unsendable.

## Requirements

1. User-facing delivery must not depend on parsing a mixed internal/user freeform blob.
2. The compose prompt/contract must have one job: write exactly what the user should receive.
3. The composer must receive enough context to write well:
   - triggering user message(s),
   - relevant recent/hot context,
   - the internal reply goal/brief,
   - bot voice constraints,
   - any already-sent message parts if applicable.
4. Discord multi-message behavior must remain possible, but message parts should be composed as user-facing speech, not emitted while the model is still doing internal tool work.
5. Newer-inbound interruption behavior must remain intact. If the user sends a newer message before/during delivery, stale outbound should still be withheld.
6. Durable writes should remain in record/schedule phases unless there is already an explicit, justified existing exception.
7. If sanitizer removes or withholds compose output, observability must distinguish that from intentional silence.
8. Existing safety checks and OOB checks must remain in the final delivery path.

## Non-Goals

- Do not redesign the entire agent runtime.
- Do not remove the sanitizer.
- Do not remove turn skeletons.
- Do not change unrelated bot personas or broad prompt registry behavior.
- Do not build a heavy subagent system unless the codebase already has a very small local abstraction that makes that cheaper than a structured compose pass.
- Do not weaken internal-process leak prevention.

## Touchpoints

Likely files/modules:

- `app/services/agentic.py`
- `app/services/turn_plan.py`
- `app/services/tools/registry.py`
- `app/services/text_safety.py`
- `app/services/tools/read_tools.py`
- `app/bots/hector.py`
- `app/bots/mediator.py`
- `app/bots/habits.py`
- `app/bots/tante_rosi.py`
- `tests/test_agentic_lifecycle.py`
- `tests/test_text_safety.py`
- `tests/test_send_outbound.py`
- Hector/turn-plan tests if routing or phase contracts change.

## Regression Scenario

The motivating incident:

- User confirmed: `I worked out properly yessterday!`
- Then asked: `And what could i do today instead of running?`
- Hector needed to answer with toe-safe alternatives.
- The model generated a useful reply but mixed in `Here's the read step done.`
- Delivery produced no outbound message.

Done behavior: Hector should answer the latest message with user-facing advice, preserve newer-inbound withholding for stale drafts, and never expose phase/process language.

## Done Criteria

- Tests prove mixed internal/user text no longer becomes the sendable channel.
- Tests prove internal-only process text is still withheld.
- Tests prove the motivating toe/running reply shape is delivered or composed cleanly.
- Tests prove newer-inbound withholding still works.
- Tests prove multi-part Discord delivery, if changed, still checks interruption between parts.
- Tests or audit assertions distinguish sanitizer/composer withholding from intentional silence.
- Focused test suite passes.

