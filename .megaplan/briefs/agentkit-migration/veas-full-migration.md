# Veas agentkit migration: full mediator-bot loop on the kernel

Profile intent: `thoughtful//medium @claude` *(adjust per `--profile` flag at runtime)*.

This milestone migrates Veas's mediator bot off its hand-rolled `_run_agentic` loop and onto `agentkit.loop` + `agentkit.plan.StepPlan`. Operational primitives Veas re-implements (atomic claim, burst coalescing, audit events, LLM provider chaining, hot context) are replaced with the agentkit equivalent where the APIs match, and adapted via thin shims where they don't. The mediator's spec, prompt, charge logic, and escalation gates stay app-local.

## Prerequisites

- `agentkit >= 0.3.2` installable (current public ref: `git+https://github.com/peteromallet/agentkit.git@v0.3.2`). v0.2.0 is the operational-layer minimum but v0.3.x is what's actually been built and tested.
- Audit-2 LLM-adapter patches landed (anthropic error class shadows fixed, router `asyncio.Lock`/`wait_for` hardened, deepseek `reasoning_content` preserved). Without these, the provider router does not actually fall back.

## Source plan

- `agentkit`: `docs/agentkit-design.md`, `docs/operational.md`, `docs/storage.md`.
- This repo, with verified line numbers (run grep before relying on any of them — code drifts):
  - `app/services/agentic.py` — `async def _run_agentic(...)` at **L1503**; `async def run_step(...)` at **L820**; `STEP_ITERATION_CAPS = {...}` at **L1460**; `_check_outbound_oob(...)` at **L1145**; `scheduled_jobs` INSERT at **L1404**.
  - `app/services/hot_context.py` — `class HotContext` at **L38**, `async def build_hot_context(...)` at **L471**.
  - `app/services/debouncer.py` — `class BurstCoalescer` at **L31** (NOT `app/main.py`).
  - `app/services/tools/registry.py` — `TOOL_REGISTRY` and `TOOL_DISPATCH` (~75 entries); `to_anthropic_tools(allowed: set[str])` at **L403**; `call_tool(...)` at **L497**.
  - `tool_schemas.py` — 66 `*Input` Pydantic v2 classes + matching `*Output` classes (so ~66 tools, not "95+").
  - `app/services/deepseek.py` — DeepSeek-as-Anthropic shape-shifter.
  - `app/services/inbound_queue.py` — `claim_messages_for_turn(...)`.
  - `migrations/0018_turn_audit_events.sql` — existing audit-event table schema.
  - `mediator-bot-spec.md` — mediator behavioural contract; crisis definition lives here.

## Goal

All Veas bots run on `agentkit` in prod. The mediator's behaviour, OOB semantics, crisis-escalation gates, and atomic message claim are preserved exactly. `app/services/agentic.py` shrinks to a thin app-layer config defining the `quick_reply` and `extended` `StepPlan`s, mediator-specific gates, and the Veas-shaped daily-spend pre-step check that wraps agentkit's per-step `Budget`.

## Required scope

### 1. Pin agentkit

Add `agentkit @ git+https://github.com/peteromallet/agentkit.git@v0.3.2` (or local editable) to `pyproject.toml`. The migration depends on the v0.3.2 patches: anthropic error class fix, router concurrency, reasoning_content preservation.

### 2. Tools

`tool_schemas.py` is already Pydantic v2 — rebadge each tool to `agentkit.tools.ToolRegistration` (66 tools). Add `operation_kind` (`read` / `write` / `meta`) to each registration; classification source = the read/write split already present in `app/services/tools/read_tools.py` vs `write_tools.py`. Move dispatch from `app/services/tools/registry.py:call_tool` (L497) into the agentkit `Toolkit.dispatch`. Veas's `to_anthropic_tools(allowed: set[str])` at L403 maps to `Toolkit.to_anthropic_tools(allowed=...)` — same shape.

### 3. StepPlans

Define `quick_reply_plan` and `extended_plan` using `agentkit.plan.StepPlan` with the existing step skeleton (`read → consult → respond → record → schedule`). Per-step `allowed_tools` and iteration caps come from `STEP_ITERATION_CAPS` (`agentic.py:L1460`).

### 4. HotContext

`agentkit.state.HotContext` is a `Protocol` whose only required method is `async def render(self) -> str`. Define `class MediatorHotContext(HotContext)` and a module-level factory `async def build_mediator_hot_context(user_id: UUID, bot_id: UUID, conn: ...) -> MediatorHotContext` that wraps Veas's existing `build_hot_context(...)` at `hot_context.py:L471`. Do NOT use `MediatorHotContext.build_for(...)` — that pattern isn't in agentkit's Protocol.

### 5. Atomic claim

