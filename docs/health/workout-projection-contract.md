# Workout Projection Contract — Withings → Commitment Auto-Completion

Status: settled (post-implementation)
Last updated: 2026-07-22
Audience: downstream consumers (M3 tool surfaces, M4 hot-context rendering), future maintainers

This document records the implementation contract for the workout auto-projection
pipeline: the deterministic chain from a Withings workout record to an optional
projection-owned `mediator.events` row that satisfies a Hector fitness commitment.
Every decision below is the settled North Star — changes require explicit
cross-team coordination.

---

## 1. Architecture Summary

```
Withings API (GET /v2/measure?action=getworkouts)
       │
       ▼
  health_source_records (provider-shaped, soft-delete audit trail)
       │
       ▼
  normalize_workout()  ──►  health_normalized_workouts
       │
       ▼
  project_workout()  (pure matcher, no I/O)
       │
       ▼
  apply_workout_projection()  (stateful applicator)
       │
       ├── [feature off]      → no-op
       ├── [first-time match] → 1 event + 1 ledger row
       ├── [retry/replay]     → return existing active row
       ├── [revision]         → supersede old, create new (if match)
       └── [tombstone]        → remove projection, delete owned event
```

Key components:

| Module | Role |
|:---|---|
| `app/services/health_sync/models.py` | `NormalizedWorkout`, `WithingsWorkoutCategory`, `WITHINGS_WORKOUT_TAXONOMY`, `HECTOR_FITNESS_TAXONOMY_LABELS` |
| `app/services/health_sync/normalization.py` | `normalize_workout()`, `resolve_workout_type()` |
| `app/services/health_sync/workout_projection.py` | `project_workout()` — pure deterministic matcher |
| `app/services/health_sync/projection_applicator.py` | `apply_workout_projection()` — stateful applicator |
| `app/services/health_sync/repository.py` | Ledger primitives: `find_active_projection`, `insert_projection`, `supersede_projection`, `remove_projection`, `create_projection_event`, `delete_projection_event`, `detach_projection_event`, `find_projection_by_event` |
| `app/services/health_sync/read_models.py` | `get_recent_workouts()`, `get_weekly_workout_summary()` |
| `app/services/hot_context_solo.py` | `_build_workout_summary_block()` — compact Hector context |
| `app/services/adherence.py` | Canonical adherence computation (type-safe) |
| `app/services/commitments.py` | Compatibility shim (delegates to `adherence.py`) |
| `migrations/0065_workout_projection_contract.sql` | Ledger schema upgrade |

---

## 2. Withings-to-Hector Taxonomy

### 2.1 Category Enumeration

`WithingsWorkoutCategory` in `models.py` defines 53 canonical Withings category
constants (1–52 plus 999 for OTHER). These match the Withings API v2 `getworkouts`
response `category` field.

### 2.2 Taxonomy Mapping

`WITHINGS_WORKOUT_TAXONOMY` maps 52 Withings category integers to Hector taxonomy
labels. Key design choices:

- **Many-to-one compression**: Several Withings categories collapse to the same
  Hector label. For example, `LIFT_WEIGHTS` (16) and `CALISTHENICS` (17) both
  map to `"strength"`; `BMX` (5) and `BICYCLING` (6) both map to `"cycling"`;
  `PING_PONG` (45) maps to `"table_tennis"`; `ROCK_CLIMBING` (47) maps to
  `"climbing"`.
- **Unmapped categories**: Any integer not in the map (including 999/OTHER)
  resolves to `"unknown"`. The normalizer never estimates a type from other
  fields.
- **Missing category**: When the provider omits the `category` field entirely,
  `resolve_workout_type(None)` returns `"unknown"`.
- **Broadcast labels**: `HECTOR_FITNESS_TAXONOMY_LABELS` is a `frozenset` of 24
  labels that the projection matcher considers compatible with Hector fitness
  commitments. Types outside this set (e.g., `"surfing"`, `"bodyboard"`,
  `"sailing"`) are normalized and persisted but never eligible for projection.

