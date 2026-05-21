# Live Agentic Episode Chain

This directory contains a five-sprint megaplan chain for making live voice sessions first-class agentic episodes.

Run status without driving:

```bash
megaplan chain status --spec /Users/peteromalley/Documents/Veas/megaplans/live-agentic-episode/chain.yaml
```

Drive one milestone locally without pushing:

```bash
megaplan chain start --spec /Users/peteromalley/Documents/Veas/megaplans/live-agentic-episode/chain.yaml --one --no-git-refresh --no-push
```

Drive the whole chain:

```bash
megaplan chain start --spec /Users/peteromalley/Documents/Veas/megaplans/live-agentic-episode/chain.yaml
```

Milestones:

1. `m1-artifacts-contract`: schema, provenance, and non-chat turn contract.
2. `m2-agentic-live-prep`: private selected-bot prep turn with `submit_live_brief`.
3. `m3-agentic-live-debrief`: private post-session debrief turn with durable writes and `submit_live_debrief`.
4. `m4-provenance-durable-writes`: systematic provenance for debrief-created durable writes.
5. `m5-productization-recovery`: UI states, retries, debug endpoints, metrics, backwards compatibility.
