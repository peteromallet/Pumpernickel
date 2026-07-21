# Weight and Sleep Read Models — Implementation Contract

Status: settled (post-implementation)
Last updated: 2026-07-21
Audience: downstream consumers (M3 tool surfaces, M4 hot-context rendering), future maintainers

This document records the implementation contract for the weight measurement and
sleep summary read models built in `app/services/health_sync/`. It covers the
normalization pipeline, database tables, query service, and the guardrails that
M3/M4 consumers must respect. This is the single source of truth for how
Withings raw data becomes queryable derived values.

---

## 1. Architecture Summary

```
Withings API (GET /measure, GET /v2/sleep)
       │
       ▼
  health_source_records (provider-shaped, soft-delete audit trail)
       │
       ├── normalize_measure_group()  ──► health_normalized_measurements
       └── normalize_sleep_summary()  ──► health_normalized_sleep
                                                  │
                                                  ▼
                                     read_models.py (query service)
                                                  │
                                                  ▼
                               read_tools.py (get_weight_trend, get_sleep_summary)
```

- **health_source_records** stores the raw provider record, including `is_deleted`
  and `deleted_at` for audit. Raw provider payloads are never persisted.
- **health_normalized_measurements** holds one row per `(source_record_id, metric)`.
- **health_normalized_sleep** holds one row per sleep summary source record.
- The **query service** (`read_models.py`) is the only read path. No caller reads
  the normalized tables directly.

---

## 2. Measurement Decoding

### 2.1 Withings Exponent Decoding

Withings sends integer measure values with a signed exponent unit:

```
real_value = value × 10^unit
```

Examples from the golden fixture `measurements_page_1.json`:

| Withings type | Raw `value` | `unit` | Decoded value | Canonical metric |
|:---|:---|:---|:---|---:|
| 1 (weight) | 70540 | -3 | 70.54 kg | `weight` |
| 6 (fat ratio) | 212 | -1 | 21.2 % | `fat_ratio` |
| 76 (muscle mass) | 14980 | -3 | 14.98 kg | `muscle_mass` |

The decoder is `decode_withings_value(value, unit)` in `normalization.py`. It
returns a `float` with no rounding — callers that need decimal precision should
round at the persistence or presentation layer.

### 2.2 Absence Semantics

| Condition | Behavior |
|:---|:---|
| Measure type not in `WITHINGS_METRIC_MAP` | Silently skipped — no normalized row |
| Measure entry missing `value` or `unit` | `KeyError` raised (required fields) |
| `unit` key absent in entry | Defaults to `0` |

---

## 3. Metric Map

Only measure types in the default fetch set are mapped:

| Withings Type | Canonical Metric | Canonical Unit |
|:---|---:|:---|
| 1 | `weight` | `kg` |
| 6 | `fat_ratio` | `percent` |
| 8 | `fat_mass` | `kg` |
| 76 | `muscle_mass` | `kg` |
| 88 | `bone_mass` | `kg` |

Types not in this map (e.g., 9 diastolic BP, 10 systolic BP, 11 heart rate,
54 SpO₂) produce no normalized row — they are known but excluded from the
default meastypes fetch set and the metric map.

---

## 4. Canonical Units

| Unit | Meaning | Applies to |
|:---|:---|:---|
| `kg` | Kilograms | weight, fat_mass, muscle_mass, bone_mass |
| `percent` | Percentage (0–100 scale) | fat_ratio |

All normalized measurement rows store both `canonical_unit` and `source_unit`
(which mirrors `canonical_unit` after normalization). Future metrics that use
different units (e.g., mmHg for blood pressure) would add entries to the map
with their own canonical unit strings.

Sleep duration fields are stored as integer seconds in the normalized table and
converted to decimal hours at the presentation layer (1 decimal place).

---

## 5. Null And Optional Field Semantics

### 5.1 Measurement Fields

