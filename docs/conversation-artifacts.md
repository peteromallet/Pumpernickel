# Conversation artifacts — design doc

Status: implemented (Sprint 1 — schema + helpers).  
Last updated: 2026-05-21.  
Parent project: [live-agentic-episode](../megaplans/live-agentic-episode/README.md).  
Security posture: see [docs/SECURITY.md](./SECURITY.md).

## 1. Motivation

Live voice sessions are not a separate product — they are **first-class agentic episodes**
that produce durable, typed, auditable outputs.  The existing `mediator.conversations` tree
stores user utterances (`conversation_items`, `transcript_turns`), but after a live session
ends we have no structured record of what the agent *produced*: the prep brief it worked
from, the debrief it generated, any agenda revisions it made, or any summaries it wrote.

`mediator.conversation_artifacts` fills that gap.  Each artifact is an **immutable,
revision-tracked** output tied to a conversation.  `mediator.artifact_links` records
**provenance** — which conversation items or durable state rows were inspected, summarized,
or created during the artifact's generation.

Together these two tables turn a live session from an ephemeral audio stream into a
fully traceable agentic episode: you can follow a debrief artifact back to the transcript
turns it summarized, the observations it extracted, and the commitments it created.

## 2. The two tables

### 2.1 `mediator.conversation_artifacts`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid PK` | gen_random_uuid() |
| `conversation_id` | `uuid NOT NULL` | FK → conversations(id) ON DELETE CASCADE |
| `bot_id` | `text NOT NULL` | Unvalidated string (see §7) |
| `user_id` | `uuid NOT NULL` | FK → mediator.users(id) (see §8) |
| `artifact_type` | `text NOT NULL` | Closed allow-list (see §2.3) |
| `payload` | `jsonb NOT NULL` | Schema-free; caller-owned |
| `payload_version` | `int NOT NULL DEFAULT 1` | CHECK ≥ 1 |
| `revision_number` | `int NOT NULL DEFAULT 1` | CHECK ≥ 1; auto-incremented |
| `created_by_turn_id` | `uuid NULL` | FK → bot_turns(id) ON DELETE SET NULL |
| `deleted_at` | `timestamptz NULL` | Soft-delete |
| `expires_at` | `timestamptz NULL` | Optional TTL |
| `created_at` | `timestamptz NOT NULL` | DEFAULT now() |

Unique: `(conversation_id, artifact_type, revision_number)`.  
Index: `(conversation_id, artifact_type, revision_number DESC)` for current-artifact lookup.  
Partial index: `(conversation_id) WHERE deleted_at IS NULL`.

### 2.2 `mediator.artifact_links`

| Column | Type | Notes |
|---|---|---|
| `id` | `uuid PK` | gen_random_uuid() |
| `artifact_id` | `uuid NOT NULL` | FK → conversation_artifacts(id) ON DELETE CASCADE |
| `target_table` | `text NOT NULL` | Closed allow-list, unqualified (see §5) |
| `target_id` | `uuid NOT NULL` | Row in target_table |
| `relation` | `text NOT NULL` | Closed allow-list (see §2.3) |
| `evidence` | `jsonb` | Free-form; documented shape in Python module |
| `deleted_at` | `timestamptz NULL` | Soft-delete |
| `created_at` | `timestamptz NOT NULL` | DEFAULT now() |

Unique: `(artifact_id, target_table, target_id, relation)`.  
Index: `(target_table, target_id)` for reverse lookup.

### 2.3 Closed allow-lists

Adding a new value to any of these lists **requires a migration** because the
CHECK constraint is closed.  The Python constants in `app/services/live/artifacts.py`
must also be updated in lockstep (enforced by parity tests).

**`artifact_type`** — currently:

- `live_prep_brief`
- `live_debrief`
- `review_summary`
- `agenda_revision`
- `transcript_reflection`

**`relation`** — currently:

