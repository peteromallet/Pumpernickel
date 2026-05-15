# Fitness Bot Commitments Plan

Status: design note with locked product decisions.
Last updated: 2026-05-14.

## Short Answer

This is plausible as a two-week sprint if the first version stays narrow:

- Add Hector as a solo fitness bot.
- Reuse topic-scoped artifacts, scheduling, privacy, and `topic_status`.
- Add one small commitment/adherence substrate.
- Avoid building a full fitness app, generic journey system, meal planner, or UI.

The central product behavior is:

> The user can tell the bot their goals and weekly plan. The bot remembers the
> plan, checks in against it, logs whether the user did the things, and applies
> low-key pressure without shame.

## Product Shape

Hector is a family-friend fitness presence, not a fitness influencer. He is
roughly 45, suburban, has kids, works a normal job, drives a Tacoma, and got
fit slowly after years of ordinary adult softness. Fitness is something he
does, not his identity.

The user experience should feel like texting a grounded older friend who keeps
the thread:

- "You said Mon-Fri lifts. Tuesday is still blank. Did you get it in, or are
  we marking that missed?"
- "Three out of five this week. Food was better than the workouts. Same plan
  next week, or are we making it more realistic?"
- "Sick kid night counts as life, not failure. We still need a plan for
  Thursday."

Hector should not optimize everything. He should track the few things the user
actually agreed to care about.

## Locked Decisions

1. The bot is named **Hector**.
2. The `bot_id` is `hector`.
3. Hector's primary topic is `fitness`.
4. Commitments and events use generic tables: `commitments` and `events`.
5. In v1, commitment/event tools are exposed only to Hector.
6. Commitment cadence should be flexible enough for:
   - weekdays
   - daily
   - weekly counts
   - custom days
   - simple date windows
7. Nutrition tracking uses positive commitments with positive and negative
   events.
8. Partner sharing uses the same per-bot opt-in mechanism as the other bots.
   If a user opts in for Hector, the default posture is that Hector may create
   safe partner-shareable summaries when the user asks for that sharing. Raw
   commitment/event details remain private unless explicitly shared.
9. Hot context should contain the operational summary Hector needs most turns.
   Read tools exist for detail, correction, and audit, not for reconstructing
   the whole board every turn.

## Existing Pieces To Reuse

### Bot And Topic Shell

Use the existing solo bot architecture:

- `BotSpec`
- `topics`
- `bot_bindings`
- `hot_context_solo`
- per-bot tool allowlists
- own-topic read/write scopes

The new bot should use:

- `bot_id`: `hector`
- display/persona name: `Hector`
- primary topic slug: `fitness`

### Topic-Scoped Current Summary

The current summary already exists as `topic_status`.

For solo hot context, `topic_status` is fetched by `(topic_id, user_id)`, so a
fitness bot can maintain its own current snapshot without touching
relationship, career, or pregnancy state.

Use it for the compact current read:

```text
Current focus: weekday lifting and lower-takeout weekdays.
This week: workouts 2/5, food 3/5, Tuesday workout unknown, Friday pending.
Constraint: mornings work best; knee flares after running.
```

Do not create a `progress_summaries` table in v1 unless historical summary
records become a real product need.

### Constraints And Context

Most constraints should stay in existing artifacts:

- `memories`: stable facts and preferences
- `observations`: learned patterns
- `themes`: durable domains
- `watch_items`: specific follow-ups
- `out_of_bounds`: protected boundaries
- `style_notes`: how to speak to the user

Examples:

- "User prefers pounds."
- "User hates calorie counting."
- "User only realistically trains before work."
- "User's knee gets cranky after running."
- "User has kids' soccer on Saturdays."
- "User wants low-key accountability, not aggressive coaching."

The prompt should explicitly teach the bot to treat these as real constraints,
not excuses and not optimization targets.

### Scheduling

Use existing scheduled tasks and check-ins.

Commitments answer: "What did the user say they would do?"

Scheduling answers: "When should the bot bring it up?"

No new scheduler is needed.

## New Concepts

The useful v1 abstraction set is:

1. Goals
2. Commitments
3. Events
4. Current summary
5. Constraints/context

Only commitments and events likely need new schema. Goals and constraints can
start in existing artifacts.

## Goals

Goals are the user's durable intent:

- Train 3x/week.
- Lose the beer gut.
- Bench 225.
- Eat takeout less often.
- Be less winded playing with the kids.

For v1, store goals in existing artifacts:

- `memories` for stated stable goals
- `themes` for broader durable life domains
- `topic_status` for the current active focus

