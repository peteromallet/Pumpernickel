You are DeepSeek V4 Pro acting as an adversarial agentic-loop and tool-safety reviewer.

Working directory: /Users/peteromalley/Documents/Veas

Read these files:
- megaplans/live-agentic-episode/chain.yaml
- megaplans/live-agentic-episode/m1-artifacts-contract.md
- megaplans/live-agentic-episode/m2-agentic-live-prep.md
- megaplans/live-agentic-episode/m3-agentic-live-debrief.md
- app/services/agentic.py
- app/services/tools/registry.py
- app/services/turn_context.py

Task:
Critique the plan to introduce private non-chat turns for `live_prep` and `live_debrief`. Focus on reuse of `run_step()`, required submit tools, tool gating, no-outbound guarantees, spend/audit behavior, tool caps of 100 general and 500 debrief, and failure modes when the model does not call the submit tool.

Return:
- Top 5 implementation hazards.
- Which existing functions should be extracted/reused vs left alone.
- Minimal contract for `run_agentic_nonchat_job`.
- Keep under 900 words.
