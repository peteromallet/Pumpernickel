# Durable inbound queue hardening — brief

**Profile**: `thoughtful//medium` (standard robustness, medium planner depth)

**Mode**: code

**Purpose**: make inbound message handling robust across app downtime, process crashes, model/tool failures, send failures, and partial processing.

---

## Context

The immediate reconnect catch-up fix handles Discord gateway gaps by replaying recent REST history after reconnect. That is necessary but not sufficient.

The more general reliability problem is that inbound messages should be treated as a durable queue:

1. Store the inbound message durably and idempotently.
2. Track whether it has actually been handled.
3. Retry or recover if handling fails or stalls.
4. Avoid duplicate turns/replies when replaying or retrying.
5. Preserve auditability: why did the bot reply, stay silent, withhold, or fail?

Today `messages.processing_state` already approximates this, but the semantics are not crisp enough to confidently recover every partial failure. The user asked whether an "addressed column" is needed. The preferred design is not a vague `addressed` boolean, but explicit queue-state and handling metadata.

---

## Goal

Define and implement durable inbound-message processing semantics so any inbound Discord message can be safely:

- replayed from provider history,
- retried after failure,
- recovered after a worker crash,
- marked as intentionally silent/withheld,
- audited later,
- and never answered twice accidentally.

---

## Relationship To The Reconnect Catch-up Sprint

This sprint builds on `discord-reconnect-catchup`.

Assume the reconnect sprint has already made catch-up bot-scoped and idempotent. This sprint hardens what happens after a message exists in `messages`.

If both sprints are executed together, the reconnect catch-up changes should land first.

---

## Settled Decisions

- **SD-001** — Keep `messages` as the durable inbound queue. _load_bearing: true_
  Rationale: Inbound messages already live there, with bot/topic scope, external ids, direction, sender, content, and processing state. A separate queue table would duplicate ownership unless a later design proves it is necessary.

- **SD-002** — Do not add a bare `addressed` boolean. _load_bearing: true_
  Rationale: "Addressed" hides important distinctions: replied, silent by design, withheld due to newer inbound, failed, expired, or still processing. Use explicit state/result fields instead.

- **SD-003** — `processing_state` should have crisp queue semantics. _load_bearing: true_
  Target states:
  - `raw`: stored, not yet claimed.
  - `deferred`: intentionally waiting for coalescing or pacing.
  - `processing`: claimed by a worker/turn.
  - `processed`: successfully handled by a completed turn.
  - `expired`: intentionally no longer needs direct handling.
  - `failed`: attempted and failed; retryable or inspectable depending on error.

- **SD-004** — Add handling metadata instead of overloading state. _load_bearing: true_
  Recommended fields, subject to planner verification:
  - `handled_at timestamptz`
  - `handled_by_turn_id uuid references bot_turns(id)`
  - `handling_result text` such as `replied`, `silent`, `withheld_newer_inbound`, `no_action`, `expired`, `failed`
  - `processing_started_at timestamptz`
  - `processing_error text`

- **SD-005** — Message completion is tied to turn completion, not just turn start. _load_bearing: true_
  Rationale: A message is not handled merely because a turn was opened. It is handled when the turn completes successfully or makes an explicit no-reply/withheld decision.

- **SD-006** — Recovery is periodic and provider-agnostic. _load_bearing: true_
  Rationale: The sweeper should recover local queue state (`raw`, stale `processing`, retryable `failed`) independent of whether the inbound came from Discord or another transport.

- **SD-007** — Duplicate prevention remains external-id based. _load_bearing: true_
  Rationale: Provider replay should remain safe. Existing or new unique constraints must prevent duplicate inbound rows for the same `(bot_id, transport/external_message_id)` pair.

---

## Implementation Shape

1. **Clarify the state model**
   - Audit every read/write of `messages.processing_state`.
   - Document state transitions in code comments or a small module-level docstring near the transition helpers.
   - Prefer central helper functions for state changes rather than ad hoc SQL scattered across services.

2. **Add handling metadata**
   - Add a migration for the chosen metadata columns.
   - Backfill conservatively:
     - Existing `processed` inbound messages can get `handled_at = sent_at` or remain null if provenance is unclear; planner should decide.
     - Existing rows should not be reprocessed solely because metadata is null.
   - Add indexes for sweeper queries if needed.

3. **Mark messages as `processing` when claimed**
   - When a coalescer/agentic runner starts handling a set of inbound messages, move them from `raw`/eligible states to `processing`.
   - Stamp `processing_started_at`.
   - Avoid racing two workers into the same message.

4. **Mark completion after the turn completes**
   - On successful reply: `processing_state='processed'`, `handled_by_turn_id=<turn>`, `handling_result='replied'`, `handled_at=now()`.
   - On deliberate silence: `handling_result='silent'`.
   - On stale outbound withheld because a newer inbound arrived: `handling_result='withheld_newer_inbound'`.
   - On no-action turns that are valid: `handling_result='no_action'`.