- `planned_item`
- `summarized_from`
- `evidence_quote`
- `extracted_memory`
- `extracted_observation`
- `extracted_distillation`
- `created_commitment`
- `logged_event`
- `created_follow_up`
- `updated_topic_status`

**`target_table`** — the 12 known durable/session tables (unqualified):

- `conversations`, `conversation_items`, `transcript_turns`, `conversation_notes`
- `messages`, `memories`, `observations`, `distillations`
- `commitments`, `events`, `scheduled_jobs`, `topic_status`

**Deliberately excluded from `target_table` allow-list:**

- `bot_turns` — provenance for the producing turn is stored via the
  `created_by_turn_id` FK on the artifact row itself, not as a link row.
  Placing it in `artifact_links` would create a redundant provenance path and
  confuse the `(target_table, target_id)` reverse-index contract.
- `pregnancy_state` — this table does not exist.  Pregnancy data lives as
  columns on `mediator.users` per migration `0032_pregnancy.sql`.

## 3. Immutability and the revision rule

Artifacts are **immutable after creation**.  If a prep brief needs to be regenerated
(e.g., after a steering change or a retry), do not UPDATE the existing row — call
`create_artifact` again.  The helper auto-increments `revision_number` for the same
`(conversation_id, artifact_type)` pair.

**The "current" artifact is the row with the highest `revision_number`**, not the
newest `created_at`.  This is enforced by `get_current_artifact()`:
```sql
WHERE conversation_id = $1 AND artifact_type = $2
  AND deleted_at IS NULL
ORDER BY revision_number DESC LIMIT 1
```

Callers may soft-delete individual revisions by setting `deleted_at`, but the
revision-number ordering ensures a coherent linear history even when revisions
are inserted out of timestamp order.

## 4. The `bot_turns.kind` convention — typed-column substitution for `trigger_metadata`

The original brief used `trigger_metadata.kind` on `bot_turns` to distinguish
live prep/debrief turns from normal chat turns.  The implementation substitutes
a **typed `kind text` column** with a closed CHECK constraint:

```sql
ALTER TABLE mediator.bot_turns ADD COLUMN kind text
    CHECK (kind IS NULL OR kind IN ('live_prep', 'live_debrief'));
```

**Rationale for the substitution:**

- `bot_turns` has no `trigger_metadata` column (confirmed by T1 audit).
- A typed CHECK-constrained column is queryable, indexable, and avoids the
  schema-later JSONB decode-for-filter pattern.
- The CHECK constraint is closed — adding a new kind (e.g., `live_followup`)
  requires a migration.  This is intentional: new turn kinds should be
  deliberate schema changes, not accidental runtime values.

**The two non-chat turn kinds are:**

| Kind | Meaning | Sprint wired |
|---|---|---|
| `live_prep` | Agentic prep turn (runs before the live session starts) | Sprint 2 |
| `live_debrief` | Agentic debrief turn (runs after the live session ends) | Sprint 3 |

**No-data window (Sprint 1):** The `kind` column exists on `bot_turns` but no
production code path populates it.  In Sprint 1, `SELECT * FROM mediator.bot_turns
WHERE kind IS NOT NULL` returns **zero rows** on every environment.  This is safe
because the column is nullable with no default and the existing `_open_turn` INSERT
(at `agentic.py:1382`) uses a hardcoded column list that does not include `kind`.

**Important:** Analytics or dashboard queries that filter by `kind` will see
zero rows until Sprint 2/3 respectively wires the column.  Do not interpret this
as a metric regression — it is an intentional no-data window.

## 5. Canonical form for `target_table`

`target_table` values are **unqualified, lowercase, logical identifiers**,
not schema-qualified table names:

```
conversations          (not mediator.conversations)
conversation_items     (not mediator.conversation_items)
...
```

**Why unqualified rather than schema-qualified:**

