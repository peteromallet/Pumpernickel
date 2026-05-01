# Operations

This service exposes `GET /health` for uptime checks. The endpoint returns
`{"status":"ok","db":"ok"}` only after the application can run `SELECT 1`
against the configured Postgres database.

## Uptime Checks

Use an external monitor such as UptimeRobot or Better Stack.

1. Create an HTTP monitor pointed at `https://<your-railway-domain>/health`.
2. Use a 1 minute or 5 minute interval.
3. Treat any non-2xx response as down.
4. Configure alert routing outside this repo, for example email, Slack, or pager.

For cron-style pings, configure the same monitor target:

```sh
curl -fsS "https://<your-railway-domain>/health"
```

Railway should also be configured with `healthcheckPath: "/health"` from
`railway.json`, so deploy health and external uptime checks use the same route.

## Spend Counters

Daily LLM spend counters live in Postgres table `llm_spend_log`.

The primary key is `(provider, day)`. Supported provider names in application
helpers are:

- `text`
- `vision`
- `transcription`

Rows are updated by `record_llm_cost()` using an UPSERT that increments
`total_usd`. `is_under_cap()` reads today's row and compares it to the matching
settings cap.

## Rotation Expectation

Counters are keyed by calendar day, so no daily reset job is required for the
foundation plan. Keep historical rows for audit and cost review unless a later
retention policy says otherwise.

## Discord Pacing Audit

Discord conversation pacing records every pre-turn wait, react, silence, or
answer decision in `pacing_events`. Use those rows when debugging why the bot
paused, reacted, stayed quiet, or ran a full turn. The row includes source
metadata, message IDs, signal and preference snapshots, wait duration, reaction,
and any optional LLM judgement or fallback reason.

See [`discord-pacing.md`](discord-pacing.md) for operator tuning variables,
per-user `users.pacing_preferences` keys, typing behavior, source handling, and
the manual Discord smoke-test expectations.

Operational secret rotation is handled outside the app:

- Rotate Supabase service-role keys and API keys in Railway environment
  variables.
- Redeploy or restart the Railway service after changing secrets.
- Verify `/health` after rotation.
- Confirm the next app log contains `heartbeat: alive at <utc_iso>`.