5. **Mark failure explicitly**
   - If a turn fails before completing, mark associated inbound rows `failed` with `processing_error`.
   - Decide which failures are retryable. Do not blindly retry permanent validation failures forever.

6. **Add a sweeper**
   - Periodically find:
     - `raw` messages older than a small grace interval.
     - `failed` messages that are retryable and below a retry cap.
     - `processing` messages with stale `processing_started_at`.
   - Re-enqueue or move back to `raw` safely.
   - Skip old messages outside a retention window unless explicitly configured.

7. **Make catch-up and sweeper cooperate**
   - Catch-up inserts or surfaces rows.
   - Sweeper handles local unprocessed rows.
   - Neither path should duplicate turns for already handled rows.

---

## Files Expected To Change

- `migrations/`
  - new migration for handling metadata and any indexes/constraints
- `app/services/inbound.py`
  - idempotent inbound insert/upsert semantics
- `app/services/agentic.py`
  - mark triggering messages processing/processed/failed around turn lifecycle
- `app/services/scheduled_jobs.py` or a new queue/sweeper module
  - periodic recovery of stuck inbound messages
- `app/services/discord.py`
  - ensure catch-up uses the new idempotent insert and does not bypass state semantics
- `app/services/hot_context.py`
- `app/services/hot_context_solo.py`
  - ensure silent/withheld turns and message states remain auditable in context where relevant
- `tests/conftest.py`
  - FakePool support for new state transitions
- New or updated tests around inbound queue state transitions

Planner must inspect existing coalescer behavior before finalizing exact transition points.

---

## Invariants

1. **At-most-once user-visible handling.** A single inbound message should not cause two user-visible replies unless a human explicitly retries with a separate command.
2. **At-least-once durable consideration.** A stored inbound message should not remain forever in `raw` or stale `processing` without being retried or marked terminal.
3. **Deliberate silence is handled, not ignored.** If the bot intentionally stays silent, the message should still become terminal/auditable.
4. **Withheld because newer inbound arrived is handled.** This should be visible as a result, not confused with failure.
5. **Provider replay is safe.** Discord catch-up or future provider catch-up can replay recent messages without duplicates.
6. **Bot scope is preserved.** Every queue/recovery query must respect `bot_id` and topic/channel scope where applicable.
7. **No infinite retry loops.** Permanent errors need terminal marking or bounded retry with audit.

---

## Edge Cases To Test

- App stores inbound row, then crashes before opening a turn. Sweeper later processes it.
- App opens a turn, then crashes before completion. Stale `processing` row is recovered.
- Model/tool call fails. Message becomes `failed` with error metadata and is retried or left inspectable according to policy.
- Outbound send fails after model completes. Message is not incorrectly marked as fully replied unless the send actually succeeded or the turn made an explicit terminal decision.
- User sends a newer inbound while an older response is pacing. Older message gets `handling_result='withheld_newer_inbound'`; newer message is processed.
- Catch-up replays a processed Discord message; no new turn.
- Catch-up replays a raw/stuck Discord message; it gets processed.
- Scheduled task/silent turn audit remains accurate.
- Multiple bot DMs for the same Discord user do not interfere with each other's queue states.

---

## Open Questions For The Planner

1. Does `messages` already have a uniqueness constraint on `whatsapp_message_id`? If yes, is it bot-scoped and compatible with Discord?
2. Is `processing_state='processed'` currently set before or after turn completion? Identify every transition.
3. Should failed messages retry automatically, or should only raw/stale-processing messages auto-recover in v1?
4. What is the correct retention window for old unprocessed inbound messages: 24h, 7d, or configurable?
5. Should coalesced groups mark every triggering message with the same `handled_by_turn_id`, or only the primary trigger? Preferred answer: every triggering message.

---

## Success Criteria

**MUST**

- Processing states have documented, enforced semantics.
- Inbound messages claimed by a turn are marked `processing` with a timestamp.
- Completed turns mark all triggering inbound messages terminal with `handled_by_turn_id`, `handled_at`, and `handling_result`.
- Failed turns mark triggering messages `failed` or recoverable according to documented policy.
- Stale `processing` messages are recoverable by a sweeper.
- Raw messages left behind by downtime are recoverable by a sweeper.
- Replaying already handled provider messages does not create duplicate turns or duplicate replies.
- Tests cover crash-before-turn, crash-during-turn, duplicate replay, deliberate silence/withheld, and stale processing recovery.

**SHOULD**

- Queue-state changes are centralized in helper functions.
- Sweeper logs recovery counts by bot and state.
- `get_bot_actions` or hot context can surface relevant silent/withheld/failed handling results when the user asks why something happened.

**INFO**

- This sprint does not need to build a user-facing retry UI.
- This sprint does not need to rename `whatsapp_message_id`, although future cleanup may want a transport-neutral `external_message_id`.

