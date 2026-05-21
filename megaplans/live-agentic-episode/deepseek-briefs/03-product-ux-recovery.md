You are DeepSeek V4 Pro acting as a product, UX, and recovery-path reviewer.

Working directory: /Users/peteromalley/Documents/Veas

Read these files:
- megaplans/live-agentic-episode/chain.yaml
- megaplans/live-agentic-episode/m2-agentic-live-prep.md
- megaplans/live-agentic-episode/m3-agentic-live-debrief.md
- megaplans/live-agentic-episode/m4-provenance-durable-writes.md
- megaplans/live-agentic-episode/m5-productization-recovery.md
- web/live-voice/src/components/AgendaCard.tsx
- web/live-voice/src/components/LiveScreen.tsx
- web/live-voice/src/components/ReviewScreen.tsx
- app/routers/live_voice.py

Task:
Critique whether the chain handles user-facing states and failure/retry flows well enough. Focus on latency, polling/background debrief, what the user sees while prep/debrief runs, failed prep/debrief recovery, old conversations without artifacts, and whether Sprint 4 is overloaded.

Return:
- Top 5 UX/recovery risks.
- Missing status/API states.
- Suggested changes to sprint split or done criteria.
- Keep under 900 words.