- The `mediator` schema is the logical namespace for these tables, but the
  schema itself is an environmental concern — the app discovers it through
  `SET LOCAL search_path` in `app/db.py:41`, not through hardcoded schema
  prefixes in migration SQL.
- If a future environment moves these tables to a different schema (or the
  schema is renamed), the unqualified identifiers remain correct — the
  schema binding is applied at connection time, not stored in data.
- This is consistent with the existing `commitments.bot_id` and
  `events.bot_id` convention (unvalidated text, not a regclass reference).
- The Python constant `ALLOWED_TARGET_TABLES` mirrors the exact same strings.
  Callers pass these strings verbatim; no normalization is performed.

## 6. Provenance — single canonical source for the producing turn

Two distinct provenance questions have two distinct answers:

**"Which bot_turn produced this artifact?"** → `conversation_artifacts.created_by_turn_id` FK.

**"Which rows did this artifact inspect, summarize, or create?"** → `artifact_links`.

`bot_turns` is **deliberately excluded** from the `artifact_links.target_table`
CHECK allow-list.  Including it would create a second provenance path for the
producing turn — one FK and one link row — with no canonical answer to "which is
the real one?"  The FK is the canonical path.

If a future caller needs to link an artifact to a *different* bot_turn (not the
producer), it should use the `created_by_turn_id` FK on a separate artifact
revision, or discuss extending the link model — but for v1, one FK is sufficient.

## 7. `bot_id` is intentionally an unvalidated string

`conversation_artifacts.bot_id` has no CHECK constraint and no FK to any bot
registry table.  This is consistent with the existing conventions for
`commitments.bot_id` and `events.bot_id` (both introduced in migration
`0038_commitments_events.sql`).  These tables also use unvalidated `text` for
`bot_id`.

The rationale: bot identities are code-registered (`app/bots/registry.py`), not
DB-registered.  Validating them at the Postgres level would require an enum or
FK that must be updated in lockstep with every new bot, creating deployment-order
coupling between code deploys and migration applies.  The app-layer validation
(at tool-registration time) is sufficient; the DB column is pure storage.

## 8. `user_id` rule for future callers

`conversation_artifacts.user_id` has a FK to `mediator.users(id)` and must
always equal `conversations.user_id` for the artifact's conversation.

**Why not `partner_user_id`?** Live sessions are always initiated by one user
(the conversation owner).  Attributing artifacts to the partner would create
ambiguity in RLS policies and in the owner-scoped read path.  If a future
feature allows the partner to explicitly author an artifact, a separate
`author_user_id` column should be added — but for v1, `user_id` always means
"the conversation owner."

This rule is enforced by caller convention, not by a DB constraint, because
Postgres cross-table CHECK constraints are limited.  The `create_artifact`
helper accepts `user_id` as a keyword argument; callers must pass
`conversation.user_id`.

## 9. Encryption at rest — deferred question

The brief's open question #1 asks:

> Should artifact payloads be encrypted at rest when they contain
> transcript-derived summaries, or is existing DB/storage policy sufficient?

**Answer for Sprint 1: defer.**  Existing DB/storage policy is sufficient
because:

- The `DATA_ENCRYPTION_KEY` mechanism described in [docs/SECURITY.md](./SECURITY.md)
  already covers the highest-sensitivity content fields (`messages.content`,
  `memories.content`, `observations.content`, `bot_turns.reasoning`, etc.).
- Artifact payloads are (by design) *derived summaries* — they contain what
  the agent decided was worth surfacing, not raw transcript text.  Any
  confidential raw content that was summarized is already covered by the
  encrypted columns on the source tables.
- Adding a `payload_encrypted` column would require a dual-write path
  (plaintext + ciphertext) plus a backfill migration, which is scope creep
  for Sprint 1.

If a future security review determines that artifact payloads regularly
contain verbatim transcript excerpts, add a `payload_encrypted` column
following the same pattern as `bot_turns.prompt_snapshot_encrypted` and
update [docs/SECURITY.md](./SECURITY.md) accordingly.

