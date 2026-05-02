# Discord Presence Pacing Plan

## Scope

This plan covers Discord bot presence behavior for the Véas Discord transport. The goal is to stop the bot from looking green and active all day while preserving reliable DM ingestion and guaranteeing that the bot is online before any visible typing indicator is sent.

The Discord Gateway websocket in `app/services/discord.py` must remain connected. It is the live DM ingestion path, so disconnecting or cycling the Gateway to look offline would trade a presence polish problem for message reliability risk. The implementation should keep the current persistent Gateway connection and control user-visible availability through explicit Gateway presence instead.

## Implementation Thesis

The fix is to make presence an explicit part of the Gateway lifecycle:

- Identify with an invisible/offline-looking initial presence when the Gateway session starts.
- Use Gateway Presence Update events for later transitions while the websocket remains connected.
- Centralize transition logic in a presence manager tied to the active Gateway sender, rather than hand-rolling status changes inside individual send paths.

Discord's Gateway docs describe `invisible` as showing the client offline and `online` as the active state in the Presence Update payload: https://docs.discord.com/developers/events/gateway-events#update-presence. Discord's opcode table defines opcode `3` as Gateway Presence Update: https://docs.discord.com/developers/topics/opcodes-and-status-codes#gateway-gateway-opcodes.

The hard correctness invariant is stronger than the presence polish: no Discord typing call may be made unless online presence has just been confirmed for the active Gateway connection, or presence control is explicitly disabled by config. If the manager cannot confirm an active sender and successful online transition first, the implementation must skip the REST typing indicator for that attempt. It should still allow the message send path itself to proceed when appropriate; the fail-closed behavior is specifically about visible typing while presence is unknown, detached, or invisible.

## Critical Invariants

- Keep the Gateway websocket connected for DM ingestion. Do not disconnect, cycle, or delay Gateway connectivity to simulate offline presence.
- Identify starts offline-looking, normally `invisible`; later status changes use centralized opcode `3` presence updates.
- `ensure_online_for_typing(...)` bypasses `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S`; anti-flap never blocks a typing-critical upgrade to `online`.
- If no active Gateway sender can confirm `online`, skip the REST typing call. Message sending may still proceed; typing fails closed.
- Soft inbound, reaction, send, and after-send transitions are bounded polish only. They may be skipped during reconnect and must not change pacing decisions.

## Configuration

Add presence settings alongside the existing Discord pacing settings. Defaults should be conservative: idle starts invisible, online windows are short, and all randomized tails have tight bounds so a single interaction cannot leave the bot green for long.

| Setting | Default | Bounds / intent |
| --- | --- | --- |
| `DISCORD_PRESENCE_ENABLED` | `true` | Feature flag. Set `false` to stop sending explicit presence payloads if Discord rejects the behavior or rollout needs to be reversed. |
| `DISCORD_PRESENCE_DEFAULT_STATUS` | `invisible` | Idle/no-conversation baseline. Prefer `invisible`; any fallback must still avoid all-day green status. |
| `DISCORD_PRESENCE_TYPING_TAIL_S` | `8.0` | Hard online hold after a typing pulse, long enough for the client-visible typing window plus a small tail. Keep bounded and short. |
| `DISCORD_PRESENCE_AFTER_SEND_TAIL_MIN_S` | `18.0` | Lower bound for the after-send online tail. |
| `DISCORD_PRESENCE_AFTER_SEND_TAIL_MAX_S` | `45.0` | Upper bound for the after-send online tail; this cap prevents a normal answer from becoming a long green session. |
| `DISCORD_PRESENCE_INBOUND_ONLINE_MIN_S` | `2.0` | Lower bound for a soft inbound-message presence window. |
| `DISCORD_PRESENCE_INBOUND_ONLINE_MAX_S` | `6.0` | Upper bound for a soft inbound-message presence window; short enough that received DMs do not make the bot appear continuously active. |
| `DISCORD_PRESENCE_REACTION_TAIL_MIN_S` | `8.0` | Lower bound for a short online tail after a successful reaction-only response. |
| `DISCORD_PRESENCE_REACTION_TAIL_MAX_S` | `20.0` | Upper bound for reaction-only online visibility; reactions should feel noticed without implying an extended session. |
| `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S` | `4.0` | Anti-flap guard for soft transitions and downgrades only. It must never delay or suppress a typing-critical upgrade to `online`. |

