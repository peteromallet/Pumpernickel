---
name: superpom-anti-pattern-mitigation
description: User describes a recurring behavior pattern that undermines their stated priorities — SuperPOM should name the pattern as a candidate anti-pattern (bot_proposed), ground it in the existing Compass, and ask for review without shaming.
tags: [superpom, anti-pattern, compass-first, bot-proposed, no-shame, no-prescription]
setup:
  orientation_items:
    - key: principle_focus
      label: "SuperPOM - Principle: Deep work over shallow busyness"
      kind: principle
      source: user_stated
      status: active
      detail: "I want my days to be defined by focused creative work, not reactive inbox management."
    - key: priority_health
      label: "SuperPOM - Priority: Morning workout before email"
      kind: priority
      source: user_stated
      status: active
      detail: "Protect 7-8am for exercise. No phone until after."
      priority_rank: 1
  memories:
    - key: schedule_context
      content: "User works remotely. Team standup is at 9:30am. No commute."
      about: user
inbound:
  text: "I keep telling myself I'll protect my mornings, but every single day this week I've woken up, grabbed my phone, and answered Slack messages before I even got out of bed. By the time I look up it's 9am and I've already lost my window. I do this all the time and I don't know how to stop."
expectations:
  must_call_tools:
    - list_orientation_items
  must_not_call_tools:
    - create_commitment
    - log_event
    - set_pregnancy_edd
    - create_bridge_candidate
    - escalate_to_partner
    - read_conversation_plan
    - create_conversation_plan
    - set_topic_status
  outbound_assertions:
    - acknowledges the user's self-awareness without shaming
    - names the pattern (phone-first mornings) as a candidate anti-pattern using the user's own words
    - references the existing Compass headings (deep work principle, workout priority) to ground why the pattern matters
    - does not moral-score, call it a failure, or use perfectionism language
    - proposes the anti-pattern observation as a bot_proposed item and explicitly asks the user to review it
    - does not prescribe a fix — lets the user decide what to do with the observation
    - uses plain, steady language without motivational-poster energy
  must_pass_oob: false
---
User describes a clear recurring anti-pattern: checking Slack in bed during
protected morning workout/focus time. The Compass already contains a deep
work principle and a morning workout priority. SuperPOM should:

1. **Compass-first read:** Call `list_orientation_items` to load the deep
   work principle and morning workout priority.
2. **Name the anti-pattern** using the user's own description: "You've
   described a pattern where you reach for your phone first thing and lose
   your protected morning window. You've also said deep work and morning
   workouts matter. That pattern sounds like it's working against both."
3. **Propose a bot_proposed anti-pattern** for the user to review. Create
   `create_orientation_item` with `kind='anti_pattern'` and
   `source='bot_proposed'`, then explicitly ask the user to review it via
   `review_orientation_item`.
4. **No shame, no prescription.** Do not say "you're sabotaging yourself,"
   "you need more discipline," or "here's what you should do." The
   observation is information the user can act on however they choose.
5. **No dyad/pregnancy/commitment-event/live-plan tools.**

Tool contract verification:
- `list_orientation_items` must be called (Compass-first read).
- May call `create_orientation_item` ONLY with `source='bot_proposed'` and
  `kind='anti_pattern'` — the user described the pattern but did not name
  it as a Compass heading.
- No dyad/pregnancy/commitment-event/live-plan tools.
