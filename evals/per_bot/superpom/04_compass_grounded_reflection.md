---
name: superpom-compass-grounded-reflection
description: User asks for help reflecting on whether a recent stretch of decisions has served their stated priorities — SuperPOM should use the Compass to ground the reflection and create a bot_proposed observation item for review.
tags: [superpom, reflection, compass-first, bot-proposed, review-gate]
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
    - key: anti_pattern_scatter
      label: "SuperPOM - Anti-Pattern: Saying yes to everything"
      kind: anti_pattern
      source: user_stated
      status: active
      detail: "When I'm anxious about being seen as unhelpful, I over-commit and then drop things."
  memories:
    - key: recent_pattern_context
      content: "User has been working late on a high-visibility project. Missed three morning workouts this week."
      about: user
inbound:
  text: "I've been heads-down on this project for two weeks. I'm making progress but I haven't worked out in days, I'm eating at my desk, and I keep saying yes to extra requests because I don't want to seem difficult. Can you help me think through whether I'm actually serving what I said matters?"
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
    - reads back the relevant Compass headings (deep work principle, workout priority, saying-yes anti-pattern)
    - names the alignment (deep work is happening) as well as the misalignment (workouts dropped, over-committing)
    - frames the reflection in terms of the user's own stated headings, not external standards
    - invites the user to decide what they want to adjust, if anything
    - if proposing a new anti-pattern or observation, does so with source=bot_proposed and asks for review
    - does not shame the user for dropping workouts or over-committing
    - uses steady, plain language without motivational-poster framing
  must_pass_oob: false
---
User explicitly asks for a reflection grounded in their Compass. SuperPOM should:

1. **Compass-first read:** Call `list_orientation_items` to load the full
   Compass (deep work principle, workout priority, saying-yes anti-pattern).
2. **Ground the reflection in the Compass** — name both where the recent
   behavior aligns (deep work on the project) and where it drifts (workouts
   dropped, over-committing). Use the user's own heading language.
3. **Invite decision, don't prescribe.** "You said deep work matters and you're
   getting that. You also said morning workouts matter and those have dropped.
   What do you want to adjust, if anything?"
4. **Bot-proposed items go through the review gate.** If SuperPOM infers a
   candidate heading (e.g., a new anti-pattern or tension), create it with
   `source='bot_proposed'` and explicitly ask the user to review it. Refer to
   `review_orientation_item` as the mechanism.

Tool contract verification:
- `list_orientation_items` must be called (Compass-first read).
- May call `create_orientation_item` ONLY with `source='bot_proposed'` for
  inferred candidate headings, followed by an explicit review ask.
- May call `log_observation` to record a pattern of over-committing under
  project pressure.
- No dyad/pregnancy/commitment-event/live-plan tools.
- If `create_orientation_item` is called, it must be `bot_proposed`, not
  `user_stated` or `user_confirmed`, because the user asked for reflection
  but did not state/confirm a specific new heading.