| Column | Nullable | Behavior when null |
|:---|:---|:---|
| `source_timezone` | Yes | Propagated as `None`; no fallback |
| `source_offset_seconds` | Yes | Propagated as `None`; no fallback |
| `source_device_id` | Yes | Propagated as `None` |
| `source_device_model` | Yes | Propagated as `None` |
| `attribution` | No | Always `{}` (empty dict) when absent |

### 5.2 Sleep Fields

| Column | Nullable | Behavior when null |
|:---|:---|:---|
| `local_timezone` | Yes | `None`; `local_sleep_date` falls back to UTC date of `ended_at` |
| `local_offset_seconds` | Yes | `None`; attempted via `calculate_offset_seconds()` |
| `total_in_bed_seconds` | Yes | `None` (missing data field) |
| `total_asleep_seconds` | Yes | `None` (missing data field) |
| `awake_seconds` | Yes | `None` |
| `light_sleep_seconds` | Yes | `None` |
| `deep_sleep_seconds` | Yes | `None` |
| `rem_sleep_seconds` | Yes | `None` |
| `sleep_latency_seconds` | Yes | `None` |
| `wake_after_sleep_onset_seconds` | Yes | `None` |
| `wakeups` | Yes | `None` |
| `sleep_score` | Yes | `None` |
| `source_device_id` | Yes | `None` |
| `source_device_model` | Yes | `None` |
| `attribution` | No | Always `{}` when absent |

### 5.3 Invalid Timezone Handling

`resolve_timezone()` returns `None` for empty, whitespace-only, or
unrecognized IANA names instead of raising. This allows optional schema
columns to stay null without crashing the normalization pipeline.

---

## 6. Attribution And Provenance

### 6.1 Attribution Propagation

Every normalized measurement and sleep row carries an `attribution` JSONB
column shallow-copied from the parent `HealthSourceRecord.attribution`. This
allows downstream consumers to trace lineage without joining back to source
records.

### 6.2 Source Record Linkage

| Normalized table | FK column | References |
|:---|:---|:---|
| `health_normalized_measurements` | `source_record_id` | `health_source_records(id)` ON DELETE CASCADE |
| `health_normalized_sleep` | `source_record_id` | `health_source_records(id)` ON DELETE CASCADE |

Both tables also carry `connection_id` and `user_id` for direct scoping
without joins.

---

## 7. Revisions

### 7.1 Delete-Then-Insert Semantics

When a source record is revised (e.g., `measurements_revision.json` replaces
muscle_mass with bone_mass, or `sleep_summary_completed_revision.json` marks a
session completed), the sync transaction:

1. Upserts the source record (incrementing `revision_count` if content changed)
2. Calls `replace_normalized_measurements()` or `replace_normalized_sleep()`
3. These methods execute `DELETE` then `INSERT` within the same transaction

This guarantees:
- **No stale rows** from previous revisions
- **Atomicity** — delete and insert are in the same database transaction
- **Idempotency** — replaying the same source record produces identical
  normalized state

### 7.2 Measurement Revision Example

Before revision (from `measurements_page_1.json`):
```
source_record_id=X, metric=weight       → 70.54 kg
source_record_id=X, metric=fat_ratio    → 21.2%
source_record_id=X, metric=muscle_mass  → 14.98 kg
```

After revision (from `measurements_revision.json`, same grpid):
```
source_record_id=X, metric=weight      → 70.42 kg
source_record_id=X, metric=fat_ratio   → 20.9%
source_record_id=X, metric=bone_mass   → 5.45 kg
```

The muscle_mass row is gone; bone_mass is new; weight and fat_ratio are updated.

### 7.3 Sleep Revision Example

Before: `completeness_state = "partial"`, `sleep_score = 55`
After: `completeness_state = "complete"`, `sleep_score = 83`

The old row is deleted and replaced; `local_sleep_date` remains derived from
the (same) wake time.

---

## 8. Tombstones

### 8.1 Propagation Rule

When a tombstone arrives for a source record:

1. The source record is soft-deleted (`is_deleted = true`, `deleted_at` set)
2. All normalized rows for that `source_record_id` are explicitly `DELETE`d
   from the normalized tables in the same transaction