Validate max values are greater than or equal to their paired min values, durations are finite and non-negative, and the default status is one of the statuses the implementation intentionally supports. Randomness must be bounded by these min/max settings and injected in tests so tail selection is deterministic.

The important exception is `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S`: it is a polish guard, not a safety gate. It may suppress redundant inbound/reaction/send soft transitions and may delay downgrades enough to avoid mechanical flapping, but `ensure_online_for_typing(...)` must bypass it and force `online` immediately when an active Gateway sender can confirm the presence update.

## Centralized State Machine

Implement a small `DiscordPresenceManager` as the single owner of Discord presence transitions. Place it near `DiscordGatewayBot` in `app/services/discord.py`, or move it to `app/services/discord_presence.py` if keeping it separate makes tests and ownership clearer. Do not spread direct presence updates through the pacer, send helpers, reaction helpers, or inbound handlers; those paths should call manager methods.

Inject dependencies so tests can run without real Discord timing or network behavior:

- `settings`: the `DISCORD_PRESENCE_*` values.
- `send_json`: the active Gateway sender, attached and detached with the websocket lifecycle.
- `sleep`: async sleep used by delayed downgrade scheduling.
- `now`: monotonic clock returning seconds.
- `uniform`: bounded random source for min/max tail selection.

Core state:

- `default_status`: configured offline-looking baseline, normally `invisible`.
- `current_status`: last status successfully sent, or the initial status included in Identify.
- `hard_online_until`: deadline for typing-critical online holds.
- `soft_online_until`: deadline for believable activity tails from inbound messages, sends, and reactions.
- `last_transition_at`: anti-flap timestamp for soft upgrades and downgrades only.
- `sender`: currently attached Gateway send callable, or `None` while disconnected/reconnecting.
- `sender_generation`: integer incremented on every attach and detach so delayed tasks can detect stale websocket ownership.
- `downgrade_task`: the single reschedulable task that returns presence to `default_status` when all holds expire.

Public methods:

- `initial_presence_payload() -> dict | None`: returns the Identify `presence` object when enabled, usually `{"since": None, "activities": [], "status": "invisible", "afk": False}`.
- `attach_sender(send_json)`: stores the active Gateway sender, increments `sender_generation`, and enables later opcode `3` updates.
- `detach_sender()`: clears the sender, increments `sender_generation`, and prevents stale delayed tasks from writing to the old websocket.
- `ensure_online_for_typing(visible_s: float, reason: str) -> bool`: extends `hard_online_until`, bypasses `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S`, sends `online` immediately when needed, and returns `false` if no active sender can confirm the update.
- `message_received()`: requests a short soft inbound online window using the inbound min/max settings.
- `message_sent()`: requests the randomized after-send tail after a successful message send.
- `reaction_sent()`: requests the randomized reaction tail after a successful reaction-only response.
- `close()`: cancels the downgrade task and detaches the sender.

Desired status is derived from holds: if either hard or soft deadline is in the future, desired status is `online`; otherwise it is `default_status`. The manager should suppress duplicate Gateway payloads when `desired_status == current_status`; refreshing a hold deadline is still valid, but it should not emit another identical presence update. Soft upgrades and downgrades respect `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S` to avoid mechanical flapping. Typing-critical upgrades never respect that gap.

Downgrades should use one reschedulable task, not one task per event. Each schedule captures the current `sender_generation`; when the task wakes, it recomputes desired status and exits without sending if generation changed, the manager closed, or another hold still requires `online`. This prevents delayed writes to stale websockets after reconnect.

