# Cross-partner nudges + bot tool-awareness — brief

**Profile**: `thoughtful//medium` (claude vendor, standard robustness, medium planner depth, low critic depth)

**Mode**: code

**Context window**: assumes the per-bot partner sharing primitive shipped on 2026-05-14 (commit `971c0d0`, migration `0035`) is in production. Builds directly on `user_bot_state.partner_share`, `memories.visibility`, `distillations.visibility`, and the cross-bot pull in `app/services/hot_context.py`.

---

## The problem

Three real-world observations from live Tante Rosi conversations on 2026-05-14:

1. **Partner identity is invisible to solo bots.** Pom asked Tante Rosi to ask Hannah something; Tante Rosi responded as if it had no idea Hannah existed or that Pom was paired with anyone. The bot does not even know "your user has a partner named Hannah" — let alone how the privacy boundary works between them.

2. **There is no way to nudge a check-in on the partner.** Pom wants two things:
   - **Explicit**: "can you check in on Hannah?" → the bot schedules a future turn on Hannah's side.
   - **Bot judgment**: when the bot senses asymmetric care load, long silence near a significant event, or distress that would benefit from looping the partner in, it can decide on its own to schedule a partner check-in — within tight guardrails.

3. **Bots refuse tools they actually have.** Pom asked Tante Rosi for a weekly check-in. Tante Rosi said *"I don't have the ability to set up a scheduled reminder on my end — that's not something I can do from here."* This is **factually wrong** — Tante Rosi already has `schedule_task` (with recurrence: daily/weekly/monthly via `ScheduledTaskRecurrence` at `tool_schemas.py:1182`), `schedule_checkin`, `list_scheduled_tasks`, `update_scheduled_task`, `cancel_scheduled_task`, and `cancel_scheduled_checkin` in her `WRITE_PHASE_TOOLS`. None are excluded by `_COACH_EXCLUSIONS` at `app/bots/tante_rosi.py:60-73`. The bot is refusing real functionality because the system prompt **never taught her she has these tools**. The mediator bot likely has the same blind spots. This is a *prompt-awareness bug*, not a missing-tool bug.

All three problems must be fixed technically AND in the system prompts. Tool-awareness is treated as a first-class deliverable, not a comment in an implementation file.

---

## What's deliberately out of scope

- **Crisis escalation.** Tante Rosi already excludes `escalate_to_partner` for §4.1 reasons (no auto-bridging from solo bots). Do not reuse that path. Partner nudges are a *different product* — gentle, scheduled, recipient-consenting.
- **Cross-bot content sharing beyond what already ships.** Hannah's solo Tante Rosi thread stays private. The nudge causes a turn on Hannah's side, it does not grant Pom read access to her content.
- **Bot-judgment autonomy enabled by default.** The autonomous-judgment prompt guidance ships as comment-blocked text in the prompt slot file, NOT active. Slice 3 lays the foundation but does not flip the switch. We'll enable autonomy only after observing explicit-request usage.
- **Group/n-way nudges.** Strictly 1:1 dyad-partner.

---

## Settled Decisions

### SD-001 — New tool `schedule_partner_checkin`, not extension of `schedule_checkin`
*load_bearing: true*
Rationale: `schedule_checkin` takes an arbitrary `user_id` (see `tool_schemas.py:1410-1443` and `write_tools.py:1658`); reusing it would mean every bot's existing checkin tool could aim at strangers if the bot hallucinated an id. New tool with NO target user id in input — backend resolves the partner. Auth-safe by construction.

### SD-002 — Use `job_type='scheduled_task'`, not `'checkin'`
*load_bearing: true*
Rationale: The unique partial index `idx_scheduled_jobs_one_pending_checkin_per_user` in `migrations/0004_plan5_scheduled_jobs.sql:105` allows only one pending `checkin` per user. A partner nudge inserted as `checkin` would silently supersede the recipient's own pending self-checkin via `_schedule_once` in `app/services/checkins.py:33-41`. That is wrong. Use `scheduled_task` (already wires into `run_agentic_job` via `app/services/scheduled_job_handlers.py:144-149`) and carry the nudge payload in `context` jsonb.