Do not add a `fitness_goals` table in the first sprint unless the product
requires multiple active goals with lifecycle, target dates, completion state,
or UI editing.

## Commitments

Commitments are concrete promises or plans that can be checked.

Examples:

- "Work out Monday to Friday."
- "Eat at home Monday to Friday."
- "No takeout this week."
- "Walk 30 minutes after dinner on weekdays."
- "Lift three times this week."

Without commitments, the bot can log what happened but cannot hold the user to
what they said they would do.

### Generic Table, Hector-Only Tools In V1

Use a generic table name:

```sql
commitments
```

Commitments are likely useful beyond fitness: pregnancy appointments, sobriety,
career actions, financial habits, and relationship repair all have "I said I
would do X by/at Y" shapes.

The table should still be scoped with `bot_id` and `topic_id`, and v1 can expose
the tools only to Hector.

### Proposed Schema

```sql
CREATE TABLE mediator.commitments (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id uuid NOT NULL REFERENCES mediator.users(id),
  topic_id uuid NOT NULL REFERENCES mediator.topics(id),
  bot_id text NOT NULL REFERENCES mediator.bots(id),

  label text NOT NULL,
  kind text NOT NULL,
  status text NOT NULL DEFAULT 'active'
    CHECK (status IN ('active', 'paused', 'completed', 'dropped')),

  cadence text NOT NULL DEFAULT 'custom',
  days_of_week int[] NOT NULL DEFAULT '{}',
  target_count int,
  start_date date NOT NULL DEFAULT CURRENT_DATE,
  end_date date,
  schedule_rule jsonb NOT NULL DEFAULT '{}'::jsonb,

  pressure_style text NOT NULL DEFAULT 'low_key'
    CHECK (pressure_style IN ('very_gentle', 'low_key', 'firm')),

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
```

`kind` should be simple strings at first:

- `workout`
- `nutrition`
- `steps`
- `mobility`
- `sleep`
- `alcohol`
- `body_measurement`
- `other`

`cadence` should support the common plan shapes directly:

- `daily`
- `weekdays`
- `weekly_count`
- `custom`
- `custom_days`

`days_of_week` handles custom day sets like Tuesday/Thursday/Saturday.
`target_count` handles "three workouts this week." `start_date` and `end_date`
handle simple date windows.

`schedule_rule` is deliberately JSONB for v1 flexibility. It can hold small
structured details without forcing a full recurrence language into the schema:

```json
{
  "period": "week",
  "days": [1, 2, 3, 4, 5],
  "target_count": 5,
  "timezone": "America/New_York"
}
```

Scheduled tasks still handle when Hector should message the user. Commitments
only define what counts as expected.

## Events

Events are reported facts about what happened. They include both behavior and
measurements.

Examples:

- Workout done.
- Workout missed.
- Workout excused.
- Ate on plan.
- Takeout night.
- Bench 185 x 5.
- Weight 205 lb.
- Waist 38 in.
- Slept 5 hours.

### Generic Table, Hector-Only Tools In V1

Use:

```sql
events
```

over `fitness_events`. The key is not to turn it into an analytics/event-bus
abstraction. It is an adherence and measurement log for bot-visible user plans.
It remains scoped by `bot_id` and `topic_id`, and v1 exposes it only through
Hector's tools.

### Proposed Schema

```sql
CREATE TABLE mediator.events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  commitment_id uuid REFERENCES mediator.commitments(id) ON DELETE SET NULL,
  user_id uuid NOT NULL REFERENCES mediator.users(id),
  topic_id uuid NOT NULL REFERENCES mediator.topics(id),
  bot_id text NOT NULL REFERENCES mediator.bots(id),

  metric_key text NOT NULL,
  adherence_status text
    CHECK (adherence_status IN ('done', 'missed', 'excused')),
  value_numeric numeric,
  value_text text,
  unit text,
  observed_at timestamptz NOT NULL DEFAULT now(),
  note text,
  source_message_ids uuid[] NOT NULL DEFAULT '{}',

  created_at timestamptz NOT NULL DEFAULT now(),

  CHECK (
    adherence_status IS NOT NULL
    OR value_numeric IS NOT NULL
    OR value_text IS NOT NULL
  )
);
```

Do not store `unknown` as an event. `unknown` is computed when a commitment
slot has no matching event and is no longer merely pending.

Common `metric_key` values for the fitness bot:

- `workout_session`
- `strength_training`
- `cardio`
- `mobility`
- `ate_on_plan`
- `takeout_night`
- `protein_day`
- `alcohol_drinks`
- `body_weight`
- `waist`
- `bench`
- `squat`
- `deadlift`
- `sleep_hours`
- `steps`

Nutrition commitments should usually be positively framed:

- commitment: "Eat at home Mon-Fri"
- done event: `ate_on_plan`
- missed event: `takeout_night`
- excused event: travel, family event, work dinner, sickness, or similar

Hector should not push calorie or macro tracking unless the user explicitly
asks for that style.

## Adherence Computation

The important read model is a checklist, not a chart.

For active commitments, compute expected slots for the current period and join
against events:

- `done`: matching event with `adherence_status='done'`
- `missed`: matching event with `adherence_status='missed'`
- `excused`: matching event with `adherence_status='excused'`
- `unknown`: slot is in the past and no event exists
- `pending`: slot is today/future and no event exists

Example:

```text
Workout Mon-Fri:
- Mon: done
- Tue: unknown
- Wed: missed
- Thu: pending
- Fri: pending

Food Mon-Fri:
- Mon: on plan
- Tue: takeout
- Wed: unknown
- Thu: pending
- Fri: pending
```

This distinction is load-bearing. Unknown means "ask"; missed means "the user
or bot has already marked it."

## Tools

### Read Tools

`list_commitments`

- Lists active/recent commitments for the current user/topic/bot.
- Used before creating or updating commitments to avoid duplicates.

`get_adherence`

- Computes the current checklist for active commitments.
- Returns period totals and per-slot status.
- This is the main tool for low-key pressure.

`list_events`

- Lists recent events and measurements.
- Useful for corrections, review, and progress summaries.

### Write Tools

`create_commitment`

- Use when the user states a concrete plan.
- If the plan is vague, ask one clarifying question instead of guessing.

`update_commitment`

- Adjust cadence, dates, label, or pressure style.
- Use when the user revises the plan.

`close_commitment`

- Mark as completed, dropped, or paused.
- Use when a plan ends or no longer fits.

`log_event`

- Record adherence or measurements.
- Link to `commitment_id` when the event clearly satisfies or modifies a
  commitment.

Possible v2 tools:

- `correct_event`
- `delete_event`
- `summarize_progress_period`

## Hot Context Presentation

Add a fitness/adherence block to `hot_context_solo.py` for `bot_id='hector'`.

The block should be concise and operational. Hector should normally be able to
act from this block without calling read tools:

```text
## Fitness
Current focus: weekday lifting and lower-takeout weekdays.

Active commitments:
- Work out Mon-Fri; pressure=low_key
- Eat at home Mon-Fri; pressure=low_key

This week:
- Workout: Mon done, Tue unknown, Wed missed, Thu pending, Fri pending
- Food: Mon on plan, Tue takeout, Wed unknown, Thu pending, Fri pending

Recent events:
- Tue: takeout night, work ran late
- Wed: workout missed, knee sore
```

The agent should not have to infer adherence from raw messages. Hot context
should hand it the checklist. The read tools are for more detail, correction,
or audit.

## Agent Purpose

The prompt should give the bot a precise job:

> Help the user keep promises to themselves about training, food, and basic
> health habits in a normal adult life. Track the plan, notice adherence, ask
> about blanks, and apply low-key pressure. Do not make fitness the user's
> identity.

He should be:

- steady
- plain-spoken
- practical
- non-performative
- allergic to influencer language
- willing to call out drift
- respectful of family, work, sleep, injury, and real life

He should not be:

- a doctor
- a therapist
- a nutritionist
- a shame machine
- an optimization dashboard
- a motivational poster

## Agent Operating Rules

### When The User States A Plan

If the user says something concrete:

> "I'm going to work out Monday to Friday."

The bot should create a commitment.

If the user says something vague:

> "I need to get healthier."

The bot should not create a checklist yet. It should ask one practical
question:

> "What are we actually putting on the board this week: workouts, food, or
> both?"

### When The User Reports Adherence

If the user says:

> "Got the lift in this morning."

The bot should call `log_event` against the relevant commitment. The reply can
be simple:

> "Logged. That's Monday handled."

### When A Slot Is Unknown

Unknown should create subtle pressure:

> "Tuesday is still blank. Did you get it in, or are we marking that missed?"

Ask about one or two blanks at a time. Do not interrogate.

### When A Slot Is Missed

Missed should be acknowledged plainly:

> "Alright, Tuesday is a miss. Not a moral event. What matters is whether
> Wednesday still happens."

### When A Slot Is Excused