| Event | Desired behavior |
| --- | --- |
| Gateway connect | Identify as `invisible`; stay connected and attach the active sender for later opcode `3` updates. |
| Inbound DM | Request a bounded soft `online` window, unless already online. Subject to anti-flap and safe to skip during reconnect. |
| Bot typing pulse | Force `online` before `send_typing`; bypass anti-flap; hold through visible typing plus `DISCORD_PRESENCE_TYPING_TAIL_S`. |
| Gateway sender detached | Typing-critical calls return `false`; REST typing is skipped until a sender reattaches. |
| Thinking/composing typing loop | Repeated pulses refresh `hard_online_until` without duplicate presence updates when already online. |
| Send message | After successful send, request the randomized after-send soft tail, then downgrade to default when holds expire. |
| Reaction-only response | After successful reaction, request a brief soft online tail without a long green session. |
| Idle/no conversation | Return to `DISCORD_PRESENCE_DEFAULT_STATUS`, normally `invisible`; no green status. |

## Gateway Lifecycle Wiring

`DiscordGatewayBot` owns the live Discord Gateway websocket in `app/services/discord.py`, so it should also own attachment of the presence sender for that websocket lifetime. Presence updates require the active Gateway connection; the manager can be app-created and shared, but sender ownership follows the current Gateway session.

Update `DiscordGatewayBot.__init__` to accept `presence_manager: DiscordPresenceManager | None`. In `_run_once`, build the Identify payload with `presence_manager.initial_presence_payload()` when presence is enabled. The Identify `d` object should include the same token, intents, and properties it uses today, plus a `presence` object such as `{"since": None, "activities": [], "status": "invisible", "afk": False}`.

After the websocket connects and before later transitions can occur, attach a sender to the manager that serializes opcode `3` presence updates through the active websocket. The sender should be narrowly scoped to presence payloads, for example a callable that sends `{"op": 3, "d": presence}` via `ws.send(json.dumps(payload))`.

Detach the sender whenever `_run_once` exits, including normal close, reconnect, or exception paths. The detach must increment `sender_generation`; delayed downgrade tasks compare their captured generation to the current one before sending. This prevents an old downgrade from writing to a stale websocket after a reconnect.

`DiscordGatewayBot.close()` should continue closing the Gateway loop and heartbeat, then close or detach the presence manager so background downgrade tasks are cancelled. Close behavior should be idempotent: repeated shutdown calls should not emit more presence updates or resurrect a detached sender.

In `app/main.py`, create one shared `DiscordPresenceManager` when the Discord provider is enabled and presence is enabled. Pass that same instance to both `DiscordGatewayBot` and `DiscordPacer`. The Gateway bot provides the live sender; the pacer and later send/reaction hooks use the same manager state so typing-critical and soft transitions cannot disagree.

Do not create a second manager inside `DiscordPacer`, direct send helpers, or reaction code. Multiple managers would split `current_status`, duplicate suppression, and sender generation state, which is exactly the failure mode this plan avoids.

## Typing-Critical Integration

Typing is the hard safety path. Every Discord typing indicator must be preceded immediately by a successful `DiscordPresenceManager.ensure_online_for_typing(...)` call, unless presence control is disabled by config. If that call returns `false`, skip the REST typing request for that attempt.

Add an optional `presence_manager` dependency to `DiscordPacer`. In `_send_bot_typing_pulse(...)`, keep the existing Discord typing pulse-gap check first; when a pulse is allowed, call `ensure_online_for_typing(visible_s=pulse_s, reason="paced_typing")` immediately before `self._send_typing(channel_id)`. If the manager is enabled and returns `false`, return `False` without recording a bot typing pulse and without calling REST typing.

This ordering makes all pacer-owned typing paths inherit the guarantee because they already route through `_send_bot_typing_pulse(...)`:

- `perform_initial_typing_until_stopped(...)` for first visible typing during live coalescing.
- `perform_thinking_typing_until_stopped(...)` while the agent is preparing a live answer.
- `perform_send_typing(...)` for final answers.
- Incremental send typing paths that use `perform_send_typing(...)`, including first and later message parts.

`ensure_online_for_typing(...)` must bypass `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S`. A recent downgrade or soft anti-flap window may never cause typing to happen while invisible. If a sender is attached, the manager should force or confirm `online`; if no sender is attached, it returns `false` and typing is skipped.

Update the low-level Discord send path too. `app.services.discord.send_text(..., send_typing_indicator=True)` is used by fallback/direct sends and currently calls `send_typing(channel_id)` directly. Route that low-level typing attempt through the same manager immediately before `send_typing(channel_id)`. If `ensure_online_for_typing(...)` returns `false`, skip the typing indicator but still proceed to `rest.send_message(...)` when the caller intended to send the message.