This means:
- **Queries never see deleted data** — normalized tables have no `is_deleted`
  column; rows are physically removed
- **Audit trail preserved** — the soft-deleted source record remains for
  compliance/debugging
- **Cursor advances normally** — the tombstone is a first-class event, not a
  cursor skip

### 8.2 Tombstone Fixture IDs

| Fixture | Resource type | Effect |
|:---|:---|:---|
| `measurements_tombstones` | MEASUREMENT | Deletes all normalized measurement rows for the source record |
| `sleep_tombstones` | SLEEP | Deletes the normalized sleep row for the source record |

---

## 9. Sleep Local-Date Rules

### 9.1 Derivation

`local_sleep_date` is derived from the **wake time** (`ended_at`), not the
sleep start time. The wake time is converted to the source timezone, and the
local date is extracted.

```
local_wake = ended_at.astimezone(source_timezone)
local_sleep_date = local_wake.date()
```

### 9.2 Rationale

A sleep that starts at 23:00 Monday and ends at 07:00 Tuesday is "Monday
night's sleep." Using wake time captures this convention correctly.

### 9.3 DST Handling

`local_sleep_date` uses the **actual DST-aware offset** at the wake time, not
a fixed offset. This means:

- **Spring forward** (e.g., `sleep_dst_spring.json`): A sleep ending at 07:00
  EDT (UTC-4) on March 10 is assigned to March 9 if it crossed the transition.
- **Fall back** (e.g., `sleep_dst_fall.json`): A sleep ending at 07:00 EST
  (UTC-5) on November 4 is assigned to November 3 if it crossed the transition.

The `local_offset_seconds` column stores the DST-resolved offset at wake time.
If the timezone cannot be resolved, `local_sleep_date` falls back to the UTC
date of `ended_at`.

### 9.4 Cross-Midnight And Split Dates

| Scenario | Behavior |
|:---|:---|
| **Cross-midnight** (`sleep_cross_midnight.json`) | Wake time determines the date; start time is irrelevant |
| **Nap** (`sleep_nap.json`, < 2h) | Treated identically to any sleep session; same local-date rule |
| **Split same date** (`sleep_split_same_date.json`) | Two sessions on same `local_sleep_date`; rolling query groups them |
| **Overlapping** (`sleep_overlapping.json`) | Both sessions stored independently; no dedup by time range |

---

## 10. Query Rules

### 10.1 User Scoping

Every query in `read_models.py` requires `user_id` as a mandatory parameter.
No query can cross user boundaries. This is enforced at the SQL level (`WHERE
user_id = $1`).

### 10.2 Query-Time Aggregation

There are **no aggregate tables**. All trends and summaries are computed at
query time from the normalized tables. The bridge decisions behind this:

| Decision | Rationale |
|:---|:---|
| No aggregate tables | Avoids stale aggregate state after source revisions or tombstones |
| Query-time computation | Acceptable for MVP query volumes; keeps implementation simple |
| No sleep stage timeline table | Sleep detail records are intentionally not normalized; only summaries are stored |

### 10.3 Weight Queries

| Function | Returns | Window |
|:---|:---|:---|
| `get_weight()` | `WeightResult` (latest + trends) | N/A |
| Latest reading | Single `WeightReading` | Most recent by `measured_at DESC` |
| 7-day trend | `readings_7d` list + `avg_7d`, `min_7d`, `max_7d` | `measured_at >= now - 7 days` |
| 30-day trend | `readings_30d` list + `avg_30d` | `measured_at >= now - 30 days` |

When no weight data exists, `WeightResult` returns `None`/empty for all fields.

### 10.4 Sleep Queries

| Function | Returns | Window |
|:---|:---|:---|
| `get_nightly_sleep()` | `NightlySleepResult` | Single `local_sleep_date` |
| `get_sleep_rolling_7d()` | `SleepRollingResult` | `local_sleep_date` in `[ref_date - 6 days, ref_date]` |

Within the rolling window, sessions are grouped by `local_sleep_date`. Per-date
aggregates include `session_count`, `total_asleep_seconds`,
`total_in_bed_seconds`, and `avg_sleep_score`.

