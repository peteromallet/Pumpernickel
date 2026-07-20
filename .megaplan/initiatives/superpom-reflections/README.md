# SuperPOM Reflections Initiative

Status: authorized for unattended pinned-e894 cloud execution.

This initiative decomposes the complete SuperPOM reflections feature into four
dependency-ordered megaplan milestones. The product contract is
[`docs/superpom-reflections-full-build.md`](../../../docs/superpom-reflections-full-build.md).

No implementation is authorized by these files. They are durable inputs for a
future `megaplan chain start` invocation.

## Sizing

The complete feature is estimated at 12–20 engineer-days and crosses several
high-risk contracts: production schema, session concurrency, inbound routing,
private knowledge derivation, retrieval corpus, and SuperPOM behavior. It does
not fit safely in one roughly two-week megaplan. The chain uses four
sprint-sized milestones with explicit handoffs.

## Milestones

1. `m1-foundation` — schema, invariants, template contract, storage services.
2. `m2-capture` — recognition, temporal classification, session lifecycle,
   normalization, derivation, correction, and provenance.
3. `m3-knowledge-retrieval` — retrieval, embeddings, hot context, tools, and
   complete SuperPOM behavioral integration.
4. `m4-hardening-ship` — privacy and migration validation, agentic evaluations,
   admin operations, full regression, staging, and production proof.

## Rubric summary

- M1: overall plan difficulty 5/5; `partnered-5/full/medium` because a schema
  or concurrency mistake could pass local tests while corrupting downstream
  contracts.
- M2: overall plan difficulty 5/5; `partnered-5/full/medium` because subtle
  capture ordering and automatic durable writes can silently misattach evidence
  or pollute knowledge while appearing functional.
- M3: overall plan difficulty 5/5; `partnered-5/full/medium` because retrieval and
  prompt/routing topology are cross-cutting, but M1–M2 freeze the data contracts.
- M4: overall plan difficulty 5/5; `partnered-5/full/medium` because it owns
  production migration, privacy, deployment, and release evidence, while the
  procedural plan itself needs only medium author depth.

The chain is configured for unattended execution with automatic approval and
automatic milestone merging. Launch and supervision must use the pinned-e894
cloud procedure documented by the operator.