Replace the body of `claim_messages_for_turn(...)` in `app/services/inbound_queue.py` with `await agentkit.control.claim.claim_rows(conn, table='messages', where=..., limit=N, claim_actor='veas-turn-runner')`. Note: `claim_rows` returns row dicts; Veas's existing function returns a richer structured tuple — write a thin adapter inside `claim_messages_for_turn` to preserve the existing call sites' return shape.

### 6. BurstCoalescer (adapter required — APIs are NOT compatible)

Veas's coalescer lives at `app/services/debouncer.py:L31` with parameters `on_burst_complete / debounce_seconds / max_seconds / pacer / on_paced_answer / on_paced_reaction / on_live_typing`. Agentkit's `agentkit.control.coalesce.BurstCoalescer` is `Generic[K, T]` with parameters `key_fn / window_ms / on_flush`. These cannot be a one-line swap. Two paths:
- **(preferred for v1)** Keep Veas's `BurstCoalescer` as-is; do NOT migrate it in this milestone. Defer the coalescer migration to a follow-up sprint with its own adapter brief.
- **(if migrated now)** Build a `MediatorCoalescerAdapter` that exposes Veas's existing call surface (`on_burst_complete` etc.) on top of agentkit's primitive. This is a non-trivial adapter — budget 1 day.

### 7. Spend cap — per-step Budget vs Veas's daily cap

Agentkit's `Budget` enforces per-step caps (`max_usd`, `max_input_tokens`, `max_output_tokens`, `max_iterations`) with `mode='abort'` or `mode='defer'`. There is **no `daily_cap_usd` parameter**. Veas's `is_under_cap()` is a **daily** spend check against a Postgres aggregate — a different concept. Wire as:

1. **Pre-step gate**: before `run_plan`, call the existing Veas `is_under_cap()` query. If under cap, proceed. If over cap, persist the turn to `scheduled_jobs` (existing path at `agentic.py:L1404`) and return without running.
2. **Per-step Budget** (separate): construct `Budget(max_usd=<per_turn_cap>, max_iterations=<sum of STEP_ITERATION_CAPS>, mode='defer')` for in-turn protection. The `BudgetDeferred` exception caught by `run_plan` drains partial messages into the conversation store and re-raises; Veas's app code catches it and persists to `scheduled_jobs`.

### 8. Audit events

agentkit's `agentkit_audit_events` table (created by `migrations/agentkit/0002_audit_events.sql`) is a strict superset of Veas's `turn_audit_events` columns BY DESIGN — the 12 Veas columns are listed first, followed by 8 agentkit additions. Note the table **names differ** (`turn_audit_events` vs `agentkit_audit_events`). Two deploy options:

- **(preferred)** Dual-write: agentkit's `PostgresAuditSink` writes to `agentkit_audit_events`; existing Veas code keeps writing to `turn_audit_events`. Diffing job compares the two.
- **(alternative)** Configure `PostgresAuditSink(table='turn_audit_events')` so agentkit writes directly to Veas's existing table. Requires verifying that `PostgresAuditSink` supports a `table` parameter — if not, add one in a small agentkit patch.

### 9. LLM router

Configure `agentkit.llm.router.ProviderRouter` with `[AnthropicLLM(), DeepSeekLLM()]` (or just `[AnthropicLLM()]` for users not in `deepseek_enabled_user_names`). NOTE: agentkit's router uses a **cumulative strike counter** (sideline after `strike_threshold` failures, decrement on success). Veas's `_provider_call_with_fallback` uses **per-hop retry then advance** with an explicit Anthropic→DeepSeek block. These are different semantics:
- The cumulative strike model is the agentkit primitive.
- The Anthropic→DeepSeek block (do not fall back from Anthropic *to* DeepSeek) must be re-implemented as a pre-flight `Gate.pre_step` callback that selects the router instance based on user preference, OR by constructing two routers (one Anthropic-only, one DeepSeek-with-Anthropic-fallback) and picking by user.

### 10. Mediator gates

OOB outbound check (`_check_outbound_oob` at `agentic.py:L1145`) returns a dict with `verdict ∈ {ok, block, rewrite}` and (for `rewrite`) `suggested_rewrite`. agentkit's `Gate.pre_send` returns `Proceed | Rewrite(reason, payload) | Withhold(reason) | Escalate(target)`. Mapping:
- `verdict=='ok'` → `Proceed()`
- `verdict=='block'` → `Withhold(reason)`
- `verdict=='rewrite'` → `Rewrite(reason, payload=suggested_rewrite)`. **Note**: agentkit's kernel does NOT have a built-in rewrite-then-recheck loop for `pre_send` — you'll need an app-level wrapper that re-invokes the gate after applying the rewrite, with a max-rewrite-count to prevent infinite loops.