Full taxonomy:

| Withings category | Hector label | Projection-eligible |
|:---|---:|:---|
| 1 Walk | walking | Yes |
| 2 Run | running | Yes |
| 3 Hiking | hiking | Yes |
| 4 Skating | skating | Yes |
| 5 BMX | cycling | Yes |
| 6 Bicycling | cycling | Yes |
| 7 Swimming | swimming | Yes |
| 8 Surfing | surfing | No |
| 9 Kitesurfing | kitesurfing | No |
| 10 Windsurfing | windsurfing | No |
| 11 Bodyboard | bodyboard | No |
| 12 Tennis | tennis | Yes |
| 13 Table tennis | table_tennis | No |
| 14 Squash | squash | No |
| 15 Badminton | badminton | No |
| 16 Lift weights | strength | Yes |
| 17 Calisthenics | strength | Yes |
| 18 Elliptical | elliptical | Yes |
| 19 Pilates | pilates | Yes |
| 20 Basketball | basketball | Yes |
| 21 Soccer | soccer | Yes |
| 22 Football | football | No |
| 23 Rugby | rugby | No |
| 24 Volleyball | volleyball | No |
| 25 Water polo | waterpolo | No |
| 26 Horse riding | horse_riding | No |
| 27 Golf | golf | Yes |
| 28 Yoga | yoga | Yes |
| 29 Dancing | dancing | Yes |
| 30 Boxing | boxing | Yes |
| 31 Fencing | fencing | No |
| 32 Wrestling | wrestling | No |
| 33 Martial arts | martial_arts | Yes |
| 34 Skiing | skiing | Yes |
| 35 Snowboarding | snowboarding | Yes |
| 36 Ice hockey | ice_hockey | No |
| 37 Climbing | climbing | Yes |
| 38 Ice skating | ice_skating | Yes |
| 39 Multisport | multisport | Yes |
| 40 Rowing | rowing | Yes |
| 41 Zumba | zumba | No |
| 42 Baseball | baseball | No |
| 43 Handball | handball | No |
| 44 Hockey | hockey | No |
| 45 Ping pong | table_tennis | No |
| 46 Riding | horse_riding | No |
| 47 Rock climbing | climbing | Yes |
| 48 Sailing | sailing | No |
| 49 Ski touring | skiing | Yes |
| 50 Snowshoeing | snowshoeing | No |
| 51 Stand-up paddle | stand_up_paddle | No |
| 52 Triathlon | triathlon | Yes |
| 999 / unmapped | unknown | No (rejected before slot check) |
| None (missing) | unknown | No |

---

## 3. Normalized Workout Shape

`NormalizedWorkout` is the pure domain dataclass produced by `normalize_workout()`.
It lives in `app/services/health_sync/models.py` lines 688–731.

### 3.1 Mandatory Fields

| Field | Type | Derivation |
|:---|---:|---:|
| `started_at` | `datetime` (UTC) | From `record.starts_at` |
| `workout_type` | `str` | From `WITHINGS_WORKOUT_TAXONOMY`; defaults to `"unknown"` |

### 3.2 Optional (Nullable) Fields

| Field | Type | Nullable behavior |
|:---|---:|---:|
| `ended_at` | `datetime \| None` | `None` when provider omits end time |
| `local_date` | `date \| None` | Derived from `started_at` in source timezone; falls back to UTC date of `started_at` |
| `local_timezone` | `str \| None` | Propagated from source record |
| `local_offset_seconds` | `int \| None` | DST-aware offset at `started_at`; fallback via `calculate_offset_seconds()` |
| `duration_seconds` | `int \| None` | From `source_metadata.data.duration`, or computed from `ended_at - started_at` when both are present |
| `pause_duration_seconds` | `int \| None` | From `source_metadata.data.pause_duration` |
| `distance_meters` | `float \| None` | From `source_metadata.data.distance` |
| `steps` | `int \| None` | From `source_metadata.data.steps` |
| `energy_kcal` | `float \| None` | From `source_metadata.data.calories` |
| `elevation_gain_meters` | `float \| None` | From `source_metadata.data.elevation` |
| `average_heart_rate_bpm` | `float \| None` | From `source_metadata.data.hr_average` |
| `max_heart_rate_bpm` | `float \| None` | From `source_metadata.data.hr_max` |
| `source_device_id` | `str \| None` | From source record |
| `source_device_model` | `str \| None` | From source record |