### SD-003 — Recipient `opt_out` and `pending` both hard-block; only `opt_in` allows
*load_bearing: true*
Rationale: This is a cross-partner action, not ordinary self-scheduling. If Hannah has explicitly opted out for this bot, she has refused involvement on this side — hard block. If Hannah is `pending` (never decided), the bot does not know whether she consents; initiating on her behalf is presumptuous. Hard block, with a clean error the caller-bot can verbalize: "Hannah hasn't enabled partner check-ins from me yet — when she next talks to me on her side, I'll ask her." Hard block applies to **both** explicit and bot-judgment sources.

### SD-004 — Five slices in one megaplan
*load_bearing: true*
Rationale:
- **S1**: Partner identity in `hot_context_solo`. Smallest separable fix; resolves observation #1 alone.
- **S2**: `schedule_partner_checkin` tool, explicit-request only, with backend partner resolution and opt-out/pending gating.
- **S3**: Hot-context render of `## Incoming nudge from your partner` block when the job fires + shared `PARTNER_NUDGE_PROMPT_SLOT`. Includes a *commented-out* autonomous-judgment block to be uncommented in a later iteration after observing real usage.
- **S4**: `SCHEDULING_CAPABILITY_PROMPT_SLOT` — mounted in BOTH mediator (`app/services/prompts.py`) and Tante Rosi (`app/services/prompts_solo.py`). Teaches each bot what scheduling tools it actually has and when to use them. Fixes observation #3. See SD-013.
- **S5**: `list_scheduled_checkins` read tool — symmetric to `list_scheduled_tasks`. Lets the bot answer "do you already have a check-in scheduled?" before booking another. See SD-014.

Land all five slices in one PR, but the final commit must NOT enable autonomous partner nudging. The autonomous prompt guidance inside `PARTNER_NUDGE_PROMPT_SLOT` ships as inline comment text or behind a feature-flag default-off — pick whichever is simpler and reversible.

### SD-005 — `nudge_note` is recipient-visible; `reason` is audit-only
*load_bearing: true*
Rationale: The `context` jsonb stores both. The render path (S3) surfaces `nudge_note` in the recipient's `## Incoming nudge from your partner` block. `reason` is logged for audit and NEVER rendered into a prompt. The prompt slot must explicitly forbid quoting originator text or summarizing private content in `nudge_note`. Acceptable: "Pom asked me to check how you're doing today." Not acceptable: "Pom says you've been distant and he's spiraling."

### SD-006 — Shared prompt slot, mounted in mediator + Tante Rosi
*load_bearing: true*
Rationale: Model after `app/bots/prompts/partner_sharing.py:6`. New file `app/bots/prompts/partner_nudge.py` exporting `PARTNER_NUDGE_PROMPT_SLOT`. Mount it in:
- `app/services/prompts.py` (mediator system prompt) — surfaces when the user has a partner.
- `app/services/prompts_solo.py` (Tante Rosi and future solo bots) — surfaces when the user has a partner AND `partner_user.partner_sharing_state_recipient_side` is known.

The slot must include: (a) explicit-request handling shape, (b) opt-out/pending refusal wording, (c) `nudge_note` containment rule, (d) "never claim access to the partner's private thread."

Autonomous-judgment guidance lives in the same file as a separate constant (`_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT`) but is NOT mounted by any prompt renderer in this megaplan. Leave as a draft string with a comment explaining "land after observing explicit usage; gated on a future feature flag."