After `rest.send_message(...).raise_for_status()` succeeds, call `presence_manager.message_sent()` so successful sends get the bounded after-send tail. This hook belongs after provider acknowledgement; failed sends should not extend presence as if a message was delivered.

If the module-level `send_text(...)` function cannot receive the app-owned manager cleanly through normal dependency injection, add a module-level setter during startup and reset it during shutdown. The setter should only register the shared manager; presence decisions still live inside `DiscordPresenceManager`.

The invariant to preserve in review: no code path may call Discord REST typing unless `ensure_online_for_typing(...)` returned `true` immediately beforehand, or `DISCORD_PRESENCE_ENABLED=false` makes presence control intentionally inactive. Detached Gateway sender means fail closed for typing: no active sender, no typing indicator.

## Soft Activity Transitions

Soft transitions make the bot feel present around real activity, but they are not correctness gates. They may be skipped during reconnect, throttled by `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S`, and suppressed when they would duplicate the current status. Only typing-critical upgrades are mandatory.

Add these observer hooks:

- Inbound accepted-partner DM: after `_handle_message(...)` rejects bot authors, non-whitelisted users, and empty content, call `presence_manager.message_received()` before `process_inbound(...)` or immediately after validation. This requests a short inbound online window using `DISCORD_PRESENCE_INBOUND_ONLINE_MIN_S/MAX_S`.
- Successful send: after `rest.send_message(...).raise_for_status()` succeeds in `send_text(...)`, call `presence_manager.message_sent()`. This requests a randomized after-send tail using `DISCORD_PRESENCE_AFTER_SEND_TAIL_MIN_S/MAX_S`.
- Successful reaction: after `add_reaction(...)` receives a successful provider response, call `presence_manager.reaction_sent()`. This requests a brief reaction tail using `DISCORD_PRESENCE_REACTION_TAIL_MIN_S/MAX_S`.

These transitions should be bounded, deterministic in tests, and globally visible. Random tails must come from the manager's injected `uniform` source, not direct `random.uniform(...)` calls in transport paths. A reconnect or detached sender may cause a soft transition to be dropped because it is only believability polish; do not queue soft updates for later replay.

Soft transitions must not change pacing decisions. `DiscordPacer` remains responsible for `wait`, `react`, `silence`, and `answer`; presence observes accepted inbound messages and successful outbound effects after those decisions happen. Do not use presence state as an input to reaction sparsity, silence, answer timing, or coalescing.

Presence is global to the bot, not per-DM. One user's typing or one accepted DM can make the bot appear online to everyone, so all holds must remain short and bounded. This is also why reaction-only responses should get a small tail, not a long session-like green status.

Anti-flap behavior applies to these soft paths: repeated inbound messages, sends, or reactions inside the transition gap should extend bounded deadlines without sending mechanical status updates every time. Downgrades back to `DISCORD_PRESENCE_DEFAULT_STATUS` should be scheduled by the single manager task described above.

## Test Matrix

Use deterministic unit tests with fake sender, fake clock, fake sleep, and injected `uniform` values. No test should require a real Discord Gateway or REST connection.

