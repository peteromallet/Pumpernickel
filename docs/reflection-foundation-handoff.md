# Reflection Foundation Handoff (M1)

> **Milestone:** M1 — Reflection Foundation  
> **Migration:** `0063_reflection_foundation` (up + down)  
> **Service modules:** `app/services/reflections.py`, `app/services/reflection_templates.py`  
> **Plan:** `m1-reflection-foundation-20260719-2043` (plan_v1.meta.json)  
> **Date:** 2026-07-19

---

## 1. Overview

M1 delivers the durable, encrypted storage foundation for structured reflections. It
introduces exactly three domain tables, a template registry for payload validation,
a session lifecycle with queue/claim/retry semantics, append-only entry revisions
with correction support, and a traceable knowledge derivation ledger.

**Locked boundary:** M1 does NOT introduce inbound routing, SuperPOM prompts,
retrieval, embeddings, hot context, admin UI, scheduling, scheduled jobs, or
feature flags. Those are deferred to M2+.

---

## 2. Three-Table Contract

| Table | Role | Mutability |
|-------|------|------------|
| `mediator.reflection_sessions` | Mutable coordination + queue/claim state | Mutable (status, claim, retry fields) |
| `mediator.reflection_entries` | Immutable normalized reflection documents | Append-only; corrections create new rows |
| `mediator.reflection_derivations` | Knowledge derivation ledger | Creation + decision update only |

**No fourth association/join table.** Source message IDs are stored as ordered
`uuid[]` arrays directly on `reflection_sessions` and `reflection_entries`, not in
a separate join table.

**No `scheduled_jobs` integration.** Finalized sessions are the durable processing
queue. Workers claim sessions directly via `claim_session()`.

### 2.1 `reflection_sessions`

- **PK:** `id` (uuid, gen_random_uuid)
- **Scope:** `user_id` (FK→users), `topic_id` (FK→topics, nullable), `bot_id` (FK→bots)
- **Source messages:** `source_message_ids uuid[] NOT NULL DEFAULT '{}'`
- **Template classification:** `template_key`, `temporal_scope`, `phase`, `period_start/end`, `timezone`
- **Classification metadata:** `classification_source`, `classification_confidence` (real [0,1]), `classification_metadata` (jsonb)
- **Lifecycle:** `status` (collecting → finalizing → processed / processing_failed / abandoned)
- **Queue/claim state:** `claimed_by`, `claimed_at`, `retry_count`, `failure_class`, `failure_reason`, `last_error`
- **Idempotency:** `idempotency_key` (UNIQUE, nullable)
- **Timestamps:** `created_at`, `updated_at`, `finalized_at`, `processed_at`, `abandoned_at`, `idle_finalize_at`

**Constraints:**
- `status` CHECK: `collecting | finalizing | processed | abandoned | processing_failed`
- `abandoned` ⇒ `abandoned_at IS NOT NULL`
- `processed | processing_failed` ⇒ `finalized_at IS NOT NULL`
- `idle_finalize_at` only meaningful during `collecting`
- Partial UNIQUE index: `(user_id, bot_id) WHERE status = 'collecting'` — enforces at most one collecting session per (user, bot)

### 2.2 `reflection_entries`

- **PK:** `id` (uuid, gen_random_uuid)
- **FKs:** `session_id` (CASCADE), `user_id`, `topic_id`, `bot_id`
- **Denormalized classification:** `template_key`, `temporal_scope`, `phase`, `period_start/end`, `timezone` (copied from session at creation for self-contained reads)
- **Source messages:** `source_message_ids uuid[] NOT NULL DEFAULT '{}'`
- **Encryption (dual-column convention from migration 0007):**
  - `payload_encrypted` (bytea) — AES-GCM encrypted JSON payload (AGV1 prefix)
  - `plaintext_searchable` (text) — minimal canonical plaintext for retrieval/embedding
  - `summary_encrypted` (bytea) — encrypted human-readable summary
- **Versioning:** `schema_version` (INT ≥1), `processor_version` (text), `revision_number` (INT ≥1)
- **Revision chain:** `supersedes_entry_id` (FK→reflection_entries, SET NULL on delete)
- **UNIQUE:** `(session_id, revision_number)`
- **CHECK:** `supersedes_entry_id <> id` (cannot supersede itself)

### 2.3 `reflection_derivations`