### SD-007 — New unique partial index + code-side daily rate limit
*load_bearing: true*
Rationale:
- **DB index**: unique partial index on `(user_id, bot_id, (context->>'originating_user_id'))` `WHERE status='pending' AND job_type='scheduled_task' AND context->>'kind' = 'partner_nudge'`. Prevents two pending nudges from the same originator to the same recipient for the same bot.
- **Code rate limit**: count `scheduled_task` rows with `context->>'kind'='partner_nudge'` AND `originating_user_id=ctx.user.id` AND `bot_id=ctx.bot_id` created in the last 24h. If ≥1 already exists in *any* status (pending/completed/superseded), reject with `rate_limited: one partner nudge per (dyad, bot) per 24h`. This is stricter than the unique-pending index — the index just prevents stacking; the code limit prevents fire-and-replace abuse.

### SD-008 — Render `## Incoming nudge from your partner` block in both dyadic and solo hot contexts
*load_bearing: true*
Rationale: When the scheduler fires a `scheduled_task` whose `context.kind='partner_nudge'`, the recipient's turn enters `build_hot_context*` with `trigger_metadata.context.kind='partner_nudge'`. Add a render block surfacing: originator name, `nudge_note`, `created_at` (relative + absolute). Place after the trigger metadata section. Apply to BOTH `app/services/hot_context.py` (dyadic) and `app/services/hot_context_solo.py` (solo) — partner nudges work across both bot shapes.