When no sleep data exists, `SleepRollingResult` returns empty `summaries` list
and `nights_with_data = 0`.

### 10.5 Connection Freshness

`get_connection_freshness()` checks whether the connection's `last_success_at`
is within 7 days. This is exposed at the tool layer as `connection_fresh` and
`last_sync_at`. Callers should use freshness as a staleness hint, not a hard
data-availability gate (a user with no recent sync may still have historical
data).

### 10.6 Tombstone Safety

Because tombstone processing physically deletes normalized rows and queries
only read the normalized tables, deleted data is immediately invisible to all
query paths. No `is_deleted` filter is needed in query SQL.

---

## 11. Golden Fixture Examples

All examples below are drawn from files in `tests/fixtures/withings/`. They are
synthetic and sanitized — no real user data.

### 11.1 Weight Measurement (measurements_page_1.json)

**Raw Withings measure group (grpid=9001001):**

```json
{
  "grpid": 9001001,
  "date": 1784509200,
  "deviceid": "synthetic-scale-device-01",
  "model": "Body Comp",
  "timezone": "America/New_York",
  "measures": [
    {"value": 70540, "type": 1, "unit": -3},
    {"value": 212,   "type": 6, "unit": -1},
    {"value": 14980, "type": 76, "unit": -3}
  ]
}
```

**Normalized output (3 rows):**

| metric | measured_at | value_numeric | canonical_unit |
|:---|---:|---:|:---|
| weight | 2026-07-19T13:00:00Z | 70.54 | kg |
| fat_ratio | 2026-07-19T13:00:00Z | 21.2 | percent |
| muscle_mass | 2026-07-19T13:00:00Z | 14.98 | kg |

### 11.2 Weight Revision (measurements_revision.json)

Same grpid, later `modified`, different measures:

```json
{
  "grpid": 9001002,
  "date": 1784510100,
  "measures": [
    {"value": 70420, "type": 1, "unit": -3},
    {"value": 209,   "type": 6, "unit": -1},
    {"value": 5450,  "type": 88, "unit": -3}
  ]
}
```

**Normalized output (3 rows, replacing previous):**

| metric | measured_at | value_numeric | canonical_unit |
|:---|---:|---:|:---|
| weight | 2026-07-19T13:15:00Z | 70.42 | kg |
| fat_ratio | 2026-07-19T13:15:00Z | 20.9 | percent |
| bone_mass | 2026-07-19T13:15:00Z | 5.45 | kg |

**Key**: muscle_mass row deleted; bone_mass row appears; weight and fat_ratio
values updated.

### 11.3 Sleep Summary (sleep_summary_page_1.json)

**Raw Withings sleep summary:**

```json
{
  "id": 9203001,
  "timezone": "America/New_York",
  "startdate": 1784469600,
  "enddate": 1784494800,
  "date": "2026-07-19",
  "completed": true,
  "data": {
    "total_timeinbed": 25200,
    "total_sleep_time": 23640,
    "lightsleepduration": 13200,
    "remsleepduration": 4200,
    "deepsleepduration": 6240,
    "wakeupcount": 2,
    "sleep_score": 83
  }
}
```

**Normalized output:**

| Field | Value | Derivation |
|:---|---:|:---|
| `started_at` | 2026-07-19T22:00:00Z | `startdate` epoch → UTC |
| `ended_at` | 2026-07-20T05:00:00Z | `enddate` epoch → UTC |
| `local_sleep_date` | 2026-07-20 | Wake time (05:00 UTC → 01:00 EDT) → date |
| `local_timezone` | America/New_York | From series entry |
| `local_offset_seconds` | -14400 | EDT offset at wake time (UTC-4) |
| `completeness_state` | complete | `"completed": true` |
| `total_in_bed_seconds` | 25200 | 7h 0m |
| `total_asleep_seconds` | 23640 | 6h 34m |
| `sleep_score` | 83 | Raw score |

### 11.4 Incomplete → Complete Sleep Revision

