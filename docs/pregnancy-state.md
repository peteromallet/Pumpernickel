# Pregnancy state — per-feature schema (Tante Rosi)

Status: implemented (Phase 1, Sprint 7).
Last updated: 2026-05-12.

## Why per-feature instead of generic `user_journeys`

A prior design exploration (`docs/longitudinal-state.md`) proposed a generic
`user_journeys` table that could accommodate pregnancy, fitness, weight loss,
sobriety, and other longitudinal tracking use cases. That doc was **rejected
for this sprint** after review.

Rationale for per-feature:

1. **Zero production examples.** The generic abstraction was designed against
   speculation about three hypothetical bots, not one real one. The first
   real bot (Tante Rosi) needs to ship now, and per-feature columns on
   `mediator.users` are the fastest path to value.

2. **Schema simplicity.** Eight nullable `pregnancy_*` columns on the users
   table are trivially additive, require no JOINs, and surface directly in
   existing User hydration paths (`_row_to_user`, `fetch_user_by_id`,
   `upsert_user`).

3. **Safety and clarity.** Pregnancy state is special-category health data
   (GDPR Art. 9). Putting it in a generic `user_journeys` table would
   require a visibility system, cross-bot read rules, and sensitive-content
   handling that are unnecessary for a single-bot deployment. Per-feature
   columns keep the data surface obvious and locally auditable.

4. **Extraction path.** If 3+ bots show genuine pattern overlap (e.g. a
   fitness bot + weight-loss bot + sobriety bot all want the same anchors →
   tracks → current_state flow), we extract a shared abstraction then.
   Until then, per-feature schemas prevent premature generalization.

## What's implemented

### Schema (`migrations/0032_pregnancy.sql`)

Eight nullable columns on `mediator.users`:

| Column | Type | Notes |
|---|---|---|
| `pregnancy_edd` | `date` | Estimated due date — canonical anchor for gestational age. |
| `pregnancy_dating_basis` | `text` | `'lmp'` or `'scan'` — how EDD was determined. |
| `pregnancy_lmp_date` | `date` | Provenance field (last menstrual period). |
| `pregnancy_scan_date` | `date` | Set when dating is revised by scan. |
| `pregnancy_scan_corrected_at` | `timestamptz` | When EDD was last corrected. |
| `pregnancy_started_at` | `timestamptz` | When the pregnancy tracking began. |
| `pregnancy_ended_at` | `timestamptz` | When the pregnancy concluded. |
| `pregnancy_outcome` | `text` | `'birth'`, `'loss'`, or `'termination'`. |

Two CHECK constraints:
- `dating_basis_requires_edd`: both EDD and dating_basis are set together or
  neither.
- `outcome_requires_ended_at`: both outcome and ended_at are set together or
  neither.

Partial index `idx_users_active_pregnancy` for "find active pregnancies"
queries.

### Topic (`migrations/0033_pregnancy_topic.sql`)

Inserts `(pregnancy, Pregnancy)` into `mediator.topics`.

### Helper module (`app/services/pregnancy.py`)

Pure functions (no DB calls):
- `gestational_age(edd, today=None)` → `(weeks, days)`
- `trimester(weeks)` → `'first' | 'second' | 'third'`
- `is_pregnancy_active(user)` → `bool`
- `days_since_loss(user, today=None)` → `int | None`
- `format_pregnancy_state(user, today=None)` → `str | None`

### Write tools

Three tools registered in `TOOL_DISPATCH`, gated by allowlist:
- `set_pregnancy_edd` — initial capture
- `correct_pregnancy_edd` — mid-course correction (e.g. dating scan)
- `end_pregnancy` — close with outcome

### Hot context

- **Solo path** (`hot_context_solo.py`): renders `## Pregnancy` block when
  `bot_id == 'tante_rosi'` and state is present.
- **Dyad path** (`hot_context.py`): one-line `## Partner state` summary for
  mediator-side awareness (no symptom/themes/weight auto-bridging).

### Allowlist

Tante Rosi's tool_allowlist: full dispatch minus 8 dyad-only tools, plus
the 3 pregnancy tools. Coach and mediator allowlists exclude pregnancy
tools. All 5 bridge/escalate tools are excluded from Rosi (§4.1 guarantee).

## Future extraction

If 3+ bots show genuine pattern overlap in longitudinal state tracking,
extract a shared abstraction. The design exploration in
`docs/longitudinal-state.md` (rejected 2026-05-12) serves as a reference
for that extraction. Revisit when:
- A fitness bot is added
- A weight-loss bot is added
- A sobriety tracker is added
- Any other bot needing time-anchored numeric state tracking