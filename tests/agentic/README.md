# Veas M4 — Sisypy Agentic Validation Suite

Agent-behavior validation for the Veas mediator-bot, using the
[Sisypy](https://github.com/peteromallet/sisypy) agentic testing toolkit.

## Directory layout

```
tests/agentic/
├── __init__.py
├── adapter.py          # VeasProjectAdapter (Sisypy AgenticProjectAdapter impl)
├── runner.py           # Entry-point: python -m tests.agentic.runner
├── sisypy_compat.py    # Diagnostic-only shim when external sisypy unavailable
├── fake_pool.py        # M4 fixture-backed fake database pool (T9)
├── checks.py           # Evidence helper assertions (T13)
├── scenarios/          # Sisypy scenario YAML definitions
├── briefs/             # Scenario briefs (markdown)
├── fixtures/           # Compact deterministic fixture definitions
├── reports/            # Goal-mapping report and evidence packs
└── README.md           # This file
```

## External Sisypy dependency

| Field      | Value |
|------------|-------|
| **Package**  | `sisypy` |
| **Source**   | `github.com/peteromallet/sisypy` |
| **Pinned commit** | `650f80307d7f1d14005b954e254e9be3804f8002` |
| **Install**  | `pip install -e ".[agentic]"` |
| **Direct URL** | `git+https://github.com/peteromallet/sisypy.git@650f80307d7f1d14005b954e254e9be3804f8002` |

### Installing

```bash
# From the Veas repository root:
pip install -e ".[agentic]"

# Or directly:
pip install git+https://github.com/peteromallet/sisypy.git@650f80307d7f1d14005b954e254e9be3804f8002
```

### Fallback shim

If the external `sisypy` package cannot be installed or imported, the
diagnostic-only compatibility shim at `tests/agentic/sisypy_compat.py`
is used automatically.  The shim:

- Supports `python -m tests.agentic.runner --help` and structural smoke.
- Emits a clear diagnostic to stderr on import.
- **CANNOT satisfy behavior scenario pass criteria** — all behavior
  scenarios will be classified as `undetermined` when the shim is active.

## Actor modes

| Mode | Purpose | Behavior pass? |
|------|---------|---------------|
| `fake` | Structural harness proof | No |
| `scripted-tool` | Fixture-declared tool calls — evidence plumbing proof | No |
| `real-agent` | Live Veas agent path | **Yes** |
| `recorded-real` | Frozen transcript grading | **Yes** |

Only `real-agent` and `recorded-real` evidence can satisfy agent-behavior
pass criteria (per SD3).

## Behavior-success gating (SD3)

The runner enforces a strict gating discipline:

| Mode | What it proves | Can satisfy behavior pass? |
|------|----------------|---------------------------|
| `fake` | Structural harness integrity — scenario loading, adapter wiring, Sisypy dispatch | **No** — `success_proof_level=authored` only |
| `scripted-tool` | Evidence plumbing — `tool_transcript.json`, `hot_context.md`, `messages_seed.json`, etc. are emitted correctly via `capture_tool_calls()` and `call_tool()` | **No** — `success_proof_level=authored` only; proves the harness emits correct evidence shapes but does not validate agent behavior |
| `real-agent` | Live Veas agent path — runs through `evals.execution.run_eval_turn()` with a real LLM | **Yes** — `success_proof_level=validated` (requires live model credentials) |
| `recorded-real` | Frozen transcript grading — replays and grades a previously captured real-agent evidence pack | **Yes** — `success_proof_level=validated` (requires a frozen `project_specific/` source) |

**Key rule**: Only `real-agent` or `recorded-real` evidence can mark behavior scenarios as passing.
If live model credentials are unavailable, behavior scenarios must be recorded as `undetermined`
rather than incorrectly passed. `fake` and `scripted-tool` modes prove harness correctness but
never claim agent-behavior validation.

### How the gating works in practice

- **`fake`** runs the Sisypy structural dispatcher — it loads scenarios, instantiates the
  adapter, and writes evidence manifests.  The evidence pack will carry
  `success_proof_level=authored` and `structural_only=true`.  No tool calls execute,
  no agent behavior is assessed.

- **`scripted-tool`** executes fixture-declared tool calls through the Veas
  `evals.capture.capture_tool_calls()` and `app.services.tools.registry.call_tool()`
  paths against the M4 fake pool.  All seven project-specific evidence files are
  emitted (`tool_transcript.json`, `hot_context.md`, `messages_seed.json`,
  `expected_behavior.json`, `final_answer.md`, `assertions.json`, `infrastructure.json`).
  The evidence pack still carries `success_proof_level=authored` — tool-call shape is
  proven, but agent behavior is not validated.

- **`real-agent`** connects to a live LLM through `evals.execution.run_eval_turn()`.
  When credentials are available, this mode produces `success_proof_level=validated`
  evidence packs that can satisfy behavior pass criteria.

- **`recorded-real`** grades a previously captured `project_specific/` evidence directory.
  Use `--recorded-source <path>` to point at a frozen evidence pack.  Assertions are
  reassessed from the frozen transcript.

## Commands

### Discovery

```bash
# Help
python -m tests.agentic.runner --help

# List all available scenarios
python -m tests.agentic.runner --list

# Describe the frozen-evidence contract for a mode
python -m tests.agentic.runner --describe-evidence --mode scripted-tool
```

### Structural harness proof (no model needed)

```bash
# Fake structural smoke — all scenarios
python -m tests.agentic.runner --mode fake

# Fake structural smoke — single scenario
python -m tests.agentic.runner --mode fake --scenario positional-current-anchor

# With a custom output directory and tag
python -m tests.agentic.runner --mode fake --out-dir out/agentic/reports/my-run --tag smoke
```

### Evidence plumbing proof (no model needed)

```bash
# Scripted-tool — all scenarios
python -m tests.agentic.runner --mode scripted-tool

# Scripted-tool — single scenario
python -m tests.agentic.runner --mode scripted-tool --scenario semantic-paraphrase

# With custom output
python -m tests.agentic.runner --mode scripted-tool \
  --out-dir out/agentic/reports/plumbing-proof \
  --tag scripted-evidence
```

### Real agent (requires live model credentials)

```bash
# Real-agent — single scenario
python -m tests.agentic.runner --mode real-agent --scenario positional-current-anchor

# Real-agent — all scenarios
python -m tests.agentic.runner --mode real-agent \
  --out-dir out/agentic/reports/live-run \
  --tag production
```

### Recorded-real grading (requires frozen evidence)

```bash
# Grade a previously captured evidence pack
python -m tests.agentic.runner --mode recorded-real \
  --scenario positional-current-anchor \
  --recorded-source out/agentic/reports/prior-run/positional-current-anchor/project_specific

# With custom output
python -m tests.agentic.runner --mode recorded-real \
  --scenario semantic-paraphrase \
  --recorded-source path/to/frozen/project_specific \
  --out-dir out/agentic/reports/regraded \
  --tag regrade
```

### Running the test suite

```bash
# Agentic-specific tests only
python -m pytest tests/test_agentic_checks.py tests/test_agentic_evidence.py \
  tests/test_agentic_fake_pool.py tests/test_agentic_scripted_tool.py \
  tests/test_agentic_real_agent.py -v

# Full project test suite (includes baseline failures — see finalize.json)
python -m pytest tests/ --tb=line -q
```

### Available scenarios

| Scenario ID | Tier | Behavior family |
|-------------|------|-----------------|
| `structural-smoke` | 1 | Harness integrity |
| `positional-scripted-smoke` | 1 | Scripted-tool plumbing |
| `positional-current-anchor` | 2 | Positional navigation |
| `positional-explicit-message` | 2 | Positional navigation |
| `positional-scrollback-cursor` | 2 | Positional navigation |
| `semantic-paraphrase` | 2 | Semantic search |
| `topic-recent` | 2 | Topic recency |
| `proactive-context-gathering` | 2 | Proactive deepening |
| `suppressed-deleted-negative` | 2 | Negative suppression |
| `malformed-unsupported-recovery` | 2 | Error recovery |