From `sleep_summary_incomplete.json` (completed=false, score=55) →
`sleep_summary_completed_revision.json` (completed=true, score=83):

| Field | Before (incomplete) | After (complete) |
|:---|---:|---:|
| `completeness_state` | partial | complete |
| `total_asleep_seconds` | 14400 | 25200 |
| `sleep_score` | 55 | 83 |

### 11.5 Cross-Midnight Sleep (sleep_cross_midnight.json)

startdate=1784671200 (2026-07-22 02:00 UTC), enddate=1784702400 (2026-07-22
10:40 UTC), timezone=America/New_York (EDT, UTC-4).

Wake time in local: 10:40 UTC → 06:40 EDT → **local_sleep_date = 2026-07-22**.

### 11.6 Missing Optional Fields (sleep_missing_optional.json)

When `hash_deviceid` and some `data` fields are absent from the provider
response:

| Field | Value |
|:---|:---|
| `source_device_id` | `None` |
| Missing data fields (e.g., `wakeupcount`) | `None` |
| Present data fields | Decoded normally |
| `completeness_state` | Partial (default when no `completed` flag) |

---

## 12. Privacy Boundaries

### 12.1 Tool-Level Access Control

`_check_health_read_scope()` in `read_tools.py` enforces four hard gates:

| Gate | Requirement |
|:---|:---|
| `ctx.bot_id` | Must be `"hector"` or `"habits"` |
| `ctx.primary_topic_slug` | Must be `"fitness"` or `"habits"` |
| `ctx.primary_topic_id` | Must not be `None` |
| `ctx.user.id` | Must not be `None` |

Any other bot, topic, or missing context raises `ValueError`. This means:

- **Coach**, **Mediator**, **Tante Rosi**, and **Superpom** bots cannot call
  `get_weight_trend` or `get_sleep_summary` — they are not in the tool
  allowlist
- Even Hector cannot read health data outside fitness/habits topics
- Anonymous or unauthenticated contexts are rejected before any query

### 12.2 User Scoping at Query Layer

Every SQL query in `read_models.py` includes `WHERE user_id = $1`. No query
can accidentally cross user boundaries. The tool layer passes `ctx.user.id`
directly; no caller can supply an arbitrary user ID.

### 12.3 Data Minimization

| Layer | What is exposed |
|:---|:---|
| Tool schema (`WeightTrendPoint`) | `measured_at`, `value_numeric`, `canonical_unit` — no device info, no attribution |
| Tool schema (`SleepDaySummaryRow`) | `local_sleep_date`, `session_count`, `total_asleep_hours`, `total_in_bed_hours`, `avg_sleep_score` — no per-session detail, no stage timeline, no device info |
| Prompt guidance | "compact historical values," "never raw measurement-level data," "no sleep-stage timelines" |

### 12.4 Medical Interpretation Prohibition

The prompt slot `health_read_guidance.py` (order 790, hector-only) explicitly
instructs:
- Never for medical interpretation
- Never for commitment satisfaction
- Never for commitment creation
- Weight/sleep are "informational context, not tracked commitments"

---

## 13. Bridge Decisions

These are decisions made during implementation that affect the data model and
consumer contract. Future milestones must preserve these.

| # | Decision | Rationale |
|:---|:---|:---|
| **BD1** | Composite UNIQUE on `(source_record_id, metric)` replaces single-column UNIQUE | Allows one Withings measure group (multiple metric types) to fan out into multiple normalized rows. Migration 0064. |
| **BD2** | No sleep stage timeline table | Sleep detail records (high-frequency stage data) are intentionally not normalized. Only summary records produce rows in `health_normalized_sleep`. |
| **BD3** | Query-time aggregation only | No materialized views, no aggregate tables. Trends (7d avg, 30d avg, per-date sleep summaries) are computed at query time to stay correct after revisions/tombstones. |
| **BD4** | Delete-then-insert for revisions | Normalized tables have no version column. Revisions physically replace rows in a single transaction. Simplifies queries (no `is_current` filter) and guarantees tombstone safety. |
| **BD5** | `local_sleep_date` from wake time | Uses `ended_at` converted to source timezone (DST-aware). Avoids ambiguity of cross-midnight sessions. |
| **BD6** | `source_unit` mirrors `canonical_unit` | After normalization, the stored unit is the canonical one. The `source_unit` column exists for schema consistency but carries the same value. |
| **BD7** | No `is_deleted` filter in query SQL | Tombstones physically delete normalized rows. Queries are naturally tombstone-safe without extra predicates. |
| **BD8** | Attribution JSONB stored per row | Each normalized row carries a shallow-copy of the source record's attribution dict. Downstream consumers can trace provenance without joining to `health_source_records`. |

