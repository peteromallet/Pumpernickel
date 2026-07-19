# Withings Health Integration

Connect Withings scale and watch data to Hector for weight, workouts, and sleep through secure, reliable cloud synchronization.

## Size and Shape

This is a four-sprint epic, approximately 3–4 weeks of skilled engineering
work:

1. Secure provider foundation (`partnered-5/full/medium @codex +prep`)
2. Weight and sleep read models (`partnered-5/full/medium @codex +prep`)
3. Workout adherence projection (`partnered-5/full/medium @codex`)
4. Product hardening and rollout readiness (`partnered-5/full/medium @codex`)

All four milestones are deliberately scored at overall plan difficulty 5/5.
The work crosses protected health data, OAuth/token security, production schema
and synchronization invariants, irreversible-looking adherence effects, and
release/deletion contracts whose failures can survive happy-path tests. `full`
provides the plan/critique/gate/review cycle; `medium` gives author phases room
for cross-system judgment without raising them to open-ended research depth.

The North Star and milestone briefs lock the integration off by default and make
all automated work executable without live Withings credentials.

## Existing Production Registration Contract

The registration endpoints are already deployed on Railway and must remain
stable throughout the epic:

- OAuth: `https://veas-production.up.railway.app/api/health/devices/withings/oauth/callback`
- notifications: `https://veas-production.up.railway.app/api/health/devices/withings/notifications`

Both routes currently provide exact `HEAD 200` validation behavior. Their data
methods fail closed until M1 implements secure OAuth exchange and durable
notification ingestion. Railway owns the six documented `WITHINGS_*` values;
the chain must not copy credentials into tracked files or echo them in logs.

## Cloud Staging

Canonical workspace:

```text
/workspace/withings-health-integration/Pumpernickel
```

Canonical remote spec:

```text
/workspace/withings-health-integration/Pumpernickel/.megaplan/initiatives/withings-health-integration/chain.yaml
```

Tmux session, once launched: `withings-health-integration`.

The shared worker stores provider and GitHub credentials out of this repository.
Keep `cloud.yaml` `secrets: []` so a deploy or sync does not overwrite those
worker-managed values from a local environment.

## Safe Launch Command

Do not launch until explicitly authorized. From the Pumpernickel repository:

```bash
python -m arnold_pipelines.megaplan cloud chain \
  .megaplan/initiatives/withings-health-integration/chain.yaml \
  --cloud-yaml .megaplan/initiatives/withings-health-integration/cloud.yaml \
  --no-editable-install-sync \
  --fresh
```

`--no-editable-install-sync` is intentional: the local Arnold checkout contains
unrelated work and must not be pushed into the shared cloud engine as part of
this product chain.

Before launch, rerun remote preflight:

```bash
python -m arnold_pipelines.megaplan cloud preflight \
  .megaplan/initiatives/withings-health-integration/chain.yaml \
  --cloud-yaml .megaplan/initiatives/withings-health-integration/cloud.yaml
```
