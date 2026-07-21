# M3 — Retrieval, Context, and SuperPOM Integration

## Outcome

Make the M1–M2 reflection and derivation contracts searchable, inspectable, and
fully useful to future SuperPOM reasoning without flooding hot context or
weakening privacy.

## Scope

- Add reflections to searchable-content constraints/views, embedding lifecycle,
  worker hydration, retrieval filters/results, source-message provenance, and
  migration parity tests.
- Add list/get/search tools and compact active/recent reflection context for
  SuperPOM; historical entries are retrieved on demand.
- Support opening-versus-closing comparisons and recurring blockers/open loops.
- Complete SuperPOM BotSpec, prompt, tool allowlist, routing, inspection, and
  natural correction behavior over the established M1–M2 services.

## Locked Decisions

- A reflection is evidence, not automatically a settled fact or explanation.
- One emotional episode cannot by itself establish a recurring observation.
- Open loops never schedule messages or create tasks automatically.
- Full payloads remain encrypted; searchable plaintext is minimal and follows
  the repository's established private retrieval policy.
- Hot context stays bounded and Compass-first.

## Open Questions

- Resolve whether reflection canonical search text is a redacted summary, a
  deterministic renderer, or another established encrypted-search convention.
- Resolve the compact recent digest shape and strict token budget.

## Constraints

- Preserve retrieval visibility, partner privacy, OOB, topic, bot, and user
  boundaries.
- Retrieval migrations must retain all existing source types and compatibility
  surfaces.
- Do not broaden commitment/adherence tools into SuperPOM.

## Done Criteria

- Deferred/rejected candidates remain inspectable but do not pollute hot context.
- Keyword and vector retrieval return visible reflections under correct scope and
  reject cross-scope access.
- Hot context stays within tested token bounds.
- Existing retrieval and knowledge tests plus new privacy/idempotency tests pass.

## Touchpoints

- M1/M2 reflection and derivation services
- content-embedding migrations, views, lifecycle, worker, and retrieval service
- read tools and source hydration/provenance
- solo hot-context assembly and SuperPOM rendering

## Anti-scope

- Do not add generic analytics, a universal event store, proactive follow-ups,
  or a second retrieval stack.
- Do not redesign M2 derivation semantics in the retrieval milestone.
- Do not deploy to production in this milestone.