**The normalizer never estimates missing fields.** Absent optional metrics are
propagated as `None` — no interpolation, no defaults.

### 3.3 Attribution

Every `NormalizedWorkout` carries an `attribution` dict that includes:

```json
{
  "revision_count": 1,
  "provider_category": 2,
  ...
}
```

- `revision_count` is threaded through from the persisted source record.
- `provider_category` is the raw Withings category integer.
- Any other fields from `record.attribution` are shallow-copied.

---

## 4. Local Date Decision

### 4.1 Derivation Rule

`local_date` is derived from the **workout start time** (`started_at`) converted
to the source timezone:

```
local_start = started_at.astimezone(source_timezone)
local_date = local_start.date()
```

This means a workout that starts at 23:30 UTC on Monday in America/New_York
(UTC-5 winter → 18:30 Monday) is assigned to Monday. A workout that starts at
00:30 UTC on Tuesday in America/New_York (19:30 Monday EST) is assigned to
Monday — because the local start time is still Monday.

### 4.2 DST Handling

The `local_date` is calculated using the **actual DST-aware offset** at the
workout start time, not a fixed offset:

- **Spring forward** (e.g., 02:30 AM March 10 → 03:30 AM EDT after skipping
  02:00): The offset at `started_at` is UTC-4 (EDT), and the date is March 10.
- **Fall back** (e.g., 01:30 AM November 4 → 01:30 AM EST observed twice): The
  offset at `started_at` is UTC-5 (EST), and the date is November 4. The
  `local_offset_seconds` column stores the DST-resolved offset at start time.

### 4.3 Fallback

When the source timezone cannot be resolved (invalid IANA name, missing, or
whitespace-only), `local_date` falls back to the UTC date of `started_at`.

### 4.4 Why Not End Time

Unlike sleep (which uses wake/end time for `local_sleep_date`), workouts use
start time because:

- Workouts are typically single-session activities with a clear start.
- The start time determines which day the user *intended* to exercise.
- Using end time would misclassify late-evening weekday workouts that cross
  midnight into the next day.

---

## 5. Deterministic Matching Rules

### 5.1 Matcher Contract

`project_workout()` in `workout_projection.py` is a **pure, deterministic**
function with no database I/O. It accepts:

1. A `NormalizedWorkout`
2. A list of active commitment dicts (caller is expected to pre-filter to
   `status='active'`)
3. Optional `user_timezone` (forward-compatibility, not currently used)
4. Optional `projection_version` (forward-compatibility, not currently used)

### 5.2 Decision Pipeline

The matcher applies these guards in order:

```
1. local_date is None?                              → no_local_date
2. workout_type not in HECTOR_FITNESS_TAXONOMY_LABELS? → unknown_workout_type
3. no commitments provided?                          → zero_active_commitments
4. filter to bot_id='hector' AND topic_slug='fitness'
5. no Hector fitness commitments?                    → no_hector_fitness_commitments
6. find commitments where local_date is eligible slot
7. zero eligible?                                    → no_eligible_slot
8. more than one eligible?                           → ambiguous_multiple_commitments
9. exactly one eligible                              → matched
```

### 5.3 Slot Eligibility

Slot eligibility mirrors `app.services.commitments.compute_slots` cadence
semantics but only tests membership for a single date (no full slot generation).
Supported cadences:

