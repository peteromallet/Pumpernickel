# Superpom Railway staging target

## Designated target

- Railway project: `Veas`
- Railway environment: `staging`
- Railway service: `Veas`
- Megaplan cloud session: `superpom-reflections-20260719`
- Cloud workspace: `/workspace/superpom-reflections-20260719/Pumpernickel`

Production is not an acceptable substitute for this target.

## Approved runner credential/config channel

The persistent Hetzner worker uses root-only files under `/workspace/.creds`:

- Railway CLI authentication: `/workspace/.creds/railway-config.json`
- Staging application values: `/workspace/.creds/superpom-staging.env`

Both files must be mode `0600`; `/workspace/.creds` must be mode `0700`. Do not
send their contents through Discord, prompts, issue comments, logs, or commits.

The runner entrypoint refreshes ephemeral `/root/.railway/config.json` from the
persistent credential seed on every boot. The live container has already been
seeded and linked explicitly to this staging target.

## Deployment authorization

The environment may be configured and inspected without deploying. A deploy is
authorized only when `/workspace/.creds/superpom-staging.deploy-authorized`
exists and contains the exact line:

```text
project=Veas environment=staging service=Veas-staging
```

This marker scopes authorization to staging and prevents accidental use of the
production environment.

## Runtime configuration gate

`superpom-staging.env` must contain staging-scoped values for the application
variables documented in `README.md`. At minimum, verify that these do not equal
their production counterparts before deployment:

- `DATABASE_URL`
- `DIRECT_DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `DISCORD_BOT_TOKEN`
- `DISCORD_BOT_TOKEN_SUPERPOM`
- `DATA_ENCRYPTION_KEY`
- `ADMIN_PASSWORD`

Set `ENV_NAME=staging`. Keep `SCHEDULER_ENABLED=false` until the staging smoke
test explicitly requires scheduler behavior.

Do not duplicate the Railway production environment wholesale: the current
local application credentials are production credentials.
