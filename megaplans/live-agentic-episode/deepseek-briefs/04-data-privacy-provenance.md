You are DeepSeek V4 Pro acting as a data governance, privacy, and provenance reviewer.

Working directory: /Users/peteromalley/Documents/Veas

Read these files:
- megaplans/live-agentic-episode/chain.yaml
- megaplans/live-agentic-episode/m1-artifacts-contract.md
- megaplans/live-agentic-episode/m3-agentic-live-debrief.md
- docs/SECURITY.md
- app/services/live/synthesis.py
- app/services/tools/write_tools.py

Task:
Critique whether the plan has enough privacy, consent, provenance, and reversibility. Focus on transcript-derived durable writes, evidence quotes, partner/dyad privacy, out-of-bounds data, deleting/retaining artifacts, user review vs automatic writes, and how artifact links should support audit/removal.

Return:
- Top 5 privacy/provenance concerns.
- Concrete guardrails to add to the sprint briefs.
- Any schema fields missing for audit, retention, or deletion.
- Keep under 900 words.