| Cadence | Eligibility Rule |
|:---|---:|
| `daily` | Any date within `[start_date, end_date]` (unbounded if no start) |
| `weekdays` | Mon–Fri within the ISO week of `local_date`, bounded by commitment `start_date`/`end_date` |
| `weekly_count` | Any day in the ISO week containing `local_date`, bounded by commitment start/end |
| `custom_days` | Day-of-week in `days_of_week` list, bounded by commitment start/end within the ISO week |
| `custom` | Any date within `[start_date, end_date]` (unbounded if no start) |
| Unknown cadence | Conservative: never matches |

### 5.4 Decision Reason Constants

Every `ProjectionDecision` carries a stable, queryable `reason` string (never
a free-form message):

| Constant | Meaning |
|:---|---:|
| `matched` | Exactly one compatible commitment with an eligible slot |
| `no_local_date` | Workout has no `local_date` (should not happen in practice) |
| `unknown_workout_type` | Workout type not in Hector fitness taxonomy |
| `zero_active_commitments` | No commitments provided |
| `no_hector_fitness_commitments` | Provided commitments exist but none are Hector fitness |
| `wrong_bot_or_topic` | Filtered out non-Hector or non-fitness commitments |
| `no_eligible_slot` | Hector fitness commitments exist but none have an eligible slot on this date |
| `ambiguous_multiple_commitments` | More than one commitment has an eligible slot |

---

## 6. Duplicate-Link Tolerance

### 6.1 Idempotent Replay

When `apply_workout_projection()` is called with the same `projection_version`
and the same source record already has an active projection with that version,
the applicator returns the existing `HealthProjectionRecord` without creating
any new ledger row or event.

### 6.2 Concurrent Replay Defense

The applicator uses `find_active_projection(..., for_update=True)` which sets
`SELECT ... FOR UPDATE` on the active ledger row (when using a real connection)
to serialize concurrent replays within a transaction. The `FakePool` simulates
this by tracking locked rows.

### 6.3 Partial Unique Index

Migration 0065 adds a partial unique index:

```sql
CREATE UNIQUE INDEX idx_health_source_to_event_projections_active_source
    ON mediator.health_source_to_event_projections (source_record_id)
    WHERE projection_status IN ('pending', 'projected');
```

This enforces at most one active (pending or projected) projection per source
workout at the database level. Superseded and removed projections can coexist
archivally.

### 6.4 Composite UNIQUE

The composite `UNIQUE (source_record_id, projection_version)` constraint
replaces the old single-column `UNIQUE (source_record_id)`, allowing the ledger
to retain version history while keeping each version unique per source record.

---

## 7. Reversal and Supersession Behavior

### 7.1 Revision Path

When a workout source record is revised (content changes, `revision_count`
increments), the sync calls `apply_workout_projection()` with a higher
`projection_version`. The applicator:

1. Supersedes the old projection row (sets `projection_status = 'superseded'`,
   records `superseded_at`).
2. **Detaches** the event link from the superseded row (`event_id = NULL`) so
   future lookups don't see a stale link.
3. **Deletes** the old projection-owned event from `mediator.events` (verified
   via `find_projection_by_event` ownership guard).
4. Runs the pure matcher on the revised workout.
5. If matched: creates a new event and new projection row with
   `supersedes_projection_id` pointing to the old row, forming an auditable
   chain.
6. If not matched: no new event is created; the superseded projection remains
   as an archival record.

### 7.2 Rematch Path (Same Version, Different Match)

When a previously unmatched workout becomes matchable (e.g., a commitment is
added retroactively), the caller can trigger a rematch by calling with a higher
`projection_version`. The behavior is identical to the revision path — old
projection is superseded, old event removed, new match attempted.

### 7.3 Tombstone Path

When a workout source record is deleted (tombstone arrives):

