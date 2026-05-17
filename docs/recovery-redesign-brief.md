# Recovery redesign — kill the duplicate-bot_turn bug class

[`thoughtful//high +feedback`]

## Goal

Eliminate the bug class where multiple `bot_turn` rows reference the same
triggering messages, and where crashed `bot_turn` rows can produce orphan
state that the recovery system never reconciles. The current recovery
system *amplifies* this bug class instead of containing it.

## Current state (what we just shipped + what's left)

**Band-aid in prod (commit `39b1541`):** when `_run_agentic` aborts at
the pre-LLM claim gate (zero of N triggering messages claimable), it
now marks any matching crashed `bot_turn` rows
`failure_reason='abandoned_unclaimable'` / `completed_at=now()`. This
stops the runaway requeue loop that fired 250+ events per 12 seconds
in prod today.

**What the band-aid does not fix:**

1. Duplicate `bot_turn` rows for the same triggering messages can
   still be created — observed in prod today: message `81c2da28…` had
   3 bot_turns within 2 minutes (22:10:51, 22:11:45, 22:12:47); message
   `71efc914…` had 3 bot_turns within 3 minutes.
2. Non-atomic completion — `bot_turn` `7c0fa7da` processed 5 messages
   `silent` at 12:28:23 but its own `completed_at` was never set,
   leaving an orphan row pointing at handled messages.
3. Recovery's `add_burst` requeue path is the source of the dup
   cascade — every crashed turn becomes a new turn via
   `target_coalescer.add_burst(...)` at `app/services/recovery.py:249`,
   compounding when the new turn also crashes.

## Settled Decisions

- **SD-001** — Recovery does not call `coalescer.add_burst`. _load_bearing: true_
  Rationale: Active requeue from recovery is the root cause of the duplicate-bot_turn cascade observed in prod today. Recovery's job is to detect inconsistency, mark `bot_turn` rows terminal, release their messages back to a re-claimable state, and walk away. Re-handling is the regular inbound pipeline's job.
- **SD-002** — Add `messages.bot_turn_id` (nullable FK → `bot_turns.id`). _load_bearing: true_
  Rationale: Messages need a single explicit owner so that "is this message currently being handled" is a constant-time lookup, not an inference across `processing_state` + `bot_turns.triggering_message_ids @>`. Foundation for the UNIQUE-index invariant.
- **SD-003** — Partial UNIQUE INDEX `(bot_id, topic_id, message_id)` on a derived junction view where `bot_turns.completed_at IS NULL`. _load_bearing: true_
  Rationale: A message can have at most one in-flight `bot_turn`. The UNIQUE constraint enforces this at the database level so the dup cannot form even under concurrent INSERTs. Implementation may use a CHECK + trigger, or normalize into a `bot_turn_messages` junction table — execution must pick one.
- **SD-004** — `bot_turn` insertion happens in the same transaction as the message claim. _load_bearing: true_
  Rationale: Today, claim and INSERT are sequential. A crash between them leaves messages claimed but no bot_turn (or, in some failure modes, the other way around). Wrapping both in one tx makes it impossible to observe a partial state.
- **SD-005** — `bot_turn` completion happens in the same transaction as the final message-state UPDATE. _load_bearing: true_
  Rationale: Same reasoning as SD-004, applied to the terminal transition. Eliminates the "handled messages with no completed_at" case (`7c0fa7da` in prod).
- **SD-006** — Recovery releases messages from a crashed `bot_turn` by setting `bot_turn_id=NULL` and `processing_state='raw'` in the same transaction that marks the `bot_turn` `failure_reason='crashed', completed_at=now()`. _load_bearing: true_
  Rationale: This is the replacement for the `add_burst` requeue. The released raw messages get picked up by the regular coalescer on the next inbound delivery or by `recover_stale_processing` / `recover_retryable_failed` (which already feed the coalescer once), so they re-enter the pipeline through the single funnel.