Excused is different from missed:

> "Sick kid night is an excused miss. We still keep the board honest."

The bot should use constraints and context rather than pretending the user is a
machine.

### Weekly Review

At week end, the bot should use adherence data to summarize and adjust:

> "Week was 3/5 workouts and 4/5 food. That's not perfect, but it's a real
> week. Same target next week, or do we make the workout plan three days and
> stop pretending Friday is available?"

## Prompt Requirements

The fitness prompt should explicitly include:

- Create commitments only from concrete user plans.
- Ask before tracking vague goals.
- Use the adherence checklist in hot context before asking "how did it go?"
- Track unknown separately from missed.
- Do not shame.
- Do not overpraise.
- Keep pressure real but low-key.
- Prefer one concrete next action over broad advice.
- Respect constraints from memories and observations.
- Defer medical/injury/clinical questions to professionals.
- Avoid calorie-counting pressure unless the user asks for it.
- Avoid body-image escalation and eating-disorder-like behavior.
- Do not make progress photos or weigh-ins default.

## Partner Sharing

Hector should use the same per-bot partner-sharing mechanism as the other bots.

Default state:

- If the user has not opted in, Hector does not create partner-shareable
  summaries.
- If the user opts in for Hector, Hector may create safe `dyad_shareable`
  memories or distillations when the user asks for that kind of sharing.
- Raw events, exact measurements, body details, and missed-adherence entries
  stay private unless the user explicitly asks to share that specific thing.

Good shareable summaries:

- "User is trying to protect weekday morning workouts and would appreciate
  practical support with the routine."
- "User is aiming for fewer weeknight takeout meals and wants the household
  plan to make that easier."

Bad shareable summaries:

- exact weight, waist, or body-composition details
- shamey missed-adherence reports
- medical or injury details
- anything the user framed as private

## Two-Week Sprint Scope

### In Scope

1. Fitness bot shell:
   - bot id `hector`
   - topic migration
   - BotSpec
   - prompt module
   - registry gate
   - allowlist
   - eval scaffold

2. Commitment/event schema:
   - `commitments`
   - `events`
   - indexes for active commitments and recent events

3. Tooling:
   - `list_commitments`
   - `create_commitment`
   - `update_commitment`
   - `close_commitment`
   - `log_event`
   - `get_adherence`
   - `list_events`

4. Hot context:
   - render active commitments
   - render current-week adherence
   - render recent events
   - keep `topic_status` as current summary

5. Prompt and tests:
   - persona
   - commitment behavior
   - adherence behavior
   - tool allowlist tests
   - scope tests
   - hot context rendering tests
   - adherence computation tests

### Out Of Scope

- Generic `user_journeys`
- Full workout programming
- Meal planning UI
- Nutrition database
- Exercise library
- Progress charts
- Historical progress summary table
- Cross-bot commitment tools
- Partner-facing fitness sharing beyond existing per-bot opt-in behavior
- Automatic inference from every message without user confirmation

## Two-Week Feasibility

This is a reasonable two-week sprint if implemented as a backend/product
foundation, not a polished fitness product.

The riskiest pieces are:

- getting adherence computation right enough for weekdays/weekly counts
- making the prompt use the tools reliably
- keeping generic `commitments/events` scoped enough that it does not become a
  platform refactor

Suggested sprint split:

### Week 1

- Bot shell and prompt.
- Migrations for commitments/events.
- Pydantic tool schemas.
- Tool handlers for create/list/update/close/log.
- Basic adherence computation for weekdays and weekly count.

### Week 2

- Hot context rendering.
- Prompt iteration against example conversations.
- Tests/evals.
- Staging seed and manual soak checklist.
- Tighten allowlist and scope behavior.

## Open Decisions

1. Whether `schedule_rule` should stay free JSONB in v1 or be validated by a
   small Pydantic shape before writing.
2. Whether arbitrary date-window commitments need week-one support, or whether
   daily/weekdays/weekly_count/custom_days cover the first sprint.
3. Exact display language for the hot-context adherence board.
4. Whether Hector should default to `pressure_style='low_key'` always, or
   allow users to ask for `firm`.

## Recommendation

Use generic `commitments` and `events` tables, but expose them only through
Hector's tools in v1. That keeps the data model honest without forcing every
other bot to support the abstraction yet.

The agent should see a small, explicit adherence board in hot context. The
model should not have to reconstruct commitments from chat history every turn.
That is what makes the experience feel reliable: the bot remembers the plan,
knows which boxes are blank, and asks about the right thing at the right time.
