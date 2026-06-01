# Agentic Run Report — structural-smoke

**Tag:** run  

**Mode:** structural  

**Dispatchers:** fake  

**Started:** 2026-06-01T04:34:17.570684+00:00  

**Finished:** 2026-06-01T04:34:18.331792+00:00  


## Agent: smoke-agent (Fake Actor — no-op plumbing check)

- **Outcome:** 🔧 FAKE_NO_OP

- **Success proof level:** authored

- **Actions:** 2 (confidence: high)

- **Assessor verdict:** ❓ UNDETERMINED

  - Summary: Insufficient evidence: project_specific_evidence, infrastructure_status

- **Universal checks:** ❓ UNDETERMINED

  - ✅ deliverable_shape: report.md present with 2 numbered sections.

  - ❌ contradictions: Found 1 unsupported claim(s).

  - ✅ bypass_patterns: No bypass pattern matches detected.

  - ✅ forbidden_commands: No forbidden commands detected.

  - ❌ success_proof_ladder: Actor claims exceed evidence: 1 violation(s).

  - ✅ adapter_manifest_present: out/agentic/reports/run-structural-smoke/evidence/run-structural-smoke-smoke-agent-20260601-043417/project_specific/veas_adapter_manifest.json

  - ✅ behavior_mode_gating: Runner-level gating keeps fake/scripted-tool structural-only; real-agent and recorded-real are the only behavior-eligible modes.

  - ✅ frozen_repo_capture: Frozen repo files captured successfully.

  - ✅ fixture_capture: Declared fixtures: no scenario-specific files declared.

  - ✅ generic_evidence: Generic frozen evidence files are present.

  - ❓ project_specific_evidence: Project-specific evidence missing from frozen evidence pack.

  - ❓ infrastructure_status: infrastructure.json is missing from project-specific evidence.

  - ✅ evidence_manifest_present: out/agentic/reports/run-structural-smoke/evidence/run-structural-smoke-smoke-agent-20260601-043417/project_specific/veas_evidence_manifest.json

- **Errors:** Structural-mode guard active: RUNPOD_API_KEY and cloud credentials stripped, no-GPU constraints enforced.