| Area | Coverage |
| --- | --- |
| Config defaults | `DISCORD_PRESENCE_*` settings exist with the documented defaults; min/max pairs validate; default idle status is offline-looking. |
| Initial Identify | `DiscordGatewayBot` Identify includes invisible presence while the Gateway remains connected for event ingestion. |
| Duplicate suppression | Repeated inbound, send, or reaction hooks inside the anti-flap gap do not emit redundant opcode `3` updates. |
| Online-before-typing ordering | `_send_bot_typing_pulse(...)` sends or confirms `online` before calling REST typing, and records no typing pulse when the manager returns `false`. |
| Recent-downgrade bypass | A typing-critical upgrade immediately after a downgrade bypasses `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S` and sends `online` before typing. |
| Detached sender fail-closed | With no active Gateway sender, `ensure_online_for_typing(...)` returns `false`; pacer typing paths skip REST typing. |
| Direct send fallback | `send_text(send_typing_indicator=True)` skips low-level typing when online presence cannot be confirmed, but still sends the message. |
| Typing hold and downgrade timing | A typing pulse holds `online` through visible typing plus `DISCORD_PRESENCE_TYPING_TAIL_S`, then downgrades only after holds expire. |
| Deterministic after-send randomness | `message_sent()` uses the injected `uniform` source and stays within `DISCORD_PRESENCE_AFTER_SEND_TAIL_MIN_S/MAX_S`. |
| `send_typing_indicator=False` | Direct sends with typing disabled do not call `ensure_online_for_typing(...)` and do not create a typing hold, but successful sends still apply the after-send tail when presence is enabled. |
| Reaction-only soft tail | Successful `add_reaction(...)` calls `reaction_sent()` and applies a bounded reaction tail without changing reaction policy. |
| Startup sharing | `app/main.py` creates one manager and passes the same instance to `DiscordGatewayBot`, `DiscordPacer`, and any module-level direct-send registration. |
| Stale websocket guard | A delayed downgrade captured before reconnect does not send on the old websocket after `sender_generation` changes. |

Likely test locations:

- `tests/test_config.py` for settings defaults and bounds.
- `tests/test_discord.py` for manager behavior, Gateway Identify payloads, sender attach/detach, direct sends, reactions, and stale generation protection.
- `tests/test_pacer.py` for `_send_bot_typing_pulse(...)` ordering and inherited initial/thinking/final/incremental typing behavior.
- `tests/test_main_startup_pacing.py` for one shared manager at startup.

## Execution Sequence

1. Add and test `DISCORD_PRESENCE_*` settings first so all timing bounds and feature flags are explicit before behavior changes.
2. Implement `DiscordPresenceManager` in isolation with injected sender, clock, sleep, and `uniform`.
3. Prove the two hard invariants in isolated tests: typing upgrades bypass the anti-flap gap, and detached sender suppresses REST typing.
4. Wire the manager into Gateway Identify and opcode `3` presence update sending.
5. Wire pacer and direct typing paths next, because online-before-typing is the hard requirement.
6. Add soft inbound, send, reaction, and after-send transitions after typing guarantees are proven.
7. Finish with startup sharing tests, local validation, staging rollout, and this document review.

## Validation

Future implementation validation should run targeted tests first:

```bash
pytest tests/test_discord.py tests/test_pacer.py tests/test_main_startup_pacing.py tests/test_config.py
```

If targeted tests pass, run the broader suite:

```bash
pytest
```

Validation focus:

- Config tests prove defaults and bounds.
- Gateway tests prove Identify includes invisible presence while the websocket remains connected.
- Manager tests prove duplicate suppression, downgrade timing, generation-token stale sender protection, deterministic randomness, and soft-tail bounds.
- Pacer tests prove online precedes typing, including recent-downgrade and detached-sender cases.
- Startup tests prove one shared manager is passed through Gateway, pacer, and direct-send registration.

## Rollout

Roll out behind `DISCORD_PRESENCE_ENABLED=true` in staging first.

Checklist:

- Confirm Gateway Identify succeeds and does not create a reconnect loop.
- Observe logs for presence update failures, skipped typing during reconnect, and websocket reconnect behavior.
- Confirm idle/no-conversation state is invisible or otherwise not green.
- Confirm typing paths turn online before visible typing and stay online while typing is active.
- Confirm the bot returns to the default offline-looking state after bounded tails.
- Simulate or observe a reconnect window and confirm REST typing is skipped rather than sent without a confirmed online presence update.

Manual staging smoke is an informational rollout check, not an automated gate: in a staging Discord DM, watch the bot appear offline while idle, become online before typing, remain online during typing, and return offline after the after-send tail.

Rollback is config-only: set `DISCORD_PRESENCE_ENABLED=false`. Do not disconnect or cycle the Gateway websocket to fake offline presence; DM reliability remains more important than presence polish.

## Risks

