# SuperPOM Compass runbook

SuperPOM is a solo review-and-alignment bot. It helps the user surface what
matters right now, compare it against existing goals and commitments, and pick
one concrete next move. Compass is the read model: `app.services.compass`
aggregates `mediator.user_orientation_items` into a prioritized, reviewable
view that SuperPOM consumes instead of inventing its own state.

## Architecture

```
user_orientation_items (source of truth)
         │
         ▼
app.services.user_orientation  ──►  app.services.compass
                                          │
                                          ▼
                                   SuperPOM turn context
```

- **Orientation storage** (`migrations/0060_user_orientation.sql`): one row per
  orientation item. Fields include `label`, `detail`, `kind`, `source`,
  `status`, `review_state`, `owner_user_id`, `bot_id`, and timestamps.
- **Compass read model** (`app/services/compass.py`): builds a snapshot for a
  user/bot scope, grouping items by status and priority so the bot sees a
  curated summary rather than raw rows.
- **SuperPOM BotSpec** (`app/bots/superpom.py`): defines step instructions
  (`read`, `consult`, `respond`, `record`, `schedule`, `done`) and the tool
  allowlist. It is registered alongside the other bots in
  `app/bots/registry.py`.

## SuperPOM behavior

1. **Clarify before advising.** Ask one focused question at a time. Do not
   paraphrase partner-private material as certainty.
2. **Compass-first read.** Use `list_orientation_items` / Compass context to
   understand the current state before proposing anything.
3. **One concrete next move.** End useful turns with a single actionable step,
   not a multi-step plan or generic coaching essay.
4. **Review and correction.** When the user corrects an item, use
   `review_orientation_item` to update `review_state` rather than silently
   editing storage.
5. **Provisional `bot_proposed` state.** Items suggested by the bot are created
   with `status='bot_proposed'` and require explicit user acceptance before
   becoming `active`.
6. **Completed-goal rendering.** Finished items are surfaced in summaries so the
   user sees progress without the bot congratulating itself.
7. **Shame / perfectionism guardrails.** Avoid moral scoring, ideal-self
   impersonation, or sprawling advice. Name the pattern, then ask what the user
   wants to do next.
8. **Generic-advice avoidance.** SuperPOM differs from Coach: it does not hand
   out domain playbooks; it orients the user around their own Compass state.

## Privacy and safety rules

- **Partner-private suppression.** Never quote or paraphrase partner-private
  details. If Compass data is gated as private, treat it as absent.
- **No cross-topic leakage.** Use only the user's own Compass items and the
   current turn context. Do not pull partner-bridge or cross-topic summaries
   into SuperPOM reasoning.
- **Current-priority conflict handling.** When the user names an urgent
   priority that conflicts with an active goal, surface the tension explicitly
   and ask which to elevate; do not silently override Compass state.

## Operational commands

### Per-bot eval corpus

```bash
python -m pytest tests/test_superpom_per_bot_corpus.py tests/test_evals_scenario.py -q --tb=short
```

### Orientation / Compass focused tests

```bash
python -m pytest tests/test_compass.py tests/test_orientation_tool_schemas_and_handlers.py tests/test_open_asks.py -q --tb=short
```

### SuperPOM registration and prompt tests

```bash
STAGING=1 python -m pytest tests/test_superpom_registration.py tests/test_superpom_prompt.py tests/test_superpom_pacing.py tests/test_superpom_migration.py -q --tb=short
```

### Live persona / session flow

```bash
python -m pytest tests/test_live_router_prep.py tests/test_live_ownership.py tests/test_live_turn_loop.py -q --tb=short
```

### Telemetry script

```bash
python scripts/check_per_bot_panels.py --help
python -m pytest tests/test_per_bot_telemetry.py::test_check_per_bot_panels_help_runs_without_asyncpg tests/test_per_bot_telemetry.py::test_check_per_bot_panels_turn_audit_events_uses_metadata_bot_id -q --tb=short
```

With a real `DATABASE_URL`:

```bash
python scripts/check_per_bot_panels.py --bot-id superpom --hours 24
```

### Frontend build

```bash
npm --prefix web/live-voice run build
```

## Troubleshooting

- **SuperPOM does not appear in `/api/live/personas`.**
  - Verify `STAGING=1` is set if registration is staging-gated.
  - Check that `app/bots/registry.py` includes the SuperPOM BotSpec.
  - Confirm `app/bots/ids.py` defines `SUPERPOM_BOT_ID = "superpom"`.

- **Compass returns empty for an existing user.**
  - Check `user_orientation_items` rows for `owner_user_id` and `bot_id`.
  - Items created for a different bot or partner scope are intentionally
    excluded.

- **`check_per_bot_panels.py --bot-id superpom` returns no events.**
  - `turn_audit_events` stores `bot_id` in `metadata->>'bot_id'`, not a
    top-level column. The script query must filter on the JSONB path.
  - See `tests/test_per_bot_telemetry.py` for the covered SQL path.

- **Migration ordering failures.**
  - SuperPOM uses migration `0061_superpom_topic.sql`. Ensure
    `0060_user_orientation.sql` ran first.