Crisis-escalation rule (only `escalate_to_partner` when `charge == 'crisis'` OR explicit ask) stays inside the `escalate_to_partner` tool handler. `mediator-bot-spec.md:L23-28` is the canonical source — re-read before cutover.

### 11. Validation cap

agentkit `ToolRegistration.recoverable_error_cap` defaults to **3**. Veas uses **2** consecutive validation errors before aborting. Pass `recoverable_error_cap=2` explicitly on every Veas tool registration if parity matters.

### 12. Newer-inbound check

App-local. Wire as a `Gate.pre_send` callback that returns `Withhold("newer_inbound")` if a newer message has arrived.

## Cutover protocol

1. Deploy with `VEAS_USE_AGENTKIT=false` and `VEAS_SHADOW_AGENTKIT=true` for 24h. Shadow path dual-writes `agentkit_audit_events` for diffing without affecting prod queries against `turn_audit_events`.
2. Nightly diff job compares per-turn outcomes: same outbound text (modulo whitespace), same tool sequence, same `failure_reason`, cost delta < 5%.
3. Eval suite under `evals/` runs against both paths. Scenarios exist (01-10+ in `evals/scenarios/`); per-bot scenarios in `evals/per_bot/` are mostly empty per its README — fill enough to give meaningful parity numbers. Require ≥95% parity on the populated set before flag-flip.
4. Roll out one prod bot at a time at 1h intervals via `VEAS_USE_AGENTKIT_BOTS=<comma-list>`.
5. Monitor: spend, latency, escalations, withhold rate, recoverable-cap rate.
6. After all bots stable for 7 days, delete legacy paths.

## Explicit non-goals

- Do not change the mediator-bot prompt, charge taxonomy, or escalation rules. (`mediator-bot-spec.md` is the canonical source.)
- Do not migrate transcription / vision pre-processing. They run pre-agentic and stay there.
- Do not migrate or change Discord pacing (`DiscordPacer`). Stays per-bot.
- Do not migrate `BurstCoalescer` in this milestone (deferred — see §6).
- Do not change the existing `turn_audit_events` schema, `bot_turns` schema, or any other Veas-prod table beyond an additive `agentkit_audit_events` table for the shadow diff.

## Acceptance criteria

- 66 tools register cleanly via `agentkit.tools.Toolkit.merge`. Schema validation passes against ≥10 recorded real tool-call payloads per operation_kind (read / write / meta).
- Shadow mode: ≥95% turn-level parity (outbound text equivalence + tool sequence) for ≥24h.
- Eval suite: ≥ existing baseline pass rate on the populated `evals/scenarios/`.
- Atomic claim: load test of 100 concurrent message inserts shows zero double-processing.
- Spend cap test: synthetically exhaust daily cap, verify the pre-step gate persists the turn to `scheduled_jobs` and the per-step `Budget(mode='defer')` re-raises `BudgetDeferred` on per-turn cap exhaustion.
- All bots in prod on `agentkit` for ≥7 days. Legacy `_run_agentic` deleted. `agentic.py` shrinks by ≥60%.
- Mediator escalations behave identically: crisis + explicit-ask logic unchanged, audit trail intact in both `turn_audit_events` (Veas) and `agentkit_audit_events` (agentkit).

## Testing notes

- Per-user prompt-cache breakpoints differ between Veas's hand-rolled blocks and agentkit's renderer. Budget 1 day to tune block boundaries to match — measurable cache hit-rate regression is expected and must be closed before cutover.
- DeepSeek adapter: agentkit v0.3.2 preserves `reasoning_content`; verify the migration doesn't accidentally drop it.
- Encryption (`DATA_ENCRYPTION_KEY` + AES-GCM for sensitive metadata) — agentkit's `AESGCMEncryptor` and Veas's `app/services/crypto.encrypt_value` likely use different IV strategies. Round-trip test required before relying on cross-system decryption.
- Concurrency: agentkit's `BurstCoalescer` is async-safe but **not migrated in this milestone**.

## Risks and mitigations

- **Router semantic divergence.** Cumulative strikes vs per-hop retry will behave differently under sustained failure. Surface in shadow mode by injecting synthetic provider failures and comparing fallover paths.
- **Rewrite-then-recheck loop.** Agentkit's `Gate.pre_send` has no built-in re-invoke after `Rewrite`; build the app-level wrapper carefully with a max-rewrite-count to prevent infinite loops.
- **Audit table name divergence.** Diffing `turn_audit_events` vs `agentkit_audit_events` needs schema-aware comparison; column sets are aligned (superset) but row identity / `event_seq` semantics need verifying under load.
- **Mediator behaviour regression.** Crisis logic must not change. Have someone re-read `mediator-bot-spec.md:L23-28` and audit the migration against it before cutover.