1. Discord may rate-limit excessive presence updates. Mitigate with duplicate suppression, soft-transition anti-flap, and one scheduled downgrade task.
2. Presence is global, not per-DM. Mitigate with short bounded holds and one centralized manager.
3. A delayed downgrade could target an old websocket after reconnect. Mitigate with sender attach/detach and `sender_generation` checks.
4. Direct sends could bypass `DiscordPacer`. Mitigate by routing low-level `send_text(..., send_typing_indicator=True)` through the same manager and testing that path.
5. Discord may reject `invisible` for bot Identify or update despite the docs. Mitigate with `DISCORD_PRESENCE_ENABLED=false` rollback and consider `idle` only as an explicit follow-up decision.
6. During Gateway reconnects, users may not see typing indicators. This is acceptable; sending typing without confirmed online presence is worse.

## Assumptions

1. Discord accepts `invisible` in Identify and Gateway Presence Update payloads as documented.
2. Staying connected to Gateway while invisible preserves live DM ingestion reliability.
3. Presence behavior applies only to Discord; non-Discord providers remain unchanged.
4. No database migration is needed because presence state is runtime state plus configuration.
5. `pacing_events` remains for conversation pacing decisions; presence transitions may be logged but do not require persistence.
6. Skipping typing during Gateway reconnect is preferable to sending typing while online presence is unknown.

## Open Questions

1. If Discord rejects bot `invisible`, should the fallback default be `idle`, or should the feature remain disabled until a different offline-looking approach is chosen?
2. Should scheduled or proactive Discord messages receive the same after-send online tail, or should only live DM conversations affect presence?
3. Should presence updates and skipped typing during reconnect be operator-visible logs, metrics, or tests-only behavior?

## Success Criteria

- Idle/no-conversation Discord presence is not green while the Gateway remains connected and able to receive DMs.
- Gateway Identify includes the configured initial presence when presence is enabled.
- Opcode `3` updates are centralized in `DiscordPresenceManager`.
- Every REST typing call is immediately preceded by successful online confirmation, unless presence is disabled.
- Typing-critical online upgrades bypass `DISCORD_PRESENCE_MIN_TRANSITION_GAP_S`.
- Detached sender or reconnect state skips typing rather than sending typing with unknown presence.
- Soft inbound, send, reaction, and after-send transitions are bounded, anti-flap-safe, and globally visible.
- Tests cover config, manager state, Gateway Identify/update ordering, pacer typing ordering, direct-send typing behavior, reaction tails, and shared startup wiring.
- Rollback requires only `DISCORD_PRESENCE_ENABLED=false` and does not affect Gateway DM ingestion.

## Settled Decisions

- **SD-001** — Keep the Discord Gateway websocket connected for DM reliability. _load_bearing: true_
  Rationale: The Gateway connection is the live DM ingestion path; presence should not be simulated by disconnecting transport.

- **SD-002** — Use explicit Gateway presence for offline-looking idle behavior. _load_bearing: true_
  Rationale: Identify can start with an invisible/offline-looking presence, and later transitions can use opcode `3` Presence Update without sacrificing Gateway receipt.

- **SD-003** — Treat online-before-typing as a hard invariant. _load_bearing: true_
  Rationale: The bot must never display typing while its presence is invisible, stale, or unconfirmed; typing is skipped when online presence cannot be confirmed first.

- **SD-004** — Bound every non-idle online window through `DISCORD_PRESENCE_*` settings. _load_bearing: true_
  Rationale: Short configured tails and jitter windows make presence feel less mechanical without allowing accidental all-day green status.

- **SD-005** — Centralize presence transitions in `DiscordPresenceManager`. _load_bearing: true_
  Rationale: One manager can enforce typing-before-presence, duplicate suppression, sender generation checks, and downgrade scheduling consistently across all Discord paths.

- **SD-006** — Share one app-owned manager while binding the sender to the active Gateway websocket. _load_bearing: true_
  Rationale: Gateway presence updates require the live websocket, while pacing and send paths need the same state to avoid duplicate or contradictory transitions.

- **SD-007** — Require manager-confirmed online presence immediately before every Discord typing call. _load_bearing: true_
  Rationale: Pacer typing and fallback/direct typing both become safe only if they fail closed when the active Gateway sender cannot confirm `online` first.

- **SD-008** — Keep soft presence transitions observational and bounded. _load_bearing: true_
  Rationale: Inbound, send, reaction, and after-send tails should improve believability without changing pacing decisions or leaving the global bot presence green for long.
