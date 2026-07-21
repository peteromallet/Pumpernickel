# M4 — Product Hardening and Ship

## Outcome

Prove the complete reflections system under production-like conditions, provide
focused operational diagnosis, fix every blocking defect, and prepare an
evidence-backed staging and production handoff.

## Scope

- Add a focused admin/operator view for active/stuck sessions, processing state,
  classifications, derivation decisions, retries, and embedding coverage without
  exposing sensitive reflection content.
- Complete structured logs, failure classification, safe retry operations, and
  deletion/retention cleanup.
- Run real migration-chain and rollback validation against scratch Postgres.
- Complete classification, correction, privacy, idempotency, retry, retrieval,
  hot-context, prompt, and agentic evaluations.
- Run the full existing test suite and fix regressions at their source.
- Deploy the complete build to staging, exercise explicit and implicit text and
  voice reflections, verify derivations/search/corrections/recovery, and produce
  deployment evidence.
- Prepare production migration/apply/rollback commands and final release proof.
- Deploy to production only under the repository's established deployment
  authority and operational process.

## Locked Decisions

- The complete feature ships together: no feature flags, pilot schema, dormant
  core path, or partial production activation.
- Staging is a verification environment, not a partial rollout mechanism.
- The system sends no scheduled reflection messages.
- Sensitive payloads must not appear in logs, metrics, exceptions, or admin
  listings.

## Open Questions

- Identify the existing deployment authority and exact production migration
  workflow at execution time.
- Choose the smallest admin surface consistent with existing operator patterns.
- Establish concrete classification-quality evidence and acceptable remaining
  limitations from the completed evaluation corpus.

## Constraints

- Do not delete, reset, or rewrite production data.
- Do not claim completion from passing unit tests or a successful deploy alone.
- Preserve unrelated working-tree changes and existing bot behavior.
- Any production action requiring new authority must stop at an explicit
  execution breakpoint.

## Done Criteria

- Focused suites and the full repository suite pass.
- Scratch migration up/down and staging migration/deploy complete successfully.
- One explicit and one implicit reflection work end to end in staging, including
  source provenance, knowledge derivation, retrieval, and correction.
- Retry/restart recovery is demonstrated without duplicate entries or
  derivations.
- Cross-user/topic/bot and sensitive-content negative tests pass.
- No reflection path seeds a scheduled job or sends proactive outreach.
- Final handoff records commands, test results, deployment identifiers, evidence,
  rollback procedure, operational diagnosis, and concrete residual limitations.

## Touchpoints

- admin/operator surfaces and operational scripts
- all reflection and SuperPOM integration surfaces from M1–M3
- migration/live database test harness
- agentic evaluation suite and runbooks
- staging and production deployment workflow

## Anti-scope

- Do not redesign unrelated admin pages, schedulers, bots, or deployment
  infrastructure.
- Do not add feature flags as a substitute for fixing a failing integrated path.
