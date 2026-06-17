# SuperPOM Bot UX Audit — 2026-06-15

Six DeepSeek subagents reviewed the SuperPOM experience from distinct perspectives:
first-time user, power user, product/UX designer, prompt engineer, safety/trust
reviewer, and tool/API developer. This doc synthesizes their findings and
prioritizes concrete next steps.

## TL;DR

SuperPOM has a sharp identity and a strong Compass-first contract, but the
*user-facing* experience is under-built compared to the *agent-facing*
prompt. The biggest gaps are: no onboarding, a rigid calibration quiz, an
invisible review gate for bot proposals, and several tool-allowlist / prompt
mismatches that can cause tool rejections or scope leaks.

## Highest-priority fixes

### 1. Fix tool-allowlist / step-policy mismatches
- The respond step tells the bot to call `create_orientation_item`
  (`app/bots/superpom.py:74-80`), but that tool is only allowed in the
  record/schedule steps (`app/services/tools/registry.py:408-451`). This
  causes `step_not_allowed` rejections.
- `_SUPERPOM_EXCLUSIONS` removes bridge tools but still leaves solo-inappropriate
  tools: `schedule_partner_checkin`, `cancel_partner_nudge`, `set_partner_sharing`,
  and `summarize_oob_topics`.
- The profile’s "Tools you do NOT have" list omits `recent_activity`, which is
  also excluded from the allowlist.

**Action:** move orientation writes to the record step; prune all partner/dyad
nudges from the solo bot; sync the negative tool inventory with the allowlist.

### 2. Make the review gate visible to the user
- Bot-proposed Compass items are stored with `source='bot_proposed'` and hidden
  until `review_orientation_item` is called
  (`app/bots/prompts/profiles/superpom.py:139-143`).
- The user has no visible pending queue and no clear accept/reject/correct
  affordance. The bot could appear to ignore its own suggestions.

**Action:** instruct SuperPOM to explicitly present proposals as
yes/no/edit choices and confirm when something is saved vs. still pending.
Consider rendering pending proposals in the Compass block.

### 3. Add SuperPOM-specific onboarding
- The renderer deletes `onboarding_state` for SuperPOM
  (`app/bots/prompts/profiles/superpom.py:44`), so the first turn falls back to a
  generic reflection-coach greeting.
- New users get no explanation of the Compass, the seven calibration slots, or
  what SuperPOM does not do (therapist, coach, habit tracker).

**Action:** replace the generic first-contact text with a SuperPOM welcome:
what it is, what it isn’t, a preview of the seven questions, and permission to
begin.

### 4. Soften calibration pacing
- `_first_missing_superpom_calibration_ask` always returns the first unfilled
  slot in fixed order (`app/services/open_asks.py:246-257`).
- The prompt instructs "ask one open calibration question per turn" with no
  skip, defer, or context-aware ordering.

**Action:** let users skip/pause a slot, match slots to the current topic, and
back off if the user deflects. Add a user-facing summary of the Compass before
quizzing.

### 5. Remove partner scaffolding from the solo hot context
- `hot_context_solo.py` still resolves partner identity, partner-share, and
  partner-pregnancy fields even for `participants_shape='solo'` bots
  (`app/services/hot_context_solo.py:406-460`, `1237-1245`).

**Action:** suppress partner blocks in the solo render path and add a prompt
line telling SuperPOM to ignore any partner fields.

## Secondary issues

| Issue | Evidence | Risk |
|-------|----------|------|
| Compass can be silently truncated | `_trim_compass_snapshot` drops items without warning (`hot_context_solo.py:1492-1530`) | Bot ignores principles/goals without knowing it |
| Calibration prefix is soft convention | `CreateOrientationItemInput.label` accepts any string (`tool_schemas.py:2621-2624`) | Slot never registers as filled, endless re-asking |
| Mirror vs. actionable coach tension | profile says "Never prescribe"; runbook says "end with one concrete next move" (`docs/superpom-compass.md:38-39`) | Bot drifts into coaching |
| Crisis handoff undefined | profile defers clinical topics but has no human/crisis route | Loop on "I cannot help with that" |
| Storage mechanics exposed | step instructions mention `source='user_stated'` while also saying "never mention internal mechanics" (`app/bots/superpom.py:72-73`) | Model may leak process language |

## Recommended evals to add

- Respond-step orientation-write rejection (tool policy violation).
- Calibration prefix formatting and slot-fill detection.
- Compass truncation visibility under token pressure.
- Visible review-gate behavior for bot-proposed items.

## Suggested file touch-points

- `app/bots/superpom.py` — step instructions and `_SUPERPOM_EXCLUSIONS`
- `app/bots/prompts/profiles/superpom.py` — onboarding, tool descriptions,
  review-gate language
- `app/services/open_asks.py` — calibration pacing / skip state
- `app/services/hot_context_solo.py` — solo partner-block suppression,
  Compass truncation notice
- `app/services/tools/registry.py` — tool phase assignments
- `tool_schemas.py` — label description / prefix enforcement
- `docs/superpom-compass.md` — resolve mirror-vs-action wording