- **PK:** `id` (uuid, gen_random_uuid)
- **FKs:** `reflection_entry_id` (CASCADE), `user_id`
- **Kind:** `derivation_kind` — `memory | observation | distillation | orientation`
- **Provenance:** `assertion_source` — `user_explicit | user_implied | agent_inferred`
- **Confidence:** `confidence` (real [0,1], nullable)
- **Eligibility:** `eligibility_reasons` (jsonb — JSON array of rule keys)
- **Supporting evidence:** `supporting_message_ids uuid[]`
- **Encrypted candidate:** `candidate_payload_encrypted` (bytea)
- **Decision:** `decision` — `applied | reinforced | deferred | rejected | superseded` (default: `deferred`)
- **Applied target:** `applied_target_table` (text), `applied_target_id` (uuid) — both required when `decision='applied'`
- **Idempotency:** `idempotency_key` (UNIQUE, nullable)
- **Timestamps:** `created_at`, `decided_at`

---

## 3. State Transitions

### 3.1 Session Lifecycle

```
collecting ──► finalizing ──► processed
   │               │
   │               ├──► processing_failed ──► finalizing (retry)
   │               │
   ▼               ▼
abandoned       (stale claim recovery returns session to finalizing)
```

| Transition | API | Trigger |
|------------|-----|---------|
| → collecting | `open_or_attach_session()` | Reflection train-of-thought begins |
| collecting → finalizing | `finalize_session()` | Caller or sweeper marks session ready for processing |
| collecting → abandoned | `abandon_session()` | Caller discards a session before finalization |
| finalizing → (claimed) | `claim_session()` | Worker atomically claims oldest finalized session |
| (claimed) → processed | `mark_session_processed()` | Worker reports successful processing |
| (claimed) → processing_failed | `mark_session_failed()` | Worker reports failure with `failure_class` |
| processing_failed → finalizing | `retry_session()` | Owner retries (increments `retry_count`) |
| (stale claim) → finalizing | `recover_stale_claims()` | Sweeper recovers claims exceeding `stale_claim_seconds` |

### 3.2 Claim Concurrency

- `claim_session()` uses `FOR UPDATE SKIP LOCKED` + atomic CTE — at most one worker claims any given session.
- Stale claims (default: 300s) are eligible for inline recovery during `claim_session()` or bulk recovery via `recover_stale_claims()`.
- `release_claim()` allows a worker to gracefully return a session to `finalizing`.

---

## 4. Idempotency Keys

| Table | Key Column | Constraint | Behavior |
|-------|-----------|------------|----------|
| `reflection_sessions` | `idempotency_key` | UNIQUE (nullable) | Retried `open_or_attach_session()` with same key → UniqueViolation caught, falls back to attach |
| `reflection_derivations` | `idempotency_key` | UNIQUE (nullable) | Retried `create_derivation()` with same key → UniqueViolation caught, existing row returned |

Idempotency keys are **optional** — callers that do not provide them get normal INSERT
behavior without duplicate detection. Keys must be globally unique within their table
when non-null.

---

## 5. Revision Semantics

Entries are **append-only immutable revisions.** The correction model:

1. **Creation:** `create_entry()` inserts a new row with the next `revision_number` for the session (computed via `MAX(revision_number) + 1` CTE).
2. **Correction:** `correct_entry()` inserts a **new row** with:
   - `supersedes_entry_id` pointing to the prior entry
   - A new `revision_number` (next in sequence)
   - Fresh `payload_encrypted`, `summary_encrypted`, `source_message_ids`
   - The prior entry's row is **never mutated**
3. **Current revision:** The row where `supersedes_entry_id IS NULL` for a given session.
4. **History:** `get_entry_revision_history()` returns all revisions (including superseded) ordered by `revision_number ASC`.

**Selected correction reconciliation fields:** Corrections carry `supersedes_entry_id`
for chain traversal. No correction reason, reconciliation status, or audit/reviewer
fields are stored — M2/M3 may add those if needed. The `schema_version` and
`processor_version` columns on every entry provide processor provenance for
forensic reconstruction.

---

## 6. Template Registry

`app/services/reflection_templates.py` provides a code-based registry keyed by
`(template_key, version)` tuples.

### 6.1 Registered Templates (v1)

| Template Key | Allowed Scopes | Allowed Phases |
|-------------|----------------|----------------|
| `freeform` | all | all |
| `daily_open` | day | opening, prospective |
| `daily_close` | day | closing, retrospective |
| `weekly_open` | week | opening, prospective |
| `weekly_close` | week | closing, retrospective |
| `monthly_open` | month | opening, prospective |
| `monthly_close` | month | closing, retrospective |
| `decision_debrief` | instant, day, custom, none | closing, retrospective, freeform |
| `checkpoint` | all | checkpoint, prospective, retrospective |

### 6.2 Shared Payload Envelope