1. `apply_workout_projection(is_tombstone=True)` is called.
2. If an active projection exists:
   a. The projection-owned event is deleted (ownership verified).
   b. The projection is `remove`d (`projection_status = 'removed'`,
      `removed_at` set).
3. No new event is created.
4. Manual `log_event` testimony is **never** touched — only events linked via
   the projection ledger are candidates for deletion.

### 7.4 Ownership Guard

`find_projection_by_event(event_id)` returns the ledger row linked to that
event. Manual events (created by `log_event` or other code paths that do not
use the projection ledger) have no matching projection row, so:

- `find_projection_by_event` returns `None` for manual events.
- `_cleanup_projection_event` skips deletion when `owned is None`.
- This guarantees the applicator never mutates manual testimony.

### 7.5 Projection Status State Machine

```
             ┌──────────────────────────┐
             │         pending           │
             └──────────┬───────────────┘
                        │
                        ▼
             ┌──────────────────────────┐
     ┌───────│        projected         │◄────── active
     │       └──────────┬───────────────┘
     │                  │
     │                  ├── supersede() ────► ┌──────────────┐
     │                  │                      │  superseded  │  (archival)
     │                  │                      └──────────────┘
     │                  │
     │                  └── remove() ────────► ┌──────────────┐
     │                                         │   removed    │  (tombstone)
     │                                         └──────────────┘
     │
     └── retry/replay ──► return existing (no transition)
```

Only `pending` and `projected` are considered "active" by the partial unique
index and by `find_active_projection`.

---

## 8. Type-Safety Invariants

### 8.1 Adherence Classification

The canonical adherence implementation in `app/services/adherence.py` enforces
the following type-safety invariant (established in Step 1):

> Only events with an explicit `adherence_status` in `('done', 'missed', 'excused')`
> **AND** a matching `commitment_id` can classify a slot. Numeric-only, weight,
> sleep, or generic measurement events can NEVER satisfy a commitment.

This means:
- A `metric_key='workout'`, `adherence_status='done'` event created by the
  projection applicator **can** satisfy a fitness commitment (it has both
  `adherence_status` and the correct `commitment_id`).
- A weight measurement event (no adherence_status) **cannot** satisfy a fitness
  commitment, even if it has `value_numeric`.
- A sleep event **cannot** satisfy a fitness commitment.
- An adherence event with `commitment_id` pointing to a different commitment
  **cannot** satisfy this commitment.

### 8.2 Event Shape

Projection-owned events always have:

| Field | Value |
|:---|---:|
| `metric_key` | `"workout"` |
| `adherence_status` | `"done"` |
| `commitment_id` | UUID of the matched commitment |
| `observed_at` | `workout.started_at` (the workout start time in UTC) |
| `note` | `"Auto-projected from Withings workout"` |
| `bot_id` | `"hector"` |

The `topic_id` is resolved from the matched commitment's `topic_id` field.

### 8.3 Consumer Invariant

Every consumer of adherence data (`compute_adherence`, `summarize_board`,
`read_tools.py`, `hot_context_solo.py`) relies on the type-safety invariant.
The `commitments.py` module is an explicit compatibility shim that delegates to
`adherence.py` — no separate classification logic exists.

---

## 9. Default-Off Projection Flag

### 9.1 Config

`health_workout_projection_enabled: bool = False` in `app/config.py` (line 55).

This is a top-level setting alongside the other health sync flags:

```python
health_sync_enabled: bool = False
health_sync_workouts_enabled: bool = False
health_workout_projection_enabled: bool = False
```

### 9.2 No-Op Contract

When `enabled=False` is passed to `apply_workout_projection()`:

- The function returns `None` immediately.
- Zero database writes occur.
- The pure matcher is never invoked.
- The ledger and event tables are never read for this source record.

This means the projection pipeline is **completely inert** until an operator
sets the flag to `True`. No projection ledger rows or projection-owned events
are created while the flag is off.

### 9.3 Sync Integration