---

## 14. Schema Reference

### 14.1 `health_normalized_measurements`

| Column | Type | Constraints |
|:---|:---|:---|
| `id` | UUID | PK |
| `source_record_id` | UUID | FK → health_source_records(id) ON DELETE CASCADE, UNIQUE(source_record_id, metric) |
| `connection_id` | UUID | NOT NULL |
| `user_id` | UUID | NOT NULL |
| `metric` | TEXT | NOT NULL |
| `measured_at` | TIMESTAMPTZ | NOT NULL |
| `value_numeric` | DOUBLE PRECISION | NOT NULL |
| `canonical_unit` | TEXT | NOT NULL |
| `source_unit` | TEXT | |
| `source_device_id` | TEXT | |
| `source_device_model` | TEXT | |
| `attribution` | JSONB | NOT NULL DEFAULT '{}' |

RLS: enabled, deny-anon. Index: `idx_health_normalized_measurements_user_metric_measured`.

### 14.2 `health_normalized_sleep`

| Column | Type | Constraints |
|:---|:---|:---|
| `id` | UUID | PK |
| `source_record_id` | UUID | FK → health_source_records(id) ON DELETE CASCADE, UNIQUE |
| `connection_id` | UUID | NOT NULL |
| `user_id` | UUID | NOT NULL |
| `started_at` | TIMESTAMPTZ | NOT NULL |
| `ended_at` | TIMESTAMPTZ | NOT NULL |
| `local_sleep_date` | DATE | NOT NULL |
| `local_timezone` | TEXT | |
| `local_offset_seconds` | INTEGER | |
| `completeness_state` | TEXT | NOT NULL DEFAULT 'partial' |
| `total_in_bed_seconds` | INTEGER | |
| `total_asleep_seconds` | INTEGER | |
| `awake_seconds` | INTEGER | |
| `light_sleep_seconds` | INTEGER | |
| `deep_sleep_seconds` | INTEGER | |
| `rem_sleep_seconds` | INTEGER | |
| `sleep_latency_seconds` | INTEGER | |
| `wake_after_sleep_onset_seconds` | INTEGER | |
| `wakeups` | INTEGER | |
| `sleep_score` | INTEGER | |
| `source_device_id` | TEXT | |
| `source_device_model` | TEXT | |
| `attribution` | JSONB | NOT NULL DEFAULT '{}' |

RLS: enabled, deny-anon. Uniqueness: one row per source_record_id (single-column UNIQUE).

---

## 15. Code Anchors

| Surface | File |
|:---|:---|
| Data models | `app/services/health_sync/models.py` |
| Normalization | `app/services/health_sync/normalization.py` |
| Repository writes | `app/services/health_sync/repository.py` |
| Sync wiring | `app/services/health_sync/sync.py` |
| Query service | `app/services/health_sync/read_models.py` |
| Tool schemas | `tool_schemas.py` (WeightTrendPoint, SleepDaySummaryRow, etc.) |
| Tool handlers | `app/services/tools/read_tools.py` |
| Tool registry | `app/services/tools/registry.py` |
| Prompt guidance | `app/bots/prompts/slots/health_read_guidance.py` |
| Migration (fan-out) | `migrations/0064_health_measurement_fan_out.sql` |
| Fixtures | `tests/fixtures/withings/catalog.json` and companion JSON files |
| Contract tests | `tests/test_health_read_models.py`, `tests/test_health_sync_records.py` |
