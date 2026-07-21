# User Actions

## Railway staging remediation — 2026-07-20

The machine-side portions of the T23 blocker have been resolved:

- Railway CLI 4.12.0 is installed on `megaplan-cloud-agent`.
- Railway authentication is persisted at
  `/workspace/.creds/railway-config.json` and succeeds as the POM account.
- The designated target exists and is linked explicitly as project `Veas`,
  environment `staging`, service `Veas`.
- Project, environment, and service IDs plus `ENV_NAME=staging` are present in
  `/workspace/.creds/superpom-staging.env`.
- The complete operator handoff is at `operator_staging_handoff.md` in this plan
  directory and `/workspace/.creds/superpom-staging-handoff.md`.

T23 must not reuse its old blocker evidence. On resume, first run:

`/workspace/.creds/superpom-staging-preflight`

The only remaining preflight failure is external staging configuration. These
values are deliberately blank because the values available locally equal
production and must not be copied into staging:

- `DATABASE_URL`
- `DIRECT_DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `ANTHROPIC_API_KEY`
- `OPENAI_API_KEY`
- `GROQ_API_KEY`
- `DEEPSEEK_API_KEY`
- `WHATSAPP_VERIFY_TOKEN`
- `DISCORD_BOT_TOKEN`
- `DISCORD_BOT_TOKEN_SUPERPOM`
- `DISCORD_BOT_USER_ID_SUPERPOM`
- `DISCORD_PARTNER_USER_ID_A`
- `DISCORD_PARTNER_USER_ID_B`
- `LIVE_VOICE_TEST_USER_ID`
- `LIVE_VOICE_OPS_USER_IDS`
- `DATA_ENCRYPTION_KEY`
- `ADMIN_PASSWORD`

Populate them only through `/workspace/.creds/superpom-staging.env`. After an
operator creates `/workspace/.creds/superpom-staging.deploy-authorized` with the
exact target line documented in the handoff, publish the config and rerun T23.
Never deploy to or duplicate the production environment as a workaround.