## 10. RLS posture

Both `conversation_artifacts` and `artifact_links` have:

- `ENABLE ROW LEVEL SECURITY` + `FORCE ROW LEVEL SECURITY`
- `REVOKE ALL FROM anon, authenticated`
- A `deny_anon_*` catch-all policy (`FOR ALL TO anon, authenticated USING (false)`)
- An `owner_scoped_*` policy using one-hop or two-hop EXISTS through `conversations`

**conversation_artifacts** — one-hop: `EXISTS (SELECT 1 FROM conversations c WHERE c.id = conversation_id AND (c.user_id = auth.uid() OR c.partner_user_id = auth.uid()))`.

**artifact_links** — two-hop: `EXISTS (SELECT 1 FROM conversation_artifacts a JOIN conversations c ON c.id = a.conversation_id WHERE a.id = artifact_id AND owner check)`.

All policies are wrapped in `DO $$ ... EXCEPTION WHEN duplicate_object THEN NULL` blocks,
matching the convention established in migration `0042_live_conversations.sql`.

Service-role connections (used by `app/db.py`) bypass RLS via Supabase's `BYPASSRLS`
attribute, so the helpers in `app/services/live/artifacts.py` are not affected.
The policies are defense-in-depth against a leaked anon/authenticated key.

## 11. `bot_turns.conversation_id` — the session link

`bot_turns.conversation_id` is a nullable FK to `mediator.conversations(id)`
with `ON DELETE SET NULL`.  It is populated **only** for live prep/debrief
turns and any future live-specific agentic turns.

A partial index `idx_bot_turns_conversation_id WHERE conversation_id IS NOT NULL`
makes queries like "find all bot_turns for this conversation" efficient without
penalizing the overwhelming majority of rows (normal chat turns) where the
column is NULL.

**Sprint 1 no-data window:** Like `kind`, `conversation_id` has zero populated
rows in Sprint 1.  The column is safe against production data because the
existing `_open_turn` INSERT has a hardcoded column list that omits it.

## 12. Soft-delete semantics for artifact links

`artifact_links.deleted_at` supports soft-delete.  When a link is tombstoned:

- `list_artifact_links` filters it out by default (`include_deleted=False`).
- `add_artifact_link` with the same `(artifact_id, target_table, target_id, relation)`
  does NOT return the tombstoned row — it inserts a **fresh row** (the UNIQUE
  constraint does not cover `deleted_at`, so a new row with a different `id` is clean).
- The follow-up SELECT in `add_artifact_link` explicitly filters `WHERE deleted_at IS NULL`
  to avoid returning tombstoned rows.

## 13. Migration summary

- **Forward**: `migrations/0051_conversation_artifacts.sql` (194 lines).
  Creates both tables + CHECK constraints + indexes + RLS policies + bot_turns
  columns.  Wrapped in `BEGIN/COMMIT`.
- **Reverse**: `migrations/0051_conversation_artifacts.down.sql` (46 lines).
  Drops policies → tables → indexes → columns in correct dependency order.
  Every DROP uses `IF EXISTS`.  Wrapped in `BEGIN/COMMIT`.
- **Round-trip**: apply → down → re-apply is tested in the DB-gated test suite.

## 14. Config surface

Two new Settings fields (Sprint 1 surface-only; consumed by Sprint 2/3):

| Field | Default | Bounds | Purpose |
|---|---|---|---|
| `nonchat_default_max_tool_iterations` | 100 | 0–2000 | Default cap for non-chat agentic turns |
| `live_debrief_max_tool_iterations` | 500 | 0–5000 | Cap for live debrief turns specifically |

Both are validated by Pydantic on startup; values outside bounds raise `ValidationError`.
Neither is wired into `agentic.py` yet — they exist for future callers to import
from `app.config.get_settings()`.
