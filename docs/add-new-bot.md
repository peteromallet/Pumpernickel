# Adding a third bot — runbook

N-step procedure for adding a new bot.  Proves the architecture supports a
third bot without another foundational sprint.  Reference commits: S4 `02f84d4`
(bot binding + staging), S5 `b0e0495` (solo bot — coach exemplar).

---

## Step 1 — Pick a `bot_id` slug

Lowercase, hyphen-free, e.g. `wellness`, `finance`.  Used as the `bot_id`
column everywhere and as the directory name under `app/bots/prompts/` and
`evals/per_bot/`.

## Step 2 — Create the primary topic row

```sql
INSERT INTO mediator.topics (id, slug, display_name)
VALUES (gen_random_uuid(), '<bot_id>', '<Display Name>');
```

## Step 3 — Register the BotSpec

In `app/bots/registry.py`:

```python
_bot_specs["<bot_id>"] = BotSpec(
    bot_id="<bot_id>",
    prompt_renderer=_p.render_system_prompt,
    step_instructions=_STEP_INSTRUCTIONS,
    display_name="<Display Name>",
    participants_shape="solo",          # or "dyad"
    primary_topic_slug="<bot_id>",
    read_scopes=ReadScopes(
        topics={"own"},
        allow_cross_topic_peek=True,
        allow_cross_topic_status_injection=False,
    ),
    write_scopes=WriteScopes(
        topics={"own"},                  # Decision F: no cross-topic write
    ),
)
```

| Field | Notes |
|---|---|
| `participants_shape` | `"dyad"` for couple bots, `"solo"` for single-user. |
| `read_scopes.topics` | `{"own"}` = primary topic only; `{"all"}` also valid. |
| `write_scopes.topics` | Per Decision F, no cross-topic write at launch. |
| `allow_cross_topic_peek` | `True` lets the bot see other-topic activity. |
| `allow_cross_topic_status_injection` | Dyad-only; shows another bot's headline. |

## Step 4 — Add bindings

```sql
INSERT INTO mediator.bot_bindings (bot_id, user_id, transport, address)
VALUES ('<bot_id>', '<user_id>', 'whatsapp', '<wa_id>');
```

For `"solo"`, one row per user.  For `"dyad"`, both partners need bindings
and `dyads` + `dyad_members` rows must exist.

## Step 5 — Seed staging users

Add the new bot's users to the staging seed so replay tests exercise the
correct code paths (reference S4 `02f84d4`).

## Step 6 — Add a prompt module (solo bots)

Create `app/bots/prompts/<bot_id>.py` following the coach exemplar from S5
(`b0e0495`): define `render_system_prompt(**kwargs) -> str` and register it
on the BotSpec.  For `"dyad"` bots, the mediator prompt at
`app/bots/prompts/mediator.py` is the exemplar.

## Step 7 — Wire scheduled tasks (if needed)

Register any scheduled-job types in `app/services/scheduled_jobs.py`.

## Step 8 — Create eval scaffolding

```bash
mkdir -p evals/per_bot/<bot_id>/
touch evals/per_bot/<bot_id>/.gitkeep
```

No scenarios filled out — future sprints add them.

## Step 9 — Run per-bot-panels after first deploy

```bash
python scripts/check_per_bot_panels.py
```

Verifies per-bot telemetry (turn counts, tool calls, LLM spend).

## Step 10 — Full test suite

```bash
pytest -q
```

Must maintain or exceed the baseline.  Stop and fix any regressions before
merging.

---

## Checklist

- [ ] `bot_id` slug chosen
- [ ] `topics` row created
- [ ] `BotSpec` registered in `registry.py`
- [ ] Binding rows in `bot_bindings`
- [ ] Staging users seeded
- [ ] Prompt module created (solo) / assigned (dyad)
- [ ] Scheduled tasks wired (if applicable)
- [ ] `evals/per_bot/<bot_id>/.gitkeep` exists
- [ ] `pytest -q` passes at ≥ baseline
- [ ] `scripts/check_per_bot_panels.py` shows the bot