# Discord reconnect catch-up + idempotent replay — brief

**Profile**: `thoughtful//medium` (standard robustness, medium planner depth)

**Mode**: code

**Purpose**: immediate production reliability fix for Discord messages missed during gateway disconnect/reconnect windows.

---

## Incident Context

On 2026-05-15, Hector missed this Discord DM from Peter:

> I think that makes sense... three days of weightlifting... two days of running... monday chest and back... wednesday bicep and tricep... friday legs and shoulders... tuesday and thursday running. What do you think about that?

Direct Discord REST showed the message exists:

- Discord message id: `1504833824239124530`
- Channel id: `1504809282825359400`
- Author: Peter (`301463647895683072`)
- Timestamp: `2026-05-15T13:12:22.159Z`

Production DB showed:

- No `messages` row for that Discord id.
- No `bot_turns` row after Hector's prior turn at `13:06:45Z`.
- No `turn_audit_events` for that message.

Railway logs showed the root cause:

- `13:12:21.640Z` — `[gateway:hector] WS closed code=1006`
- `13:12:22.159Z` — user sent the message during the disconnect window.
- `13:12:25.338Z` — Hector reconnected and received `READY`.
- No `MESSAGE_CREATE` event was logged for `1504833824239124530`.

Current code runs `catch_up_recent_messages(...)` at gateway startup only, before `gateway.run_forever()`. It does not run catch-up after reconnect/`READY`, so messages sent during a websocket gap can be permanently missed until manual intervention.

---

## Goal

Make Discord message ingestion resilient to short gateway disconnects for **every bot** by running a bot-scoped, idempotent catch-up pass after every reconnect/`READY`, not only at process startup.

The immediate fix should recover messages that were sent while the websocket was disconnected, without creating duplicate message rows or duplicate bot replies.

---

## Settled Decisions

- **SD-001** — Apply to every Discord bot gateway. _load_bearing: true_
  Rationale: The same gateway loop is used for mediator, Tante Rosi, Hector, and future per-bot Discord clients. The bug is not Hector-specific.

- **SD-002** — Run catch-up after reconnect/`READY`, not only process startup. _load_bearing: true_
  Rationale: The missed Hector message landed while the process was still alive but the websocket was down. Startup catch-up cannot recover that class of loss.

- **SD-003** — Catch-up must be bot-scoped. _load_bearing: true_
  Rationale: A user's latest inbound message for Tante Rosi or mediator must not advance Hector's catch-up cursor. Every query for "already seen" Discord messages must include the current `bot_id`.

- **SD-004** — Prefer bounded replay over strict cursor-only catch-up. _load_bearing: true_
  Rationale: A strict `after=last_seen_id` cursor can skip messages if `last_seen_id` is wrong, cross-bot, or points past an unprocessed message. Replaying the last N messages and idempotently inserting missing rows is simpler and safer.

- **SD-005** — Idempotency key is `(bot_id, Discord message id)`. _load_bearing: true_
  Rationale: The same Discord user can talk to multiple bot DMs. Discord message ids are globally unique in practice, but bot-scoping keeps the app invariant explicit and matches the rest of the per-bot architecture.

- **SD-006** — This sprint should not redesign the whole inbound queue. _load_bearing: true_
  Rationale: Durable state-machine hardening is captured in the separate `inbound-queue-hardening` brief. This sprint is the urgent reconnect/catch-up fix.

---

## Implementation Shape

1. **Move catch-up into the gateway reconnect lifecycle**
   - When `DiscordGatewayBot._run_once()` receives `READY`, trigger catch-up for that gateway's `bot_id` and `DiscordClient`.
   - Ensure this also covers the initial connect.
   - Avoid double-running startup catch-up and READY catch-up in a way that causes duplicate processing. Duplicate replay must be harmless, but the code path should be understandable.

2. **Make catch-up replay recent messages**
   - Fetch the last N messages from the relevant DM channel, probably 50 or 100.
   - Do not rely solely on `after=last_seen_id`.
   - Process from oldest to newest.
   - Ignore bot-authored messages and unsupported attachment-only messages as today.

3. **Make ingestion idempotent**
   - `process_inbound` or the lower insert path must safely handle a Discord message that already exists.
   - If a matching inbound row exists and is already `processed`, do not trigger another turn.
   - If a matching inbound row exists but is `raw`, `deferred`, or retryable, do not create a duplicate row; let normal processing semantics decide whether to run.

4. **Bot-scoped duplicate checks**
   - Any "last seen" or "already stored" query must include `bot_id`.
   - Existing startup catch-up currently queries latest inbound by Discord user identity; it must not use a cross-bot cursor.

5. **Observability**
   - Log catch-up start/end per bot with count fetched, count inserted, count skipped-existing, and count handed to coalescer.
   - Log the Discord channel id and bot id, not secrets.

---

## Files Expected To Change

- `app/services/discord.py`
  - `DiscordGatewayBot._run_once`
  - `catch_up_recent_messages`
  - possibly helper functions for replay/idempotency
- `app/main.py`
  - remove or adjust startup-only catch-up if READY catch-up makes it redundant
- `app/services/inbound.py`
  - verify duplicate Discord ids are handled cleanly
- `migrations/`
  - only if the repo lacks a suitable uniqueness constraint for inbound external ids
- `tests/`
  - add or update Discord catch-up tests around reconnect replay and duplicate suppression

The planner must inspect the current `process_inbound` insert behavior before deciding whether a migration is needed.

---

## Invariants

1. **No duplicate replies.** Replaying a Discord message that is already fully handled must not create a second bot turn or second outbound.
2. **No cross-bot cursor leakage.** A recent Tante Rosi/mediator inbound must not cause Hector catch-up to skip a Hector-channel message.
3. **Catch-up is safe to run repeatedly.** Startup, reconnect, manual retry, and future scheduled sweeps can all call the same replay path.
4. **Per-bot token isolation remains intact.** Hector catch-up uses Hector's token/client; Tante Rosi catch-up uses Tante Rosi's token/client; mediator catch-up uses mediator's token/client.
5. **No broad queue semantics in this sprint.** Do not add `handled_at`, stale processing recovery, or a general sweeper here unless required to make reconnect catch-up correct.

---

## Edge Cases To Test

- Gateway reconnects after websocket close; a user message sent during the gap is present in REST history and gets ingested after `READY`.
- Catch-up runs twice over the same REST window; existing processed messages are skipped and do not trigger new turns.
- A user's latest message to mediator is newer than their latest message to Hector; Hector catch-up still sees and processes missing Hector messages.
- Bot-authored messages in the REST window are ignored.
- Unsupported empty messages/attachments keep current behavior.
- Catch-up processes messages oldest-to-newest so coalescing preserves natural order.
- If the Discord REST fetch fails, the gateway stays alive and logs the catch-up failure without killing the reconnect loop.

---

## Success Criteria

**MUST**

- Reconnect/`READY` triggers bot-scoped catch-up for mediator, Tante Rosi, Hector, and future configured Discord bots.
- Catch-up replays a bounded recent window instead of relying only on `after=last_seen_id`.
- Replay is idempotent: same Discord message id does not create duplicate inbound rows or duplicate bot replies.
- Existing processed inbound messages remain untouched.
- Missing inbound messages from the replay window are inserted and handed to the normal coalescer/agentic path.
- Tests cover reconnect replay and duplicate replay.

**SHOULD**

- Logs include per-bot catch-up metrics.
- Catch-up helper is reusable by a future sweeper from the durable queue hardening sprint.

**INFO**

- The durable "raw/processing/failed/handled" state-machine cleanup is intentionally deferred to `inbound-queue-hardening`.

