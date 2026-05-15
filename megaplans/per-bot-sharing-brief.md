# Per-bot partner sharing — design brief

## Context

The Veas codebase runs N bots that each occupy a "domain" for a user (currently: `mediator` = relationship coach for a dyad on topic=relationship; `tante_rosi` = solo pregnancy coach on topic=pregnancy; more bots planned). Today only the mediator's content can flow between dyad partners, gated by a **global** opt-in (`users.cross_thread_sharing_default`). The pregnancy coach writes everything private; her facts cannot reach the partner even when the user wants to share them.

We want to generalise partner-sharing so that **every bot** (current and future) can produce content that the user's partner sees, gated by a **per-bot** opt-in. The user gets one switch per domain ("share my pregnancy stuff with my partner — yes / no / not decided yet"). Until they decide, the bot keeps surfacing the question.

The basic infrastructure already exists for distillations (`visibility: private | dyad_shareable`, `shareable_summary`, `raw_message_visibility()` filtering in `app/services/cross_thread_privacy.py`, `app/services/hot_context.py:500-577`). The work below generalises it across both bots-and-content-types, and replaces the global toggle with a per-bot one.

## Goal (one sentence)

Replace the single global `users.cross_thread_sharing_default` with per-bot opt-in state on `user_bot_state`, extend `dyad_shareable` visibility to `memories` (today it only exists on `distillations`), and rewire the hot-context read path so a partner sees `dyad_shareable` content from any bot where the content's owner has opted in for *that* bot — with NULL ("not decided") triggering a prompt-slot the bot uses to ask.

## Decisions already made (do not re-litigate)

These are settled — the plan should implement them, not re-debate them.