### SD-009 — Tool callable via `WRITE_PHASE_TOOLS`; explicit cross-user-write justification
*load_bearing: true*
Rationale: Add `schedule_partner_checkin` to `WRITE_PHASE_TOOLS` in `app/services/tools/registry.py:201` so every bot inherits it. `check_write_scope` in `app/services/tools/scope_guard.py:93` only validates topic scope, not target user authorization — by design (single-user assumption). The tool implementation must contain an explicit comment block titled "PARTNER WRITE EXCEPTION" justifying why this tool writes a `scheduled_jobs` row whose `user_id != ctx.user.id` (because partner is resolved server-side via `resolve_dyad_partner` and the row's `bot_id` + `topic_id` stay tied to ctx).

### SD-010 — Originator-only cancellation via `cancel_partner_nudge(job_id)`
*load_bearing: false*
Rationale: Add `cancel_partner_nudge` taking a `job_id` only. Server fetches the row, verifies `context.originating_user_id == ctx.user.id` AND `status='pending'`, then sets `status='cancelled'`. Reject with `not_owner` or `not_pending` otherwise. Add to `WRITE_PHASE_TOOLS`.

### SD-011 — Source field in tool input
*load_bearing: false*
Rationale: `source: Literal['explicit_user_request', 'bot_judgment']` in `SchedulePartnerCheckinInput`. Stored in `context.source`. Telemetry-only in this megaplan; future rate-limit dials may use it. The S3 prompt slot tells the bot to set `source='explicit_user_request'` when responding to direct user words like "check on Hannah", and `source='bot_judgment'` for autonomous nudges (which won't be reachable in this slice anyway because that prompt block ships commented).

### SD-012 — Migration is single transaction; no new columns
*load_bearing: true*
Rationale: New migration `0036_partner_nudge_index.sql` adds only the unique partial index from SD-007. Everything nudge-specific lives in `scheduled_jobs.context` jsonb — no schema changes to `scheduled_jobs` itself. Single BEGIN/COMMIT.

### SD-013 — SCHEDULING_CAPABILITY_PROMPT_SLOT must teach every bot its full scheduling toolset
*load_bearing: true*
Rationale: Observation #3 is the bot refusing functionality it has. The fix is a new shared prompt slot in `app/bots/prompts/scheduling.py` exporting `SCHEDULING_CAPABILITY_PROMPT_SLOT`, mounted in BOTH `app/services/prompts.py` (mediator) and `app/services/prompts_solo.py` (Tante Rosi and future solo bots).

The slot text must:
- Be succinct (≤200 words active text — context budget matters; this AND the partner-nudge slot AND the pending-sharing slot are all loaded every turn).
- Explicitly enumerate the scheduling verbs by name: `schedule_checkin` (one-off user-facing reminder), `schedule_task` (agent-managed brief, supports recurrence: daily/weekly/monthly via `recurrence`), `list_scheduled_tasks` (see pending), `list_scheduled_checkins` (see pending; from S5), `update_scheduled_task` (change time/recurrence/brief), `cancel_scheduled_task`, `cancel_scheduled_checkin`.
- Name 3-5 concrete trigger phrases: *"weekly check-in", "remind me every Monday", "check in with me tomorrow at 9am", "stop the daily reminders", "what reminders do I have set up"*.
- Tell the bot to USE these tools, not refuse — if uncertain about timing, ask one clarifying question and then book it.
- Include an explicit anti-pattern: *Do NOT say "I can't set up a reminder" or "you'd need to set a reminder on your phone" — you have these tools and should use them.*
- Cross-reference the `delay` / `local_when` / `when` field choices (already documented in tool registry).

Mounting order in the renderers: scheduling slot first (capability awareness), partner-nudge slot second (specialized verb), pending-sharing slot last (one-shot onboarding). All three live as named string constants and are joined into the rendered system prompt.

Audit obligation: while implementing S4, verify by reading `prompts.py` and `prompts_solo.py` that no other tools the bot has are similarly under-documented. If you find another silent capability (e.g., reaction handling, OOB lifting, anything in `WRITE_PHASE_TOOLS` that the prompt doesn't mention), flag it in a comment for a follow-up — but do NOT widen scope to fix it in this megaplan.

### SD-014 — New read tool `list_scheduled_checkins`
*load_bearing: true*
Rationale: Today `list_scheduled_tasks` exists for agent-managed tasks but there is no symmetric read tool for user-facing check-ins. Without it, the bot cannot answer *"what reminders do I have set up?"* — only the cancel half exists. Add `list_scheduled_checkins` returning pending `checkin` rows for `ctx.user.id` and `ctx.bot_id` (so a user with both mediator and Tante Rosi check-ins sees only the current bot's). Output shape mirrors `ScheduledTaskRow` minus the recurrence fields (check-ins are one-off by design). Add to `TOOL_DISPATCH`, `READ_TOOLS`, and the schema dict in `tool_schemas.py`.

Out of scope: extending `schedule_checkin` to support recurrence. Recurring user-facing reminders go through `schedule_task` today; that's a deliberate product split, leave it alone.

---

## Invariants the implementation MUST preserve

1. **Hannah's solo Tante Rosi thread remains private to her.** Slice 1's `## Your Partner` block contains zero content from the partner — only identity fields (name, id, timezone) and the partner's `partner_sharing_state` for this bot. NO memories, NO themes, NO distillations, NO messages, NO pregnancy facts.
2. **`schedule_partner_checkin` NEVER accepts a target user id in input.** The Pydantic schema must not have a `user_id` field. Server resolves the partner via `resolve_dyad_partner(ctx.pool, ctx.user.id)`.
3. **`opt_out` and `pending` recipients both result in a clean tool rejection.** Never silent-noop, never auto-downgrade, never bypass.
4. **`nudge_note` and `reason` are separately stored and separately surfaced.** `reason` is never rendered into any prompt context — it's audit-only.
5. **The unique partial index in SD-007 is the ONLY new schema change.** No new columns, no new tables.
6. **Autonomous bot-judgment nudges are NOT reachable in production after this megaplan ships.** The prompt slot ships with autonomous guidance present but commented or behind a feature-flag default-off. We'll enable it in a separate, observation-informed change.
7. **The cross-bot pull for shareable summaries (shipped 2026-05-14) is untouched.** Partner nudges are a separate verb; they do not modify what content flows back to the originator when the recipient turn completes.
8. **Tante Rosi's `_COACH_EXCLUSIONS` is not relaxed.** `escalate_to_partner` stays excluded; the new `schedule_partner_checkin` is a separate tool that solo bots gain explicitly.

---

## Critique findings to pre-resolve (from 2026-05-14 gate iteration 1)

The first plan attempt hit 14 significant gate flags. These are factual corrections that must be reflected in the implementation. Treat each as load-bearing:

1. **Tante Rosi's REAL prompt renderer is `app/bots/prompts/tante_rosi.py`** (around lines 259-264, currently mounts `PENDING_PARTNER_SHARING_PROMPT_SLOT` and `_PARTNER_SHARE_OPT_IN_V1`). `app/bots/tante_rosi.py:20-45` delegates to this file, NOT to `app/services/prompts_solo.py`. The new `SCHEDULING_CAPABILITY_PROMPT_SLOT` and `PARTNER_NUDGE_PROMPT_SLOT` MUST be mounted in BOTH `app/bots/prompts/tante_rosi.py` (the actual Tante Rosi path) AND `app/services/prompts_solo.py` (the generic coach path) AND `app/services/prompts.py` (mediator). Tests must cover all three paths or they don't cover the production bug. Pull the slot constants up once at module level in the new `app/bots/prompts/scheduling.py` / `app/bots/prompts/partner_nudge.py` files and import from both renderer files.

2. **Suppress raw `context.reason` leak.** Existing hot-context renderers append raw trigger context at `app/services/hot_context.py` around line 1276 and `app/services/hot_context_solo.py` around line 834. When `trigger_metadata.kind == 'scheduled_task'` AND `context.kind == 'partner_nudge'`, the renderer MUST suppress the existing raw-context line (which would dump the full jsonb including `reason`) and emit ONLY the curated `## Incoming nudge from your partner` block instead. Do this with an explicit branch, not a generic redaction — keep the kind check narrow to `partner_nudge`.

3. **Test contradiction fix.** Do NOT include the forbidden refusal strings as inline examples *inside* `SCHEDULING_CAPABILITY_PROMPT_SLOT`. Reason: prompt tests assert those strings are absent from the rendered system prompt. The slot's anti-pattern guidance should reference the *behavior* ("never refuse a scheduling request you can fulfill; never tell the user to set a reminder on their phone or calendar"), and the *forbidden phrases* live only in `tests/test_scheduling_capability_prompt.py` as a list of strings to assert-absent.

4. **Correct constant name: `TOOL_REGISTRY`** (in `tool_schemas.py` around line 1785), NOT `TOOL_INPUT_OUTPUT`. Schema registrations for `SchedulePartnerCheckinInput/Output`, `CancelPartnerNudgeInput/Output`, `ListScheduledCheckinsInput/Output`, and `ScheduledCheckinRow` all go in `TOOL_REGISTRY`.

5. **Unique-index test seeds an older-than-24h pending row.** The 24h code rate limit fires BEFORE the database constraint, so a normal second-call-within-24h test returns `rate_limited`, not `duplicate_pending_nudge`. To exercise the unique partial index path, the integration test must seed an existing pending row with `created_at` older than 24h, then attempt a new insert and assert `duplicate_pending_nudge`. Document this explicitly in the test.

6. **`list_scheduled_checkins` placement is intentional asymmetry.** `list_scheduled_tasks` lives in `app/services/tools/write_tools.py:1840` but is registered as a read-phase tool — an existing oddity. For consistency with the rest of `read_tools.py` and because `list_scheduled_checkins` is purely a read, place its implementation in `app/services/tools/read_tools.py` and register it correctly. Add an inline comment noting the divergence from `list_scheduled_tasks` location and a TODO follow-up to move `list_scheduled_tasks` later (out of scope for this PR).

7. **`FakePool` test infrastructure must be extended.** `tests/conftest.py` `FakePool` has pattern-specific SQL handlers. Add handlers for:
   - The partner-nudge `INSERT INTO scheduled_jobs ... context jsonb` with kind='partner_nudge'.
   - Emulating the unique partial index (raise UniqueViolationError on conflict).
   - The 24h rate-limit `SELECT COUNT(*)` query.
   - The originator-only `UPDATE scheduled_jobs SET status='cancelled' WHERE id=$1 AND context->>'originating_user_id' = $2 AND status='pending'`.
   - The `list_scheduled_checkins` `SELECT ... FROM scheduled_jobs WHERE user_id=$1 AND bot_id=$2 AND job_type='checkin' AND status='pending'`.

8. **Prompt-test coverage** must hit:
   - `app.services.prompts.render_system_prompt` (mediator).
   - `app.services.prompts_solo.render_solo_system_prompt` (generic coach).
   - `app.bots.prompts.tante_rosi.render_system_prompt` (the actual Tante Rosi production path).
   - `build_tante_rosi_spec()` integration — pull `BotSpec.render_system_prompt` and verify both new slots appear in the final rendered output.

## Edge cases the test suite must cover

- Solo bot user with no dyad partner → `## Your Partner` block omitted or rendered as `none`; tool returns `no_dyad_partner` cleanly.
- Solo bot user with a dyad partner who has never used this bot (no `user_bot_state` row) → recipient share state is `pending` → tool blocks.
- Recipient `opt_out` → tool blocks with explicit reason in result.
- Both partners attempt to nudge each other within 24h → both succeed (different originator → different unique-index slot); but a single originator nudging twice in 24h → second rejected by code rate limit.
- Cancellation: originator cancels their own pending nudge → ok. Originator tries to cancel partner's nudge → rejected. Non-pending status → rejected.
- Scheduler fires `scheduled_task` with `context.kind='partner_nudge'` → recipient's hot context includes `## Incoming nudge from your partner` block; if rendering this block, originator name and `nudge_note` appear, `reason` does not.
- `nudge_note` is `None` or empty → render falls back to "{originator name} asked me to check in with you" generic text.
- Prompt slot is included in both mediator and Tante Rosi rendered system prompts; for users with no dyad partner, the slot may still be present but the tool will refuse (acceptable — slot mounting is decided per-user-has-partner if simple, else unconditional).
- Tool log: every `schedule_partner_checkin` call writes a `tool_log` row with input args (minus PII redaction is not needed — `nudge_note` is intentional product output, not private leakage).

---

## Files expected to change

Slice 1:
- `app/services/hot_context_solo.py` — fetch partner identity + render `## Your Partner` block.
- `app/services/prompts_solo.py` — surface `{partner_name}` placeholder (optional, only if a prompt rewrite naturally wants it).
- New test `tests/test_hot_context_solo_partner_identity.py`.

Slice 2:
- `tool_schemas.py` — `SchedulePartnerCheckinInput`, `SchedulePartnerCheckinOutput`, `CancelPartnerNudgeInput`, `CancelPartnerNudgeOutput`.
- `app/services/tools/write_tools.py` — `schedule_partner_checkin`, `cancel_partner_nudge` implementations with PARTNER WRITE EXCEPTION comments.
- `app/services/tools/registry.py` — add both to `WRITE_PHASE_TOOLS` and `TOOL_DISPATCH`.
- `migrations/0036_partner_nudge_index.sql` — new unique partial index.
- New test `tests/test_schedule_partner_checkin.py` (opt_in/opt_out/pending/no_dyad/rate_limit/cancellation).

Slice 3:
- `app/bots/prompts/partner_nudge.py` — `PARTNER_NUDGE_PROMPT_SLOT` (active) + `_AUTONOMOUS_PARTNER_NUDGE_PROMPT_SLOT_DRAFT` (inert).
- `app/services/prompts.py` — mount slot in mediator system prompt.
- `app/services/prompts_solo.py` — mount slot in solo system prompt.
- `app/services/hot_context.py` — render `## Incoming nudge from your partner` block when `trigger_metadata.context.kind == 'partner_nudge'`.
- `app/services/hot_context_solo.py` — same block in solo renderer.
- New test `tests/test_partner_nudge_hot_context.py` (block renders for partner_nudge triggers).
- New test `tests/test_partner_nudge_prompt.py` (slot present in mediator and Tante Rosi system prompts).

Slice 4:
- `app/bots/prompts/scheduling.py` (new) — `SCHEDULING_CAPABILITY_PROMPT_SLOT`.
- `app/services/prompts.py` — mount slot in mediator system prompt (before partner-nudge slot).
- `app/services/prompts_solo.py` — mount slot in solo system prompt (before partner-nudge slot).
- New test `tests/test_scheduling_capability_prompt.py` — slot present, names every scheduling tool, no "I can't" anti-pattern phrasing present, ≤200 words.

Slice 5:
- `tool_schemas.py` — `ListScheduledCheckinsInput`, `ListScheduledCheckinsOutput`, `ScheduledCheckinRow`.
- `app/services/tools/read_tools.py` — `list_scheduled_checkins` implementation.
- `app/services/tools/registry.py` — add to `TOOL_DISPATCH`, `READ_TOOLS`, both bot tool sets.
- New test `tests/test_list_scheduled_checkins.py` — returns only ctx.user.id × ctx.bot_id pending checkins.

---

## Success criteria

- **MUST**: Solo bots render `## Your Partner` block with only identity fields + recipient share state (no content). [verifiable]
- **MUST**: `schedule_partner_checkin` schema has no `user_id` field. [verifiable]
- **MUST**: Tool resolves partner via `resolve_dyad_partner` server-side. [verifiable]
- **MUST**: Recipient `opt_out` → tool returns rejection; row NOT inserted. [verifiable]
- **MUST**: Recipient `pending` → tool returns rejection; row NOT inserted. [verifiable]
- **MUST**: New `scheduled_task` rows have `context.kind='partner_nudge'`, `context.originating_user_id`, `context.nudge_note`, `context.reason`, `context.source`. [verifiable]
- **MUST**: New migration 0036 applies cleanly; only adds the unique partial index. [verifiable]
- **MUST**: Recipient hot context renders `## Incoming nudge from your partner` block when the job fires. [verifiable]
- **MUST**: `reason` never appears in any rendered hot context or prompt. [verifiable]
- **MUST**: `PARTNER_NUDGE_PROMPT_SLOT` is mounted in both mediator and Tante Rosi system prompts. [verifiable]
- **MUST**: Autonomous-judgment prompt guidance is present in the file but inert (commented or unmounted). [verifiable]
- **MUST**: `SCHEDULING_CAPABILITY_PROMPT_SLOT` is mounted in both mediator and Tante Rosi system prompts and names every scheduling tool by name (`schedule_checkin`, `schedule_task`, `list_scheduled_tasks`, `list_scheduled_checkins`, `update_scheduled_task`, `cancel_scheduled_task`, `cancel_scheduled_checkin`). [verifiable]
- **MUST**: The rendered system prompts for both bots do NOT contain anti-pattern refusal phrasing about scheduling ("I don't have the ability", "I can't set up a reminder", "you'd need to set a reminder on your phone"). Test asserts these strings are absent and the capability slot is present. [verifiable]
- **MUST**: `list_scheduled_checkins` returns pending checkin rows scoped to `ctx.user.id` AND `ctx.bot_id`. [verifiable]
- **MUST**: All existing tests still pass; new tests cover the edge cases above. [verifiable]
- **SHOULD**: Tool tests verify the unique partial index rejects a second pending nudge with the same originator → recipient → bot. [verifiable]
- **SHOULD**: `cancel_partner_nudge` works for originator only. [verifiable]
- **INFO**: Telemetry/`source` field is stored; no downstream consumer yet. [reference]
