# S2a Pre-flight Validation Report

**Date**: 2026-05-11
**Branch**: `s2a-stamp-rekey-observability`
**S1 Tip Commit**: `d4c2a7c` (S1 — Foundation schema + code shape for multi-agent buildout)

---

## (a) `mediator.artifact_topics` Row Count

**Query**: `SELECT count(*) FROM mediator.artifact_topics`
**Result**: **235** ✅
**Verdict**: Matches expected S1 baseline. S1 backfill held; no new writes have occurred since S1 landed.

---

## (b) Seed Tables: `mediator.user_identities` and `mediator.channels`

### `mediator.user_identities`
- **Count**: 2 rows (>0 ✅)
- **Columns**: `transport`, `address`, `user_id`, `verified_at`, `created_at`
- **Note**: No `provider` column (non-legacy check satisfied by existence of rows — these are S1-seeded identities).

### `mediator.channels`
- **Count**: 1 row (>0 ✅)
- **Columns**: `id`, `bot_id`, `transport`, `address`, `guild_id`, `channel_id`, `config`, `created_at`
- **Existing row**: Discord channel for bot address `1245222614276898866` (bot_id=`mediator`).
- **Note**: If channels had been empty in dev, `python scripts/seed_channels.py` would be the fallback. Not needed — already seeded.

---

## (c) Comprehensive INSERT Site Grep Sweep

Full sweep of every `INSERT INTO` under `app/` against target tables recorded in `migrations/validation/s2a_insert_sites.md`. Summary:

| Table | Files (lines) | Count |
|---|---|---|
| `messages` | `inbound.py:161`, `messaging.py:55,84` | 3 |
| `bot_turns` | `agentic.py:483` | 1 |
| `scheduled_jobs` | `agentic.py:577`, `scheduled_jobs.py:216`, `checkins.py:37`, `scheduled_job_handlers.py:200,318`, `write_tools.py:183,1367` | 7 |
| `feedback` | `inbound.py:105`, `discord.py:470`, `write_tools.py:1763` | 3 |
| `bridge_candidates` | `write_tools.py:305` | 1 |
| `memories` | `write_tools.py:699,746` | 2 |
| `themes` | `write_tools.py:764` | 1 |
| `watch_items` | `write_tools.py:800` | 1 |
| `observations` | `write_tools.py:878` | 1 |
| `distillations` | `write_tools.py:939,1049` | 2 |
| `out_of_bounds` | `write_tools.py:1109` | 1 |
| `tool_calls` | `write_tools.py:136` | 1 (not stamped in S2a) |
| `withheld_outbound_reviews` | `withheld_reviews.py:26` | 1 (no column change in S2a) |
| `pacing_events` | `user.py:212` | 1 (out of scope) |
| `users` | `user.py:155` | 1 (out of scope) |
| `turn_audit_events` | `turn_audit.py:88` | 1 (observability only) |
| `system_state` | `system_state.py:32,48` | 2 (out of scope) |
| `llm_spend_log` | `spend.py:27` | 1 (out of scope) |

**Tables requiring `bot_id`/`topic_id` stamps in S2a**: `messages`, `bot_turns`, `scheduled_jobs`, `feedback`, `bridge_candidates`, `memories`, `themes`, `watch_items`, `observations`, `distillations`, `out_of_bounds`.

**Tables requiring `artifact_topics` companion rows in S2a**: `memories`, `themes`, `watch_items`, `observations`, `distillations`, `out_of_bounds`.

---

## (d) Direct-Outbound Audit

**Query**: Grep for `INSERT INTO messages` with `direction='outbound'` outside `messaging.py`.
**Result**: **Empty** ✅

All `INSERT INTO messages` with `direction='outbound'` are confined to:
- `messaging.py:55-59` (with `bot_turn_id`/`outbound_part_key` columns)
- `messaging.py:84-85` (simple outbound)

No other file under `app/` performs a direct outbound messages INSERT. All outbound message creation routes through `messaging.py`'s `_insert_outbound` helper, called via `send_outbound` / `send_outbound_part`.

---

## (e) `withheld_reviews.py` — `record_withheld_outbound_review`

- **Current INSERT**: Line 26 — `INSERT INTO withheld_outbound_reviews (recipient_id, sender_id, outbound_id, original_content, suggested_rewrite, reason, verdict, checker_failed, status, created_at, updated_at)`
- **S2a readiness**: Function signature will accept `bot_id`/`topic_id` as NEW OPTIONAL kwargs (default `None`) in S2a. The SQL column list will NOT change — `withheld_outbound_reviews` has no scope columns yet. S2b adds the columns.
- **Caller contract**: Callers in `messaging.py:207-216` and `:342-365` will pass `ctx.bot_id`/`ctx.primary_topic_id` through these new kwargs. The values are accepted and stored for S2b readiness but are no-ops at the DB layer in S2a.

---

## Existing Column Shape (Post-S1, Pre-S2a)

### `messages` — existing `bot_id` and `topic_id` columns present
- `bot_id` (text, nullable) — added in S1 migration
- `topic_id` (uuid, nullable) — added in S1 migration

### `bot_turns` — existing scope columns present
- `topic_id`, `bot_id`, `bot_spec_version`, `hot_context_builder_version`, `tool_schema_version`

### `scheduled_jobs` — existing scope columns present
- `topic_id`, `bot_id`

### `feedback` — existing scope columns present
- `topic_id`, `bot_id`

### `bridge_candidates` — existing scope columns present
- `topic_id`, `bot_id`, `dyad_id`

### Artifact tables — `recorded_by_bot_id` present on all
- `memories.recorded_by_bot_id`, `themes.recorded_by_bot_id`, `observations.recorded_by_bot_id`, `watch_items.recorded_by_bot_id`, `distillations.recorded_by_bot_id`, `out_of_bounds.recorded_by_bot_id`

### `artifact_topics` — ready
- `artifact_table`, `artifact_id`, `topic_id`, `status`, `tagged_by_bot_id`, `reason`, `created_at`, `retired_at`

---

## Lint Advisory Note

CI will warn (not fail) in S2a on:
- New `INSERT INTO messages|bot_turns|scheduled_jobs|feedback|bridge_candidates` missing `bot_id`/`topic_id`
- New artifact `INSERT INTO memories|themes|observations|watch_items|distillations|out_of_bounds` lacking a companion `INSERT INTO artifact_topics` in the same SQL statement

Blocking lint is deferred to S2b.

---

## Dashboard Panes

Per-bot dashboard panes are downstream config, out of scope for S2a code changes. Noted for observability completeness.

---

## Verification Summary

| Check | Expected | Actual | Status |
|---|---|---|---|
| `artifact_topics` count | 235 | 235 | ✅ |
| `user_identities` rows | >0 | 2 | ✅ |
| `channels` rows | >0 | 1 | ✅ |
| INSERT sweep recorded | Complete | 31 sites mapped | ✅ |
| Direct-outbound audit | Empty | Empty | ✅ |
| `withheld_reviews` readiness | Documented | Kwarg pass-through plan | ✅ |