All templates share an 11-key envelope:

| Key | Type | Zero Value |
|-----|------|-----------|
| `summary` | string | `None` |
| `facts` | list | `[]` |
| `events` | list | `[]` |
| `decisions` | list | `[]` |
| `priorities` | list | `[]` |
| `wins` | list | `[]` |
| `blockers` | list | `[]` |
| `open_loops` | list | `[]` |
| `questions` | list | `[]` |
| `signals` | dict | `{}` |
| `template_data` | dict | `{}` |

### 6.3 Validation Pipeline

1. **Template lookup** — `get_template(key, version)` raises `UnknownTemplateError` or `IncompatibleTemplateVersionError`.
2. **Envelope shape** — rejects unknown keys, validates list/dict/string types per key.
3. **Template-specific validator** — e.g., `template_data` must be a dict when present.
4. **Normalization** — fills absent keys with zero values (`[]`, `{}`, `None`).
5. **Template-specific normalizer** — optional; no built-in template uses one.

### 6.4 Adding a Template

```python
from app.services.reflection_templates import ReflectionTemplate, register_template

register_template(ReflectionTemplate(
    key="my_template",
    version=1,
    allowed_temporal_scopes=frozenset({"day", "week"}),
    allowed_phases=frozenset({"opening", "closing"}),
    validate_payload=my_validator,  # optional
    normalize_payload=my_normalizer,  # optional
))
```

Templates are registered at import time. Adding a template requires code + tests
but NOT a schema migration.

---

## 7. Transaction Boundaries

| API | Transaction Scope | Notes |
|-----|-------------------|-------|
| `open_or_attach_session()` | Single statement (SELECT then INSERT/UPDATE) | UniqueViolation catch → re-read fallback |
| `finalize_session()` | Single UPDATE with WHERE guard | Atomic status transition |
| `claim_session()` | CTE with `FOR UPDATE SKIP LOCKED` | Atomic claim, single statement |
| `create_entry()` | Single INSERT with CTE (next_rev + session_check) | Atomic revision allocation |
| `create_entry_for_claim()` | **Explicit transaction** (`conn.transaction()`) | Claim + entry INSERT in one tx; rollback on either failure |
| `correct_entry()` | Verify superseded entry + INSERT | Two statements, not in explicit tx (revision_number UNIQUE guards against races) |
| `create_derivation()` | Single INSERT + UniqueViolation catch | Idempotency via catch → re-read |
| `update_derivation_decision()` | Single UPDATE with WHERE guard | Atomic decision transition |

**Key transaction boundary:** `create_entry_for_claim()` wraps claim + entry creation
in a single `pool.acquire()` → `conn.transaction()` block. If either fails, the entire
transaction rolls back — no orphaned claims or entries.

---

## 8. Encryption Convention

Following the dual-column convention from migration `0007` and `app/services/crypto.py`:

- **`payload_encrypted`** (bytea): Full JSON payload encrypted with AES-GCM, prefixed with `AGV1` version tag.
- **`plaintext_searchable`** (text): Minimal canonical plaintext stored as-is for retrieval/embedding consumers (M3+).
- **`summary_encrypted`** (bytea): Human-readable summary, same encryption as payload.
- **`candidate_payload_encrypted`** (bytea on derivations): Encrypted candidate payload — encryption is the caller's responsibility; the store accepts raw bytes.

---

## 9. RLS / Security

All three tables follow the established convention from migrations `0038`, `0051`, `0060`:

- `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`
- `REVOKE ALL ... FROM anon, authenticated`
- `deny_anon_*` policy: `FOR ALL TO anon, authenticated USING (false) WITH CHECK (false)`
- `owner_scoped_*` policy: `FOR ALL USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid())`

All tables scope directly on `user_id` — not through conversations or topics. One
participant's reflections are never visible to another, enforced at the database level.

---

## 10. Index Summary

| Table | Index | Purpose |
|-------|-------|---------|
| `reflection_sessions` | `idx_reflection_sessions_one_collecting` (partial UNIQUE) | At most one collecting session per (user, bot) |
| `reflection_sessions` | `idx_reflection_sessions_finalized_ready` (partial) | Oldest-first claim ordering |
| `reflection_sessions` | `idx_reflection_sessions_failed_retry` (partial) | Retry sweeper |
| `reflection_sessions` | `idx_reflection_sessions_idle_due` (partial) | Idle auto-finalization sweeper |
| `reflection_sessions` | `idx_reflection_sessions_user_recent` | User-facing list views |
| `reflection_sessions` | `idx_reflection_sessions_claimed_by` (partial) | Claim recovery by worker |
| `reflection_entries` | `idx_reflection_entries_session_rev` | Entry listing per session |
| `reflection_entries` | `idx_reflection_entries_current` (partial) | Current (un-superseded) entry lookup |
| `reflection_entries` | `idx_reflection_entries_supersedes` (partial) | Revision chain traversal |
| `reflection_entries` | `idx_reflection_entries_user_recent` | User-facing entry listing |
| `reflection_derivations` | `idx_reflection_derivations_entry` | Derivations for an entry |
| `reflection_derivations` | `idx_reflection_derivations_deferred` (partial) | Pending derivation sweeper |

