You are DeepSeek V4 Pro acting as an adversarial architecture/schema reviewer.

Working directory: /Users/peteromalley/Documents/Veas

Read these files:
- megaplans/live-agentic-episode/chain.yaml
- megaplans/live-agentic-episode/m1-artifacts-contract.md
- megaplans/live-agentic-episode/m2-agentic-live-prep.md
- megaplans/live-agentic-episode/m3-agentic-live-debrief.md
- megaplans/live-agentic-episode/m4-provenance-durable-writes.md
- megaplans/live-agentic-episode/m5-productization-recovery.md

Task:
Critique whether the proposed schema and episode model are structurally sound. Focus on table boundaries, cardinality, queryability, migration safety, artifact payload/versioning, `bot_turns` linkage, and whether `artifact_links` is too generic or not generic enough.

Return:
- Top 5 risks, ordered by severity.
- Concrete changes to the chain/briefs.
- Any schema invariants that should be locked before Sprint 1 starts.
- Keep under 900 words.
