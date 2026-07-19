---
type: anchor
anchor_type: north_star
slug: withings-health-integration
title: 'North Star: Withings Health Integration'
created_at: '2026-07-13T21:06:56.108623+00:00'
---

# North Star: Withings Health Integration

## End State

A user can connect a Withings account once and trust Hector to use accurate,
private, duplicate-free weight, workout, and sleep trends. Imported health data
has durable provider provenance, survives delayed notifications and revisions,
and complements the existing explicit commitment and manual-event system.

The implementation is provider-shaped and production-ready but remains disabled
by default until an operator supplies Withings credentials, completes live
validation, and enables categories through staged feature flags.

## Non-Negotiables

- Withings data feeds Hector's existing fitness model; it does not replace
  commitments, manual reports, or the adherence board.
- Device observations never create commitments and never infer missed or excused
  adherence.
- A workout affects adherence only through a versioned, reversible, idempotent
  projection to exactly one compatible explicit commitment.
- Weight and sleep cannot satisfy a workout commitment.
- Provider records retain source identity, revision, deletion, device, and time
  provenance. Cursors advance only after a complete successful transaction.
- OAuth state is user-bound and one-time; rotating refresh tokens are encrypted
  and serialized. Health integration fails closed without encryption keys.
- Health measurements, tokens, and raw payloads do not enter logs, default admin
  views, tool-call audit text, partner sharing, or routine LLM context.
- Every new table has strict user scoping, FORCE RLS, and deny-anon posture.
- Withings API/device capabilities are treated as optional and capability-driven;
  absent fields remain null and are never estimated.
- All health and projection feature flags default off, and the full automated
  suite runs without live Withings credentials or network access.

## Explicit Non-Goals

- Direct Bluetooth device integration or a replacement for the Withings app.
- A comprehensive health dashboard, medical diagnosis, recovery score, or
  medical interpretation of body composition or sleep.
- Advanced body-composition UX, stage-level sleep charts, live bed occupancy,
  partner sharing, or probabilistic/LLM workout matching.
- Creating fitness commitments from imported observations.
- Actual vendor approval, production deployment, real-user dogfood, legal
  certification, or waiting through a multi-week observation period inside the
  autonomous chain.
- Broad refactors of messaging, scheduling, authentication, or the generic
  artifact system unrelated to the health integration.

## Allowed Temporary Bridges

- Frozen official-schema-derived fixtures and a fake Withings transport stand in
  for real credentials during the chain.
- Raw provider payload retention stays off by default; fixture files provide
  replay coverage until an explicit retention decision justifies encrypted raw
  payload storage.
- Chat tools and compact summaries are the first user surface; a dashboard is
  deferred.
- Sleep uses nightly aggregates in the MVP. Stage intervals may remain an
  adapter capability without a user-facing timeline.
- Provider-specific code may sit behind a small provider-neutral seam without
  prematurely generalizing for providers not yet selected.

## Drift Signals

- A milestone bypasses source records and inserts provider responses directly
  into `mediator.events`.
- Imported data becomes visible only when an active commitment exists.
- A retry, webhook replay, provider revision, or deletion produces duplicate or
  stale adherence.
- Any automatic matcher guesses between multiple eligible commitments.
- Tests, startup, or default local development require Withings credentials.
- Health values appear in logs, generic audit arguments, default admin listings,
  or partner-visible summaries.
- A milestone activates ingestion/projection flags or claims live rollout
  success without operator evidence.
- Scope expands toward charts, medical advice, or unrelated platform refactors
  before the three core flows are correct end to end.