---

## 11. M2 / M3 Consumption Points

### 11.1 M2 — Reflection Processing

M2 processors will consume:
- `claim_session()` / `create_entry_for_claim()` to atomically claim and populate entries
- `reflection_entries.payload_encrypted` + `plaintext_searchable` for retrieval
- `reflection_sessions.source_message_ids` for evidence linking
- `reflection_entries.source_message_ids` for entry-level evidence
- Template registry (`validate_entry_payload`) for payload normalization
- `mark_session_processed()` / `mark_session_failed()` for lifecycle completion

### 11.2 M3 — Knowledge Derivation, Memory, Observation, Distillation, Compass

M3 consumers will consume:
- `create_derivation()` with `derivation_kind` and `assertion_source` for knowledge claims
- `update_derivation_decision()` to transition from `deferred` → `applied`/`rejected`/etc.
- `applied_target_table` + `applied_target_id` to record where derived knowledge was written
- `eligibility_reasons` and `supporting_message_ids` for traceability
- `candidate_payload_encrypted` for the proposed write payload
- `list_derivations_for_entry()` / `list_derivations_for_session()` for cross-referencing
- `list_derivations_for_session()` filtered by `decision='deferred'` for pending work sweeps

### 11.3 Correction Reconciliation (M3+)

- `supersedes_entry_id` on entries provides the revision chain for audit.
- `schema_version` and `processor_version` provide processor provenance.
- Additional reconciliation metadata (correction reason, reconciliation status, reviewer fields) is **not included** in M1. M3 may add these as new columns or via a separate reconciliation table.

---

## 12. Explicitly Out of Scope

The following are **not implemented** in M1 and must not be assumed available by
downstream consumers:

| Area | Status | Notes |
|------|--------|-------|
| **Inbound routing** | Out of scope | No `open_or_attach_session()` calls from message handlers |
| **SuperPOM prompts** | Out of scope | No prompt templates for reflection generation |
| **Retrieval / embeddings** | Out of scope | `plaintext_searchable` column exists but is not consumed |
| **Hot context** | Out of scope | No reflection data fed into active conversation context |
| **Admin UI** | Out of scope | No dashboard or management interface for reflections |
| **Scheduling** | Out of scope | No cron/scheduler integration; idle sessions need external sweeper |
| **Scheduled jobs** | Out of scope | No `scheduled_jobs` table entries created for reflections |
| **Feature flags** | Out of scope | No feature-flag gating on any reflection path |
| **Proactive messages** | Out of scope | North Star prohibition — no unsolicited reflection prompts |
| **Generic longitudinal-state framework** | Out of scope | Modules are domain-specific to reflections |

---

## 13. Service Module Summary

### `app/services/reflections.py` (~2635 lines)

**Read models:**
- `ReflectionSession` (frozen dataclass, `from_row()`)
- `ReflectionEntry` (frozen dataclass, `from_row()`)
- `ReflectionDerivation` (frozen dataclass, `from_row()`)

**Exception surface:**
- `SessionNotFoundError`, `SessionNotCollectingError`, `SessionNotFinalizingError`
- `SessionClaimConflictError`, `SessionFinalizeConflictError`
- `EntryNotFoundError`, `EntryRevisionConflictError`, `EntryCorrectionError`
- `DerivationNotFoundError`, `DerivationIdempotencyConflictError`, `DerivationDecisionError`

**Enumerations (frozensets):**
- `VALID_STATUSES`, `VALID_TEMPORAL_SCOPES`, `VALID_PHASES`, `VALID_FAILURE_CLASSES`
- `VALID_DERIVATION_KINDS`, `VALID_ASSERTION_SOURCES`, `VALID_DECISIONS`

**Store (`ReflectionStore`):**