The projection call in `sync.py` (wired in Step 8 of the plan) checks the flag
from settings and passes `enabled=settings.health_workout_projection_enabled`.
When disabled, the entire projection branch is skipped during workout sync.

---

## 10. Queryable Unmatched and Ambiguous Workouts

### 10.1 Projection State Resolution

`_resolve_projection_state()` in `read_models.py` derives a compact projection
state from the ledger rows for a source record (sorted by `projection_version
DESC`):

| Status | Condition | Meaning |
|:---|---:|---:|
| `projected` | Active row with `reason='matched'` and non-null `event_id` | Successfully projected to a commitment |
| `unmatched` | Active row with non-matching reason | Workout type not eligible, no eligible slot, no Hector commitments, etc. |
| `ambiguous` | Active row with `reason='ambiguous_multiple_commitments'` | More than one commitment had an eligible slot |
| `removed` | Most recent row has `projection_status='removed'` | Source workout was deleted (tombstone) |
| `duplicate_linked` | More than one projection version exists (superseded chain) | Revision/rematch history present |
| `none` | No projection row exists at all | Projection never attempted or feature was off |

### 10.2 Query Details

`_resolve_projection_state` checks in order:

1. **Removed** — if `projection_status == 'removed'`, status is `removed`.
2. **Duplicate-linked** — if `len(projections) > 1`, status is `duplicate_linked`
   (superseded chain detected).
3. **Active** — if `status in ('pending', 'projected')`:
   - If `reason == 'matched'` and `event_id` is non-null → `projected`
   - If `reason == 'ambiguous_multiple_commitments'` → `ambiguous`
   - Any other reason → `unmatched`
4. **Superseded single** — if `status == 'superseded'` with only one version →
   `none` (no active projection supersedes it).
5. **Fallback** — `none`.

### 10.3 Batch Fetching

Both `get_recent_workouts()` and `get_weekly_workout_summary()` batch-fetch
projections for all source records in a single query:

```sql
SELECT source_record_id, id, event_id, commitment_id,
       projection_version, projection_status, decision_reason,
       supersedes_projection_id
FROM mediator.health_source_to_event_projections
WHERE source_record_id = ANY($1::uuid[])
  AND user_id = $2
ORDER BY source_record_id, projection_version DESC
```

This avoids N+1 queries. The result set is grouped by `source_record_id` in
application code, and the most recent version drives the state resolution.

### 10.4 Weekly Summary Aggregation

`WeeklyWorkoutDaySummary` includes `projected_count` — the number of workouts
on that date with `projection.status == 'projected'`. This is computed at query
time by counting workouts whose resolved projection state is `projected`.

---

## 11. Privacy Boundaries

### 11.1 User Scoping

Every query in `read_models.py` includes `WHERE user_id = $1`. No query can
accidentally cross user boundaries. The projection ledger also stores
`user_id` and all repository methods scope by `user_id`.

### 11.2 Tool-Level Access Control

The `get_workout_summary` tool in `read_tools.py` uses `_check_health_read_scope()`
with the same four gates as weight and sleep:

| Gate | Requirement |
|:---|---:|
| `ctx.bot_id` | Must be `"hector"` or `"habits"` |
| `ctx.primary_topic_slug` | Must be `"fitness"` or `"habits"` |
| `ctx.primary_topic_id` | Must not be `None` |
| `ctx.user.id` | Must not be `None` |

### 11.3 Hot Context Privacy

`_build_workout_summary_block()` in `hot_context_solo.py` omits:

- Raw payloads, tokens, and access tokens
- Heart-rate detail (average/max BPM)
- Device IDs and device models
- Partner-visible sharing or connection metadata
- Any language implying imported workouts create commitments or that Hector can
  infer missed/excused adherence

Output format is compact per-day summaries:
```
Recent workouts (7d):
  2026-07-19: 2 workout(s) — running, strength, 45min total (1 projected)
  2026-07-20: 1 workout(s) — cycling (1 projected)
```