1. **Per-bot opt-in lives on `user_bot_state`** as one new column `partner_share text` with values `'opt_in' | 'opt_out' | NULL`. NULL = "pending, must ask". Key is `(user_id, bot_id)` — same shape as every other per-bot state today.
2. **Memories get `visibility` and `shareable_summary`** mirroring distillations exactly. Default `visibility='private'`. When `visibility='dyad_shareable'`, `shareable_summary` is required.
3. **The opt-in is shown until decided.** While `partner_share IS NULL`, every render of that bot's hot context includes a "pending opt-in" slot in the system prompt asking the bot to raise the question this turn. As soon as the value is non-NULL (`opt_in` OR `opt_out`), the slot drops out and the bot stops raising it.
4. **One shared prompt slot, used by every bot.** Define one canonical pending-opt-in paragraph used by every bot's `render_system_prompt`. Rosi's existing `_FIRST_CONTACT_V1` collapses into it; the mediator's onboarding language for cross-thread sharing also collapses into it. New bots inherit the slot for free — no per-bot prompt work.
5. **One shared tool: `set_partner_sharing(opt_in: bool)`.** Implicit `bot_id` (from the calling bot's scope). Writes to `user_bot_state.partner_share` for the calling (user, bot) pair.
6. **Migration: the existing global flag goes away.** Backfill `user_bot_state[bot_id='mediator', user_id=X].partner_share` from `users.cross_thread_sharing_default` for every existing user, then drop `users.cross_thread_sharing_default`. One model, not two — no fallback path, no two-system-running period.
7. **No per-row UX in v1.** The user gets the per-bot toggle. The bot decides per-call whether a given memory/distillation should be `private` vs `dyad_shareable` based on the content. No user-facing per-row controls.
8. **Hot context cross-bot pull is in scope.** When rendering for user B (the partner of A), pull A's `dyad_shareable` rows from *any* bot where `partner_share[A, that_bot] = 'opt_in'`, surfaced with a provenance prefix (e.g. `from Rosi:`). Same-bot dyad sharing (the existing mediator behaviour) keeps working unchanged in shape, just driven by the new column.

## Files known to be relevant (planner should read at least these)

The planner should still survey the repo, but these are the focal points:

- `migrations/0012_cross_thread_sharing.sql` — defines the global flag being retired.
- `migrations/0015_distillations.sql` — defines `visibility` + `shareable_summary` we're mirroring onto memories.
- `migrations/0022_topic_status_user_bot_state.sql` — defines `user_bot_state` (current schema: `user_id, bot_id, onboarding_state, paused`).
- The next available migration number is `0020` (or whatever is highest after `0019_feedback_reaction_context.sql`); planner should verify.
- `app/services/cross_thread_privacy.py` — `raw_message_visibility()` lives here; needs to flip from global flag to per-bot lookup.
- `app/services/hot_context.py` (especially lines 500-577) — read path; needs (a) per-bot lookup, (b) cross-bot pull for partner, (c) emitting the `partner_sharing_state: 'pending'` signal.
- `app/services/tools/write_tools.py` — `add_memory` (~line 776) and `add_distillation` (~line 1165). The `add_memory` write helper needs to accept `visibility` + `shareable_summary`.
- `app/services/tools/tool_schemas.py` — `AddMemoryInput` needs new fields mirroring `AddDistillationInput.visibility` etc. (~line 996 for the distillation schema as the template).
- `app/bots/mediator.py` and `app/bots/tante_rosi.py` — both need to (a) include the canonical pending-opt-in slot via their `render_system_prompt`, (b) be eligible to call `set_partner_sharing`. Rosi additionally needs prompt language about *when* to write `dyad_shareable` rows once opted in.
- `app/bots/prompts/tante_rosi.py` (`_FIRST_CONTACT_V1`) and the equivalent mediator prompt file — the canonical slot replaces/absorbs whatever ask-the-user-about-sharing language already exists in each.
- `app/bots/` tool dispatch — wherever bot tool allowlists are wired, `set_partner_sharing` must be available to every bot (it's user-state-modifying, like `set_pregnancy_edd`, not domain-specific).

## Invariants to enforce (must hold)

1. **NULL → silent.** When `partner_share IS NULL`, *no* content from that (user, bot) is shared with the partner regardless of per-row `visibility`. The toggle is the gate; per-row visibility only matters when the toggle is `opt_in`.
2. **opt_out → silent.** Same as NULL for the read path. The only difference is the prompt slot doesn't surface.
3. **opt_in → per-row decides.** When the toggle is `opt_in`, dyad_shareable rows from that bot flow to the partner; private rows don't.
4. **No partner → moot.** When the user has no partner (`users.partner_user_id IS NULL`), the prompt slot is suppressed even with `partner_share IS NULL`. Nothing to share to. Don't ask a question that has no answer.
5. **No backsliding on the mediator.** Every existing mediator user must continue to see exactly what they see today — the migration must be observationally equivalent. A user whose current `cross_thread_sharing_default = 'opt_in'` ends up with `user_bot_state[mediator].partner_share = 'opt_in'`; a user with `'opt_out'` ends up `'opt_out'`; NULL stays NULL.
6. **Cross-bot pull is opt-in-gated, not opt-out-gated.** Default is "don't show partner's other-bot content." Only `'opt_in'` from the content owner unlocks the pull. This matters: a partner who hasn't decided cannot accidentally have their content surfaced because of someone *else's* default.
7. **Tool authorisation.** `set_partner_sharing` writes only to the calling user's row for the calling bot. A user calling Rosi cannot toggle their mediator state; Rosi cannot toggle Véas's state. Scope is `(message.sender_id, message.bot_id)`.
8. **The shareable_summary contract.** When `add_memory` or `add_distillation` is called with `visibility='dyad_shareable'`, `shareable_summary` must be non-null and non-empty. Reject the call otherwise. Same rule that already applies to distillations should now apply to memories.

## Edge cases and ordering concerns

The planner should treat these as real, not paranoid:

- **Migration backfill ordering.** The new column on `user_bot_state` must be added and backfilled before `cross_thread_sharing_default` is dropped. The hot-context read path must be flipped to the new column *atomically* with the drop, or the system briefly reads from a dropped column. Plan the migration as a single transaction or with explicit phasing.
- **Backfill for users without a `user_bot_state[mediator]` row.** Some legacy users may not have a `user_bot_state` row for the mediator yet (it may be created lazily on first message). The migration must create rows for any user with a non-NULL `cross_thread_sharing_default`. For users with NULL global setting, decide: either create rows pre-set to NULL (explicit pending state) or skip (lazy creation later, still NULL). Either is defensible — make a call and write it down.
- **The pending slot wording is shared.** Different bots have different voices. The shared slot must be voice-agnostic — short, neutral, instructive to the model, not user-facing copy. The bot's own voice handles the actual question to the user; the slot just tells the bot "raise this naturally this turn."
- **Cross-bot pull volume.** A partner with several opted-in bots could see a long list of "from X:" rows in their hot context. Budget for hot-context length: cap, summarise, or rank. Don't ship a context bomb.
- **Order of cross-bot content.** When pulling rows from multiple bots, by what order? By recency? By bot? Pick one and write it down so the reviewer can check it.
- **Provenance prefix format.** "from Rosi:" / "from Véas:" — bake into a single helper, not sprinkled. Bots are named via the bot registry; use the registry's display name, not a hard-coded string.

## Explicitly out of scope

- Per-row visibility controls exposed to the user. (The bot decides per-row; users only decide per-bot.)
- Re-asking after some time has passed. Once `opt_in` or `opt_out` is set, the slot stays gone. Future work, not this sprint.
- New bot registration code. The pattern must work for new bots when they show up, but we are not adding a new bot in this sprint.
- Per-topic granularity within a bot. Each bot is one domain; one toggle per bot is enough.
- UI surfaces. There is no UI today and we are not building one.
- Encryption-at-rest changes. `shareable_summary` on memories follows whatever the existing memory content encryption pattern is.

## Success criteria

Reviewer should check, in priority order:

**must**
- Migration adds `user_bot_state.partner_share` and backfills it from `users.cross_thread_sharing_default` for every user where the global flag was non-NULL, preserving the same value semantically (`'opt_in' → 'opt_in'`, `'opt_out' → 'opt_out'`).
- Migration adds `memories.visibility` (default `'private'`) and `memories.shareable_summary` (nullable) with a CHECK constraint matching the existing distillations one (`shareable_summary` required iff `visibility='dyad_shareable'`).
- Migration drops `users.cross_thread_sharing_default` only after the read path is updated.
- `cross_thread_privacy.raw_message_visibility()` (and any other reader of the old column) reads from `user_bot_state.partner_share` for the relevant bot, not from `users.cross_thread_sharing_default`.
- `hot_context.py` cross-bot pull lands: when rendering for partner B, B sees A's `dyad_shareable` memories and distillations from any bot where `partner_share[A, that_bot]='opt_in'`, with a provenance prefix from the bot registry.
- `hot_context.py` emits `partner_sharing_state: 'pending'` when `partner_share IS NULL` and the user has a partner; the canonical prompt slot is rendered into the system prompt of any bot whose hot context carries this signal.
- `set_partner_sharing` tool exists, is wired into the tool registry for every bot, and updates `user_bot_state.partner_share` for the calling `(user, bot)` only.
- `add_memory` accepts `visibility` and `shareable_summary`; rejects `dyad_shareable` without `shareable_summary`.
- Rosi's prompt is updated so that when `partner_share='opt_in'`, she writes `dyad_shareable` memories and distillations for non-sensitive facts (with appropriate `shareable_summary`).
- Mediator's existing partner-sharing onboarding language is collapsed into the canonical slot; mediator continues to behave observationally as today for existing users post-migration.
- Tests cover: NULL → no partner content visible; `opt_out` → no partner content visible; `opt_in` + `private` row → no partner content visible; `opt_in` + `dyad_shareable` row → partner content visible with provenance; cross-bot pull respects toggle independently per bot; user with no partner never sees the pending slot.

**should**
- Cross-bot content ordering and length budget are explicit (chosen rule, written down in code or comment).
- Migration is a single transaction or has explicit phasing documented.
- The canonical prompt slot is defined in one place and imported by both bot prompt files.

**info**
- Future work for re-asking or per-topic granularity is captured (e.g., as a ticket via `megaplan ticket new`) but not implemented.

## Notes for the planner

- **Do not invent new bot-ID strings.** Use the existing `bot_id` constants from `app/bots/ids.py`.
- **Do not gate behaviour on bot identity if it can be data-driven.** The whole point is N bots; if Rosi gets a code path that Véas doesn't, that's a smell. The only legitimate per-bot differences are prompt content and topic, both of which are already data-driven.
- **The pending slot's prompt language matters.** Write it once, write it well, and stop. Bots are instructed *what* to do (raise the question this turn), not *how* to phrase it — voice belongs to each bot.
- **The migration is the riskiest piece.** Get the ordering and the backfill right and the rest is mechanical.