| Method | Purpose |
|--------|---------|
| `get_session()` | Fetch session by ID (user-scoped) |
| `list_sessions()` | List sessions for user, optional status filter |
| `open_or_attach_session()` | Create or attach to collecting session |
| `finalize_session()` | Collecting → finalizing (durable queue transition) |
| `claim_session()` | Atomic claim of oldest finalized session |
| `release_claim()` | Gracefully release a claim |
| `mark_session_processed()` | Finalizing → processed |
| `mark_session_failed()` | Finalizing → processing_failed |
| `retry_session()` | Processing_failed → finalizing (owner only) |
| `recover_stale_claims()` | Sweeper: recover stale claims |
| `abandon_session()` | Collecting → abandoned |
| `update_idle_finalize()` | Extend idle finalization deadline |
| `find_idle_sessions()` | Find collecting sessions past idle_finalize_at |
| `list_finalized_ready()` | List unclaimed finalized sessions |
| `list_failed_retryable()` | List processing_failed sessions eligible for retry |
| `create_entry()` | Create immutable entry revision |
| `create_entry_for_claim()` | Atomically claim + create entry (single tx) |
| `correct_entry()` | Create correction (new revision superseding old) |
| `get_entry()` | Fetch entry by ID (user-scoped) |
| `get_current_entry()` | Fetch current (un-superseded) entry for session |
| `list_entries()` | List entries scoped by user/bot/topic/session |
| `get_entry_revision_history()` | Full revision chain for a session |
| `create_derivation()` | Record derivation with idempotency |
| `get_derivation()` | Fetch derivation by ID (user-scoped) |
| `get_derivation_by_idempotency_key()` | Lookup derivation by idempotency key |
| `list_derivations_for_entry()` | Derivations for a specific entry |
| `list_derivations_for_session()` | Derivations across all entries in a session |
| `update_derivation_decision()` | Transition derivation decision |

### `app/services/reflection_templates.py` (~509 lines)

- `ReflectionTemplate` (frozen dataclass: key, version, scopes, phases, validators)
- `register_template()`, `get_template()`, `list_template_keys()`, `template_is_registered()`
- `validate_entry_payload()` — full pipeline: lookup → envelope shape → template validator → normalize → template normalizer
- `validate_derivation_payload()` — validates derivation_kind, assertion_source, decision, confidence
- 9 built-in templates registered at import time

---

## 14. Migration Details

- **Up:** `migrations/0063_reflection_foundation.sql` (408 lines) — creates 3 tables, 12 indexes, RLS policies on all 3 tables
- **Down:** `migrations/0063_reflection_foundation.down.sql` (39 lines) — drops all 3 tables in FK-safe order (derivations → entries → sessions)
- **Numbering:** 0063 (0062 was occupied by `orientation_manifestations`)
- **Conventions:** audited from existing migrations — BEGIN/COMMIT, IF EXISTS in down, child-first drop order in down

---

## 15. Test Coverage

| Test File | Tests | Focus |
|-----------|-------|-------|
| `tests/test_reflection_foundation_migration.py` | 30+ | DDL shape, RLS, indexes, encrypted columns, live apply/rollback |
| `tests/test_reflection_services.py` | 120+ | Session lifecycle, entry CRUD, derivation CRUD, privacy, concurrency, retry, correction |
| `tests/test_reflection_templates.py` | 73 | Template registry, payload validation, normalization, error cases |

---

## 16. Quick-Start for M2 Consumers

```python
from app.services.reflections import ReflectionStore
from app.services.reflection_templates import validate_entry_payload

store = ReflectionStore(pool)

# Open or attach to a collecting session
session = await store.open_or_attach_session(
    user_id=user_id,
    bot_id="superpom",
    template_key="daily_close",
    temporal_scope="day",
    phase="retrospective",
    source_message_ids=[msg1, msg2],
)

# Finalize when ready
session = await store.finalize_session(user_id=user_id, session_id=session.id)

# Claim + create entry atomically
entry, claimed_session = await store.create_entry_for_claim(
    user_id=user_id,
    session_id=session.id,
    claimed_by="worker-01",
    bot_id="superpom",
    payload={"summary": "Good day", "facts": ["shipped feature X"]},
)

# Mark processed
await store.mark_session_processed(session_id=session.id, claimed_by="worker-01")

# Create a derivation
derivation = await store.create_derivation(
    user_id=user_id,
    reflection_entry_id=entry.id,
    derivation_kind="memory",
    assertion_source="user_explicit",
    idempotency_key=f"mem-{entry.id}-001",
    supporting_message_ids=[msg1],
)

# Later: update decision
derivation = await store.update_derivation_decision(
    user_id=user_id,
    derivation_id=derivation.id,
    decision="applied",
    applied_target_table="mediator.memories",
    applied_target_id=memory_id,
)
```