### 11.4 Ownership Isolation

The projection ledger (`health_source_to_event_projections`) is separate from
manual events. The applicator only touches events that it can verify as
projection-owned via `find_projection_by_event()`. Manual `log_event` testimony
is immutable from the projection pipeline's perspective.

---

## 12. Migration and Schema Notes

### 12.1 Migration 0065 (`workout_projection_contract`)

The upgrade changes `mediator.health_source_to_event_projections`:

1. **Drops** the old `UNIQUE (source_record_id)` constraint.
2. **Adds** `UNIQUE (source_record_id, projection_version)` — versioned ledger.
3. **Adds** partial unique index `idx_health_source_to_event_projections_active_source`
   on `(source_record_id)` WHERE `projection_status IN ('pending', 'projected')`.
4. **Adds** `decision_reason TEXT` column — queryable rationale.
5. **Adds** `matched_local_date DATE` column — matched commitment-slot date.
6. **Adds** `supersedes_projection_id UUID` self-referencing FK — revision chain.
7. **Adds** index on `supersedes_projection_id` for audit queries.

All FK cascades, RLS policies (deny_anon + owner_scoped), CHECK constraints,
and NOT NULL defaults from migration 0063 are preserved.

### 12.2 Down Migration

The `.down.sql` reverses all seven changes, restoring the old `UNIQUE (source_record_id)`
constraint and dropping the added columns and indexes.

---

## 13. Test Coverage Summary

| File | Tests | Coverage |
|:---|---:|---:|
| `tests/test_health_workout_normalization.py` | 107 | Normalization, taxonomy, DST, revisions, tombstones |
| `tests/test_workout_projection.py` | 59 | Pure matcher: one-candidate, zero, ambiguous, DST, wrong type, wrong user, wrong bot/topic |
| `tests/test_projection_applicator.py` | 42+ | Applicator: default-off, first-time, retry, concurrent, revision, rematch, tombstone, ownership |
| `tests/test_health_workout_projection.py` | 42 | Repository/FakePool: projection transactions, idempotency, supersession, manual-event isolation |
| `tests/test_health_read_models.py` | 6+ | Privacy boundaries: cross-user isolation, empty results, projection state |
| `tests/test_hector_tools.py` | 13+ | Tool handler: empty/populated/projected data, coach rejection, wrong topic |
| `tests/test_hot_context_hector.py` | 12 | Hot context: omission without data, compact summaries, privacy exclusions |
| `tests/test_hector_hot_context.py` | 10 | Snapshot rendering: render paths, combined blocks |
| `tests/test_hector_prompt.py` | 10 | Prompt slot registration |
| `tests/test_adherence.py` | 6+ | Type-safety: weight/sleep/numeric events cannot satisfy workout commitments |
| `tests/test_commitments.py` | 9+ | Shim parity: shim matches canonical behavior |

---

## 14. Handoff Checklist

Before closing future milestone work that interacts with workout projections,
verify that it still preserves:

- [ ] `health_workout_projection_enabled` defaults to `False`
- [ ] Projection-owned events always have `metric_key='workout'`,
      `adherence_status='done'`, and a matched `commitment_id`
- [ ] Manual `log_event` testimony is never mutated by projection code
- [ ] At most one active projection per source record (database-enforced)
- [ ] Revision supersession detaches and deletes old projection-owned events
- [ ] Tombstone reversal removes projections and deletes owned events
- [ ] Read queries batch-fetch projections (no N+1)
- [ ] All read queries are scoped by `user_id`
- [ ] Hot context omits heart-rate detail, device IDs, tokens, raw payloads,
      partner sharing, and commitment-creation language
- [ ] `_check_health_read_scope()` gates workout tools like weight/sleep
- [ ] `commitments.py` is a compatibility shim; `adherence.py` is canonical
- [ ] Type-safety: only events with explicit `adherence_status` and matching
      `commitment_id` can classify slots
