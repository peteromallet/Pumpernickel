# Longitudinal user state ("user_journeys")

> **Rejected 2026-05-12** in favor of per-feature schemas — see
> `docs/pregnancy-state.md`.  Revisit if 3+ bots show pattern overlap.

Status: draft / design contract. Not yet implemented.
Last updated: 2026-05-12 (revision 2 — incorporates sense-check feedback on dating anchors, recompute scope, rate semantics, privacy contract, per-kind schemas)

## Why this exists

Some bots need to track a user's structured, evolving state over weeks or months: a pregnancy (gestational week, weight gain, scan dates), a fitness program (weights, frequency, race times), a weight-loss arc, a savings target, a sobriety streak. The existing artifact tables (`memories`, `themes`, `observations`, `distillations`, `watch_items`, `out_of_bounds`) capture narrative and emotional content. They are not the right place for *numeric, time-anchored, target-bearing* user state.

This doc defines a minimal second-class artifact for that state: **user journeys**. One table for the umbrella (a pregnancy, a fitness program), one table for measurements. Tracks (what's being tracked, with what unit and target) live as JSONB inside the journey row, not as their own table — premature normalization is the failure mode we're explicitly avoiding here.

The first consumer is Tante Rosi (pregnancy coach). The schema must also obviously fit a future fitness bot and a future weight-loss bot.

## Non-goals

- A general-purpose habits or productivity tracker
- Cross-journey analytics ("show all kg measurements across all users")
- A scheduled-task system (`scheduled_tasks` already exists; cadenced check-ins use it)
- A milestones table (appointments fit `scheduled_tasks`; "interim target hit" fits a flag in `current_state`)
- Multi-topic visibility of a single journey (deferred — one journey, one topic)

## Schema

```sql
CREATE TABLE user_journeys (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES users(id),
  topic_id uuid NOT NULL REFERENCES topics(id),
  owner_bot_id text NOT NULL REFERENCES bots(id),

  kind text NOT NULL,                    -- 'pregnancy' | 'weight_loss' | 'fitness' | ...
  label text NOT NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'completed', 'transitioned', 'abandoned', 'lost')),

  started_at timestamptz NOT NULL DEFAULT now(),
  ended_at timestamptz,
  supersedes_journey_id uuid REFERENCES user_journeys(id),

  anchors       jsonb NOT NULL DEFAULT '{}'::jsonb,
  tracks        jsonb NOT NULL DEFAULT '{}'::jsonb,
  current_state jsonb NOT NULL DEFAULT '{}'::jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_user_journeys_active
  ON user_journeys (topic_id, user_id, updated_at DESC)
  WHERE status = 'active';

CREATE INDEX idx_user_journeys_kind
  ON user_journeys (user_id, kind, status);

CREATE TABLE journey_measurements (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  journey_id uuid NOT NULL REFERENCES user_journeys(id) ON DELETE CASCADE,
  track_key text NOT NULL,
  observed_at timestamptz NOT NULL DEFAULT now(),
  value_numeric numeric,
  value_text text,
  source text NOT NULL DEFAULT 'user_reported'
    CHECK (source IN ('user_reported', 'computed', 'derived_from_message', 'imported')),
  supporting_message_ids uuid[] NOT NULL DEFAULT '{}',
  measurement_group_id uuid,      -- groups co-occurring measurements (e.g. a single gym session)
  note text,
  created_at timestamptz NOT NULL DEFAULT now(),
  superseded_by uuid REFERENCES journey_measurements(id),  -- for corrections; v1 leaves null
  CHECK (value_numeric IS NOT NULL OR value_text IS NOT NULL)
);

CREATE INDEX idx_journey_measurements_latest
  ON journey_measurements (journey_id, track_key, observed_at DESC)
  WHERE superseded_by IS NULL;

CREATE INDEX idx_journey_measurements_group
  ON journey_measurements (measurement_group_id)
  WHERE measurement_group_id IS NOT NULL;
```

Notes:

- `topic_id` is direct, not via `artifact_topics`. The polymorphic helper at `topic_filter.py:12` only knows six aliases and the unique-index on `artifact_topics` constrains an artifact to one active topic at a time anyway. A direct column is simpler and we keep the door open to migrate later.
- `owner_bot_id` is the bot that created and is primary owner of the journey. Other bots can read by joining on `topic_id` (same as how cross-bot read of any artifact works).
- No FK from `tracks` JSON keys to measurement `track_key`. The contract is upheld by code; the DB doesn't enforce. We accept that.
- `measurement_group_id` groups co-occurring measurements (e.g. a single gym session producing one `frequency` event + one `bench` reading + one optional accessory lift). Nullable; only used by kinds with composite events.
- `superseded_by` enables future correction tools (`correct_measurement`) without destructive overwrites. **v1 does not expose a correction tool** — users add a new measurement with a `note`. The column is included from day one to avoid a later migration.

## Status lifecycle

| Status | Meaning | Trigger |
|---|---|---|
| `active` | Currently being tracked. | Default on create. |
| `paused` | User stepped away; resume expected. | Explicit user/bot action. |
| `completed` | Reached intended endpoint. | E.g. weight target hit and held — see "hold rule" below. |
| `transitioned` | Replaced by a successor journey. | New journey created with `supersedes_journey_id = this.id`. |
| `abandoned` | User dropped it. Neutral, no negative framing. | Explicit. |
| `lost` | Specific to pregnancy / similar life events where "abandoned" is wrong. | Specific to applicable `kind`s. |

The `lost` status exists so a pregnancy that ended in miscarriage or stillbirth is not lumped with "abandoned." This matters for prompt-render: the bot should treat a `lost` journey with care, not cold dismissal. Only certain `kind`s should permit `lost`; enforced in app, not DB.

### Allowed transitions

| From | To | Notes |
|---|---|---|
| `active` | `paused`, `completed`, `transitioned`, `abandoned`, `lost` | All forward moves permitted. |
| `paused` | `active`, `abandoned`, `lost` | Resume or close. |
| `completed`, `transitioned`, `abandoned`, `lost` | (terminal) | No reopen. To resume, create a new journey. |

Enforced in `transition_journey` tool, not DB.

### Hold rule for `completed`

"Target hit once" ≠ completed. A journey moves to `completed` only when the recomputer detects the target has held for **N consecutive observations at cadence** (or N cadence-windows for `computed` tracks). N is per-kind, defaulting to 2. Weight-loss N=2 (two weekly weigh-ins at or below target). Fitness `bench` N=1 (a lift is a lift). Pregnancy does not transition to `completed` — it transitions to a successor postpartum journey via `transitioned`.

## `kind` registry

Each `kind` is registered in code. The registry entry defines:

1. **Allowed/required anchors** (validator)
2. **Allowed track keys + their metric_kind, unit, target shape** (validator)
3. **Recomputer** — given anchors + tracks + latest measurements, produces `current_state` JSON
4. **Renderer** — given `current_state`, produces a markdown block for the prompt
5. **Allowed statuses** — e.g. pregnancy allows `lost`; weight_loss does not

A bot only operates on kinds it owns. Hot context surfaces all active journeys for the user filtered by topic.

## Anchors

Anchors are stable facts that frame the journey. They are usually set at creation, occasionally updated, rarely mutated otherwise. Each `kind` has a documented anchors schema (validated by the registry, not the DB).

Anchors are not measurements. They are not append-only. A change to an anchor is an in-place update and triggers a `current_state` recompute.

### Canonical dating: one source of truth per derived quantity

When multiple anchor values could underlie a computed track, exactly one anchor is the canonical source and the rest are provenance. **Pregnancy is the worked example: the canonical anchor for gestational age is `estimated_due_date`, not `lmp_date`.** Reasons:

- LMP is one of several dating methods. A first-trimester dating scan supersedes LMP-based estimates.
- Mutating LMP would silently shift everything else if any code happened to read it directly.
- A single canonical anchor + a `dating_basis` provenance field is unambiguous.

Pregnancy anchors:

```jsonc
{
  "estimated_due_date": "2026-10-22",        // CANONICAL — gestational_week computes from this
  "dating_basis": "lmp" | "scan",            // how estimated_due_date was set
  "lmp_date": "2026-01-15",                  // provenance only; not used for computation
  "scan_date": null,                         // set when dating_basis = "scan"
  "scan_corrected_at": null,                 // when the EDD was last revised
  "multiple_count": 1,
  "risk_factors": []
}
```

Gestational week is `(40w - (estimated_due_date - now)) ⌊⌋` clamped to [0, 42]. A dating-scan correction:

1. Tool `update_journey_anchors` patches `estimated_due_date`, `dating_basis='scan'`, `scan_date`, `scan_corrected_at = now()`. LMP stays.
2. Recompute fires. All downstream values (week, trimester, milestones) shift consistently.

The same pattern applies to any kind with multiple potentially-conflicting date sources: declare one canonical field per derived quantity, keep the others as provenance.

## Tracks (JSON inside the journey row)

The `tracks` field is a JSONB object keyed by `track_key` (a stable string per kind). Each track value has this shape:

```jsonc
{
  "metric_kind": "measurement" | "rate" | "computed",
  "unit": "kg" | "seconds" | "sessions/week" | "weeks" | null,
  "target": { "value": <number>, "direction": "up" | "down", "range": [low, high] } | null,
  "cadence": "P1W" | "P2W" | "P1M" | null,     // ISO-8601 duration; how often to check in
  "window": "rolling_7d" | "rolling_28d" | "calendar_week" | "calendar_month" | null,
                                                // required for metric_kind=rate; null otherwise
  "from_anchor": "<anchor_key>" | null,        // required for metric_kind=computed
  "formula": "weeks_since" | "days_since" | "weeks_until" | "days_until" | null,
                                                // required for metric_kind=computed
  "completion_hold_n": 2,                       // optional; consecutive observations at target before completing
  "description": "<short human label>"
}
```

### `metric_kind` semantics

| `metric_kind` | What it represents | Measurements? | Target meaningful? | Example |
|---|---|---|---|---|
| `measurement` | A directly-observed numeric value over time. | Yes, one row per observation. | Optional. | weight, bench, balance |
| `rate` | A count-per-window derived from event measurements. | Yes, one row per event with `value_numeric = 1` (or a positive integer for a multi-count event). | Optional. | sessions/week |
| `computed` | Derived purely from anchors + `now()`. No measurements. | No rows. | Rare; usually a deadline, not a goal. | gestational_week, days_clean |

`anchor_date` (a tracked-but-unmeasured future date) was considered and **cut**: planned dates live in `anchors` directly; appointment dates live in `scheduled_tasks`. Making them tracks blurred the model without paying rent.

Streaks (sobriety days) are not their own kind — they are `computed` from an anchor (`last_break_date`) with formula `days_since`.

### Rate-window math (precise)

For a track with `metric_kind: 'rate'` and `unit: 'sessions/week'`:

| `window` | Numerator | Denominator | Notes |
|---|---|---|---|
| `rolling_7d` | sum of `value_numeric` for measurements with `observed_at` in `[now() - 7d, now()]` | 1 (week) | Recent intensity. |
| `rolling_28d` | sum in `[now() - 28d, now()]` | 4 (weeks) | Smoothed. **Default for sessions/week.** |
| `calendar_week` | sum in current ISO week (Mon 00:00 local → now) | days_elapsed_in_week / 7 | Anchored to user's local week. |
| `calendar_month` | sum in current calendar month | days_elapsed_in_month / (days_in_month) × (weeks_in_month) | Rarely useful for fitness. |

Program-age ramp-up: for the first `28d` after `started_at`, `rolling_28d` divides by `min(4, days_since_start / 7)` to avoid penalizing week 1.

### Event grouping (rate + co-occurring measurements)

A gym session that includes a bench reading is *one event* with two effects. To preserve the link:

1. Caller generates a fresh `measurement_group_id` UUID.
2. Logs the `frequency` event with `value_numeric=1` and `measurement_group_id=<uuid>`.
3. Logs the `bench` measurement with the *same* `measurement_group_id`.

The `log_journey_measurement` tool accepts an optional `measurement_group_id` argument and a sugar `with_group: bool` that auto-generates one if true. Multi-measurement sessions can also use a `log_journey_event(journey_id, group: [{track_key, value, ...}, ...])` convenience tool — added in v2 if it pays rent.

### Target shape

```jsonc
{
  "value": 75,                  // primary target value
  "direction": "down",          // "up" | "down" — required so progress_pct can be computed
  "range": [70, 75]             // optional acceptable range
}
```

If `target` is null, the track is observational only (e.g. gestational_week — there is no "target" 40, it's an expected endpoint).

## `current_state` (the prompt-ready cache)

`current_state` is a JSONB object that the prompt renderer dumps with minimal transformation. Each `kind`'s recomputer decides the schema; common conventions:

```jsonc
{
  "<track_key>": {
    "value": 23,
    "label": "23w2d",                  // optional pre-formatted display string
    "unit": "weeks",
    "target": 40,                      // copied from track def for convenient render
    "started": 0,
    "progress_pct": 57,
    "trend_4w": "down" | "up" | "flat" | null,
    "last_observed_at": "2026-05-10T08:00Z" | null,
    "stale": false,                    // last_observed_at > 2 × cadence
    "as_of": "2026-05-12T08:00Z"
  },
  "next_milestone": {                  // optional, denormalized from scheduled_tasks
    "name": "20-week scan",
    "due": "2026-06-04"
  }
}
```

The renderer can use `label` directly if present, otherwise format `value`+`unit`.

## Recompute discipline

`current_state` is a cache. It is **only valid because every mutating path calls the recomputer.** The single helper is:

```python
async def recompute_journey_state(pool, journey_id: UUID) -> None:
    """Rebuild current_state from anchors + tracks + measurements + now()."""
```

It MUST be called from:

1. After any insert (or supersede) into `journey_measurements` for this journey
2. After any update to `anchors` or `tracks` on this journey
3. After any status change
4. After any mutation to a `scheduled_tasks` row that this journey's recomputer reads (e.g. `next_milestone` denormalized into `current_state`) — see the scheduled-task hook below
5. From a cron tick for **every active journey** (not only those with `computed` or `rate` tracks). Reason: `stale` flags and `last_observed_at` ages depend on elapsed time, so a journey with only a `measurement` track still requires periodic recompute to flip `stale: false → true` when no new reading arrives.

The cron tick is the only place `current_state` updates without a user-driven mutation. Cadence: daily for most kinds, hourly if a kind explicitly needs finer granularity (declare in registry).

### Scheduled-task hook

A journey kind can declare that its `current_state` depends on `scheduled_tasks` rows (e.g. pregnancy denormalizes `next_milestone` from upcoming appointment tasks). Mechanism: each `scheduled_tasks` row may carry an optional `journey_id` foreign key. Inserts/updates/deletes on rows with non-null `journey_id` enqueue a recompute for that journey. This is a tiny additive change to `0017_scheduled_tasks.sql` (add column + index); the journey migration adds the column.

### Test contract

A unit test asserts: after `recompute`, `current_state.<track>.as_of` is within 1 second of `now()` and `last_observed_at` matches the latest non-superseded measurement. A second test asserts: an active journey with a `measurement` track and no new readings has `current_state.<track>.stale` flip true after the cron tick following `last_observed_at + 2 × cadence`.

## Render contract (hot context)

A new section is added to both `hot_context.py` and `hot_context_solo.py`, near `topic_status`:

```
## Longitudinal state
- Pregnancy (Tante Rosi, started 2026-01-15):
  - gestational_week: 17w2d (second trimester, as of today)
  - weight_gain: +2.1kg of ~12kg target (last weigh-in 2 days ago)
  - next: 20-week scan on 2026-06-04
- Weight loss (fitness_bot, started 2026-05-01):
  - weight: 82.1kg, target 75kg — down 2.9kg of 10 (29% there, trending down)
  - last weigh-in 2 days ago
```

Each `kind`'s renderer produces its own block. If there are no active journeys, the section is omitted (not rendered as "none" — keeps the prompt clean).

`current_state.stale: true` triggers a soft prompt cue: `"⚠ stale — last reading <N> days ago"` appended to the line. The bot decides whether to nudge.

## Tools (write side)

Five new write tools:

| Tool | Purpose |
|---|---|
| `create_journey(kind, label, anchors, tracks)` | Create a new journey for the current user/topic/bot. Validates against the kind registry. |
| `update_journey_anchors(journey_id, anchors_patch)` | Patch anchors. Triggers recompute. |
| `log_journey_measurement(journey_id, track_key, value, observed_at?, note?, source?)` | Append a measurement. Triggers recompute. |
| `transition_journey(journey_id, new_status, successor?)` | Move status. If `transitioned`, requires a `successor` (which is created in the same call or already exists). |
| `update_journey_tracks(journey_id, tracks_patch)` | Add/modify track definitions (e.g. user decides to also track sleep). Triggers recompute. |

Three new read tools:

| Tool | Purpose |
|---|---|
| `get_journeys(status?, kind?)` | List journeys for the current user/topic. |
| `get_journey_measurements(journey_id, track_key?, since?, limit?)` | Time-series for charting / reasoning. |
| `get_active_journey(kind)` | Quick "is there an active pregnancy?" check. |

## Multi-bot read

By default a bot sees only journeys owned by it or in its topic. A cross-bot read tool (`get_journeys_across_topics`) is deferred. The common case (mediator bot wants to know its user is pregnant) is handled by surfacing pregnancy through the relationship topic via topic linkage, not via cross-bot reads at runtime.

## Privacy and sharing (hard contract)

Journeys are sensitive-by-default. The rules are not optional and not per-kind unless explicitly opted out by a kind's registry entry.

1. **Journeys are NEVER auto-bridged** via `bridge_candidates` or any other partner-bridging path. A pregnancy doesn't leak to the partner thread because the mediator bot also lives in the relationship topic — the mediator only sees a journey if the user has explicitly enabled it via a journey-level visibility flag (see #3).
2. **`cross_thread_sharing_default` does NOT govern journey visibility.** That flag controls narrative bridging of memories/observations/distillations; journeys are categorically different (structured health/financial data) and have their own visibility regime.
3. **Per-journey `visibility` field** in `current_state` controls what other bots in the same topic see:
   - `'self_only'` (default for `pregnancy`, `weight_loss`): owner bot only. Cross-bot reads in the same topic return a redacted stub `{kind, status, started_at}` with no anchors/tracks/measurements/current_state.
   - `'shared_in_topic'`: any bot reading the topic gets the full `current_state` (but not raw measurements). Set explicitly by the user, never by a bot.
   - `'fully_shared'`: full reads including measurements. Rare; reserved for cases like a user explicitly authorizing the mediator to discuss pregnancy progress with the partner.
4. **OOB always wins.** A `medical_escalation` or any other OOB hit against journey content overrides any visibility setting in the direction of *more* protective behavior, never less.
5. **Deletion.** Hard delete of a journey hard-deletes all measurements (CASCADE). A retention/export request goes through the same path as other special-category user data.

The `visibility` field lives in `current_state` (as `current_state.meta.visibility`) so it's queryable in the same place all render data is queryable, and so changes go through `recompute_journey_state` for audit symmetry.

## Per-kind contracts

Each registered `kind` has a formal anchors/tracks/current_state schema. The kind registry validates against these on every write tool. Schemas below are normative; implementations should mirror them with Pydantic models in `app/services/journeys/<kind>.py`.

### `pregnancy`

**Anchors (required unless noted):**

| Field | Type | Notes |
|---|---|---|
| `estimated_due_date` | date | **Canonical** — gestational age is computed from this. |
| `dating_basis` | `'lmp' \| 'scan'` | How EDD was set. |
| `lmp_date` | date \| null | Provenance. Optional. |
| `scan_date` | date \| null | Set when `dating_basis='scan'`. |
| `scan_corrected_at` | timestamptz \| null | When EDD was last revised by scan. |
| `multiple_count` | int (≥1) | 1 for singleton; 2 for twins; etc. |
| `risk_factors` | string[] | Free-form tags (e.g. `gestational_diabetes`). Defaults `[]`. |

**Allowed tracks:**

| Track key | metric_kind | Unit | Notes |
|---|---|---|---|
| `gestational_week` | `computed` | `weeks` | `from_anchor='estimated_due_date'`, `formula='weeks_until'` (and then subtracted from 40 by the recomputer). Always present. |
| `weight_gain` | `measurement` | `kg` | Target `{value: 12, direction: 'up', range: [10, 14]}` typical singleton. Optional track. |

**Allowed statuses:** `active`, `paused`, `transitioned`, `abandoned`, `lost`. Not `completed` — pregnancy transitions to postpartum via `transitioned`.

**Visibility default:** `self_only`.

### `weight_loss`

**Anchors:**

| Field | Type | Notes |
|---|---|---|
| `baseline_weight` | number | kg or lb (specify in `unit_system`) |
| `baseline_date` | date | |
| `target_date` | date \| null | Optional. |
| `unit_system` | `'metric' \| 'imperial'` | Defaults `'metric'`. |

**Tracks:**

| Track key | metric_kind | Unit | Notes |
|---|---|---|---|
| `weight` | `measurement` | `kg` or `lb` | Target `{value, direction: 'down'}`. `completion_hold_n: 2`. Cadence typically `P1W`. |

**Allowed statuses:** `active`, `paused`, `completed`, `abandoned`. Not `lost` or `transitioned`.

**Visibility default:** `self_only`.

### `fitness`

**Anchors:**

| Field | Type | Notes |
|---|---|---|
| `program_start_date` | date | |
| `program_type` | string \| null | e.g. `ppl_3day`, free-form. |

**Tracks (any subset, all optional):**

| Track key | metric_kind | Unit | Notes |
|---|---|---|---|
| `frequency` | `rate` | `sessions/week` | `window` typically `rolling_28d`. One measurement per session with `value_numeric=1`. |
| `bench`, `squat`, `deadlift`, … | `measurement` | `kg` or `lb` | Target `{value, direction: 'up'}`. `completion_hold_n: 1`. |
| `5k_time`, `10k_time`, … | `measurement` | `seconds` | Target `{value, direction: 'down'}`. |

**Event grouping:** workout sessions that include lifts should share a `measurement_group_id` across the `frequency` event and any lift measurements.

**Allowed statuses:** `active`, `paused`, `completed`, `transitioned`, `abandoned`.

**Visibility default:** `shared_in_topic` (fitness is typically non-sensitive; user can opt back to `self_only`).

## Worked examples

### Pregnancy (Tante Rosi)

```jsonc
{
  "kind": "pregnancy",
  "label": "Pregnancy (due Oct 2026)",
  "anchors": {
    "estimated_due_date": "2026-10-22",       // canonical
    "dating_basis": "lmp",
    "lmp_date": "2026-01-15",                 // provenance
    "scan_date": null,
    "scan_corrected_at": null,
    "multiple_count": 1,
    "risk_factors": []
  },
  "tracks": {
    "gestational_week": {
      "metric_kind": "computed",
      "unit": "weeks",
      "from_anchor": "estimated_due_date",
      "formula": "weeks_until",
      "description": "Gestational age (40w - weeks_until_due)"
    },
    "weight_gain": {
      "metric_kind": "measurement",
      "unit": "kg",
      "target": { "value": 12, "direction": "up", "range": [10, 14] },
      "cadence": "P2W",
      "description": "Pregnancy weight gain from baseline"
    }
  },
  "current_state": {
    "meta": { "visibility": "self_only" },
    "gestational_week": {
      "value": 17, "label": "17w2d", "unit": "weeks",
      "trimester": "second", "as_of": "2026-05-12T08:00Z"
    },
    "weight_gain": {
      "value": 2.1, "unit": "kg", "target": 12,
      "started": 0, "progress_pct": 17,
      "last_observed_at": "2026-05-10T08:00Z", "stale": false
    },
    "next_milestone": { "name": "20-week scan", "due": "2026-06-04" }
  }
}
```

**Dating-scan correction (mid-journey, week 12):**

```jsonc
// Tool call: update_journey_anchors(journey_id, patch)
{
  "estimated_due_date": "2026-10-28",   // shifted 6 days later by scan
  "dating_basis": "scan",
  "scan_date": "2026-04-10",
  "scan_corrected_at": "2026-04-10T14:30Z"
  // lmp_date stays at 2026-01-15 (provenance)
}
// Recompute fires; all derived state (week, trimester, due, milestones) shifts consistently.
```

### Weight loss

```jsonc
{
  "kind": "weight_loss",
  "label": "Lose 10kg",
  "anchors": {
    "baseline_weight": 85.0,
    "baseline_date": "2026-05-01",
    "target_date": "2026-08-01"
  },
  "tracks": {
    "weight": {
      "metric_kind": "measurement",
      "unit": "kg",
      "target": { "value": 75, "direction": "down" },
      "cadence": "P1W"
    }
  },
  "current_state": {
    "meta": { "visibility": "self_only" },
    "weight": {
      "value": 82.1, "unit": "kg",
      "delta_from_baseline": -2.9,
      "target": 75, "progress_pct": 29,
      "trend_4w": "down",
      "last_observed_at": "2026-05-10T08:00Z", "stale": false,
      "plateau": { "detected": false, "window": "P4W" }
    },
    "weeks_to_target_at_current_pace": 18,
    "completion_progress": { "consecutive_at_target": 0, "hold_required": 2 }
  }
}
```

### Fitness

```jsonc
{
  "kind": "fitness",
  "label": "Strength + 5K base",
  "anchors": {
    "program_start_date": "2026-03-01",
    "program_type": "ppl_3day"
  },
  "tracks": {
    "frequency": {
      "metric_kind": "rate",
      "unit": "sessions/week",
      "window": "rolling_28d",
      "target": { "value": 3, "direction": "up" }
    },
    "bench": {
      "metric_kind": "measurement",
      "unit": "kg",
      "target": { "value": 80, "direction": "up" },
      "completion_hold_n": 1
    },
    "5k_time": {
      "metric_kind": "measurement",
      "unit": "seconds",
      "target": { "value": 1500, "direction": "down" }
    }
  },
  "current_state": {
    "meta": { "visibility": "shared_in_topic" },
    "frequency": { "current_rate": 2.4, "target": 3, "window": "rolling_28d", "trend": "down", "as_of": "2026-05-12T08:00Z" },
    "bench": { "value": 67.5, "started": 60, "target": 80, "progress_pct": 37, "last_observed_at": "2026-05-11T18:00Z" },
    "5k_time": { "value_label": "26:42", "value_seconds": 1602, "target_label": "25:00", "progress_pct": 35, "last_observed_at": "2026-05-08T07:00Z" }
  }
}
```

**Example: logging a gym session (bench + frequency, same group):**

```python
group_id = uuid4()
await log_journey_measurement(j_id, track_key='frequency', value=1, measurement_group_id=group_id, note='leg+push day')
await log_journey_measurement(j_id, track_key='bench', value=67.5, measurement_group_id=group_id)
# Single recompute fires (debounced within transaction or batched).
```

## Open questions (genuine, not contract-disguised)

1. **Onboarding flow for `estimated_due_date`.** Rosi needs EDD on first turn. Recommend: tie into the proposed `user_pending_inputs` mechanism (separate doc) — `estimated_due_date` is a registered pending input that resurfaces until answered. Deferred until that doc lands.
2. **Cross-journey conflict surface.** If a user has both `pregnancy` and `weight_loss` active, the fitness bot should see the pregnancy in its hot context (read-only, redacted if visibility is `self_only`). Mechanism: hot context fetches all active journeys for the user; filters by topic; renders a "Other active journeys" sub-section using the redacted stub for `self_only` journeys ("user has an active pregnancy journey — owner: tante_rosi"). Deferred until both bots exist.
3. **Measurement-provenance enforcement.** `source='derived_from_message'` should require non-empty `supporting_message_ids`. Enforce in tool validation. Decide whether to also enforce at DB via a check constraint (probably yes — it's a one-line CHECK).
4. **Plotting and history queries.** `get_journey_measurements` returns raw time series. Do we ever render a sparkline summary in the prompt? Defer — text trend is enough for v1.
5. **Localization of unit_system.** Weight loss has `unit_system`. Pregnancy weight gain inherits user locale. Need to decide whether unit conversion happens at storage time (always store kg) or at render time (store native, convert for display).

## Migration plan

1. Migration `0033_user_journeys.sql`: both tables, indexes, RLS policies matching the `topic_status` pattern (`migrations/0022_topic_status_user_bot_state.sql:63`).
2. Migration `0034_scheduled_tasks_journey_link.sql`: add nullable `journey_id` column + index to `scheduled_tasks`. Trigger/app-level hook enqueues a recompute on journey-linked task changes.
3. Add `app/services/journeys/` package: `registry.py`, `recompute.py`, `pregnancy.py` (recomputer + renderer + validator), `models.py` (Pydantic schemas).
4. Add 5 write tools + 3 read tools (`app/services/tools/write_tools.py` and `read_tools.py`). Validate against the kind registry.
5. Add hot context render block in both `hot_context.py` and `hot_context_solo.py` gated on `current_state.meta.visibility` per journey.
6. Cron job: daily recompute pass over all `status='active'` journeys, via `scheduled_tasks` or a dedicated scheduler hook.
7. Tante Rosi persona file uses the new tools; coach allowlist includes them; mediator allowlist includes reads only (no `create_journey`).
8. Tests:
   - Kind-registry validation per kind (happy + boundary)
   - Recomputer per kind (gestational week math, rate windows, plateau detection, hold rule)
   - Render output snapshot per kind including stale/lost cases
   - Stale flag flip via simulated cron tick
   - Transition with successor (pregnancy → postpartum chain)
   - Dating-scan correction recompute (anchor mutation invariants)
   - Visibility-gated cross-bot read (redacted stub for `self_only`)
   - Measurement provenance enforcement (`derived_from_message` requires message ids)

Estimated lift: **1.2k–1.8k LOC** including migration, per-kind code, tool schemas, RLS, hot-context render plumbing in both renderers, scheduled-task hook, cron worker, and tests. Medium-effort sprint. Per-kind code is ~120-180 LOC per kind (recomputer + renderer + validator + tests).
