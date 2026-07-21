# Superpom T23 machine handoff

The established deployment target is Railway project `Veas`, environment
`staging`, service `Veas`. Never substitute `production`.

The runner is already equipped with Railway CLI 4.12.0 and authenticated as the
POM Railway account. The active checkout is:

`/workspace/superpom-reflections-20260719/Pumpernickel`

Machine-local operator inputs are under root-only `/workspace/.creds`:

- `railway-config.json` — persistent Railway CLI authentication
- `superpom-staging.env` — staging-scoped application runtime values
- `superpom-staging-preflight` — validates target, file modes, required values,
  Railway authentication, and production isolation without printing secrets
- `superpom-staging-publish-config` — publishes validated values to Railway with
  deploys suppressed
- `superpom-staging.deploy-authorized` — explicit staging-only authorization

Before T23 deployment:

1. Run `/workspace/.creds/superpom-staging-preflight`.
2. Run `/workspace/.creds/superpom-staging-publish-config`.
3. Require `/workspace/.creds/superpom-staging-preflight --require-authorization`.
4. From the active checkout, deploy explicitly with
   `railway up --detach --service Veas-staging --environment staging`.
5. Capture only the deployment ID/status/domain and health output; never record
   variables or credential contents.
6. Verify `/health`, then update `docs/reflections_m4_release_evidence.md` and
   rerun T23/T24 from the existing M4 plan rather than creating a new initiative.

The preflight follows the real `app.config.Settings` startup contract and the
staging live-voice boot guard. It requires database/Supabase isolation, provider
keys, the required WhatsApp verification placeholder, staging Discord bot and
partner identities, encryption/admin values, and live-voice operator/test-user
identities. It intentionally keeps the scheduler disabled for the first smoke.

Authorization is valid only when the marker contains exactly:

`project=Veas environment=staging service=Veas-staging`

At handoff creation, the staging runtime secrets were still blank and the
authorization marker was absent. Do not deploy until both preflight gates pass.