- **SD-007** — Backfill of `messages.bot_turn_id` for historical rows is best-effort. _load_bearing: false_
  Rationale: For each `processed`/`failed` message, set `bot_turn_id` to the most-recent matching `bot_turn` (by `triggering_message_ids @>`). Ambiguous cases (multiple matching bot_turns from the dup-cascade bug) get the most-recent one; the historical inconsistency is sealed in place. New rows after the deploy use the atomic path.
- **SD-008** — Existing `tests/spec/test_*.py` invariants must keep passing. _load_bearing: true_
  Rationale: The spec tests pin behavioral invariants (idle_cost, silence_prompt_quota, etc.) that were green at the start of this sprint. Any regression there is a hard block. Add new spec tests for the new invariants (no dup in-flight bot_turn per message; abort path is recovery-only-cleanup).
- **SD-009** — Migration is split into two deploys. _load_bearing: false_
  Rationale: Deploy 1 ships the schema change + dual-write (code reads `triggering_message_ids` but also writes `bot_turn_id`) + the partial UNIQUE INDEX (so new rows can't dup). Deploy 2 (after backfill completes) switches reads to `bot_turn_id` and drops the band-aid in `_run_agentic`. If execution finds a way to land it as one deploy with zero observable inconsistency window, that's acceptable; the split exists to manage risk, not as a hard requirement.

## Non-goals

- **No new product features.** Behavior visible to Discord users is unchanged.
- **No change to the Live Voice agent** — that's a separate pipeline.
- **Not refactoring `_recover_legacy_invariants`** beyond what SD-006 requires. The scheduled_jobs reconciliation, retention-expiry sweeps, and bot_turn crash-marking stay as-is.
- **Not touching `mediator.inbound_handling_attempts`** (migration 0048's ledger). That dual-write is unrelated to the dup-bot_turn problem.
- **Not removing `add_burst`** — the coalescer still calls it from the normal inbound path. Only the recovery callsite at `recovery.py:249` goes away.
- **Not changing the `recover_stale_processing` or `recover_retryable_failed` paths** beyond ensuring they observe the new `bot_turn_id=NULL` invariant when releasing messages.

## Acceptance criteria

These should become megaplan success criteria.

1. **must** — Zero callsites of `coalescer.add_burst` in `app/services/recovery.py`. (`grep -c "add_burst" app/services/recovery.py` → 0.)
2. **must** — A new spec test `tests/spec/test_no_duplicate_inflight_bot_turn.py` ≤ 30 lines that creates a `bot_turn` for message M, then attempts to create a second `bot_turn` for the same M while the first is in-flight, and asserts the second INSERT fails or no-ops.
3. **must** — A new spec test `tests/spec/test_recovery_releases_messages_not_requeues.py` ≤ 30 lines that simulates a crashed `bot_turn`, runs `recover_on_startup`, and asserts: (a) the `bot_turn` row is marked `failure_reason='crashed'` with `completed_at IS NOT NULL`, (b) the triggering messages have `bot_turn_id IS NULL` and `processing_state='raw'`, (c) no new `bot_turn` row was inserted by recovery.
4. **must** — Existing 22 spec tests + 83 backend tests + 6 vitest still green. `tests/spec/` `uv run pytest tests/spec/ -q` produces 22+ passed.
5. **must** — Migrations 0049 (and 0050 if SD-009's two-deploy split is taken) are idempotent — re-running them against the dev DB is a no-op.
6. **must** — `scripts/ship-to-prod.sh` runs end-to-end clean; production smoke at https://veas-production.up.railway.app/health returns 200; new migrations apply.
7. **must** — After deploy, the kill switch (`system_state.recovery_v2_kill`) can be released (`{"on":false}`) without the requeue loop returning. Verify via 5-minute log sample: zero `recovery_requeued` events.
8. **must** — After deploy, the 13 historical `abandoned_unclaimable` rows in `bot_turns` (the band-aid cleanup) get their associated messages' `bot_turn_id` backfilled correctly (most-recent matching turn).
9. **should** — `app/services/recovery.py` body line count decreases vs pre-sprint (currently ~440 lines; SD-001 should remove ~25 lines of `add_burst` orchestration).
10. **should** — The band-aid UPDATE in `_run_agentic` (commit `39b1541`, ~20 lines added) can be deleted in deploy 2 without test regressions. If deploy is single-step (SD-009 alternative), this is removed in the same PR.
11. **info** — Add a one-paragraph "Recovery as passive observer" section to `docs/observability.md` explaining the new invariant.

## What to read in prep

- `app/services/recovery.py` — full file (~440 lines). The `add_burst` callsite at line 249 is what goes away.
- `app/services/agentic.py:1255–1284` — current `INSERT INTO bot_turns`.
- `app/services/agentic.py:1546–1596` — current claim-gate + band-aid cleanup (commit `39b1541`).
- `app/services/inbound_queue.py` — `claim_messages_for_turn`, `recover_stale_processing`, `recover_retryable_failed`. The first will need to take a `new_bot_turn_id` parameter; the others need to NULL `bot_turn_id` when flipping state to `raw`.
- `app/services/coalescer_registry.py` + `app/services/debouncer.py` — the coalescer + `add_burst` signature.
- `migrations/0048_inbound_handling_attempts.sql` — most-recent migration pattern + idempotency wrapper.
- `tests/spec/conftest.py` and one existing `tests/spec/test_*.py` — the pattern for the two new spec tests.
- `scripts/apply_live_voice_migrations.py` — how migrations apply in prod (already extended to 0046–0048).

## Migration ordering (suggested — execute may revise)

Single deploy (preferred if execution finds it safe):

1. `0049_messages_bot_turn_id.sql`: `ALTER TABLE mediator.messages ADD COLUMN bot_turn_id uuid REFERENCES mediator.bot_turns(id)`; backfill via single `UPDATE messages SET bot_turn_id = bt.id FROM bot_turns bt WHERE bt.triggering_message_ids @> ARRAY[messages.id]` (most-recent match wins via subquery); add the partial UNIQUE INDEX.
2. Code: `claim_messages_for_turn` accepts `new_bot_turn_id`, sets it during claim. `INSERT INTO bot_turns` moves inside the same tx as the claim. `_complete_turn` UPDATE merges with the final messages UPDATE. Recovery's `add_burst` block is replaced with the release-tx.
3. Tests: 2 new spec tests, plus extend `tests/conftest.py` FakePool to model `messages.bot_turn_id` + the UNIQUE invariant.
4. Deploy + verify (criterion 7).
5. Drop the band-aid UPDATE in `_run_agentic` in the same PR.

Two-deploy split (fallback per SD-009 if execution sees a risk):

- Deploy 1: schema + dual-write + UNIQUE index. Code still reads from `triggering_message_ids @>`. Band-aid stays.
- Deploy 2: code switches reads to `bot_turn_id`. Band-aid deleted.

## Risk register

- **R1**: UNIQUE INDEX rejects backfilled rows because the dup-cascade bug created multiple in-flight bot_turns for the same message historically. Mitigation: backfill marks all but most-recent `bot_turn` as `failure_reason='abandoned_pre_dedupe', completed_at=now()` first, then index is created against the cleaned-up state.
- **R2**: Recovery's "release messages, walk away" path races against an inbound that arrives during release. The released `bot_turn_id=NULL, processing_state='raw'` window is brief but real. Mitigation: the release tx is single-statement (one UPDATE per crashed bot_turn), and the regular coalescer's claim is atomic — at worst, the new inbound's claim races and one of the two attempts wins cleanly.
- **R3**: Real-PG-only invariants are hard to unit-test against the FakePool. Mitigation: extend FakePool to model `bot_turn_id` + UNIQUE behavior; for the partial-UNIQUE specifically, add a real-PG fixture test under `tests/test_postgres_*.py` (pattern exists).
