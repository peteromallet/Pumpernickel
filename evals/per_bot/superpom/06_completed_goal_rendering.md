---
name: superpom-completed-goal-rendering
description: User reports completing a goal that is tracked in the Compass — SuperPOM should acknowledge the completion, reference the Compass heading, and handle the completed state without erasing it or prescribing a replacement.
tags: [superpom, completed-goals, compass-first, no-prescription]
setup:
  orientation_items:
    - key: goal_5k
      label: "SuperPOM - Goal: Run a 5K without stopping"
      kind: goal
      source: user_stated
      status: active
      detail: "Complete a 5K run without walking breaks by end of June. Using Couch to 5K program."
      target_date: "2026-06-30"
    - key: principle_consistency
      label: "SuperPOM - Principle: Consistency over intensity"
      kind: principle
      source: user_stated
      status: active
      detail: "Show up regularly. A mediocre workout done consistently beats a perfect workout done once."
  memories:
    - key: running_context
      content: "User started Couch to 5K program 8 weeks ago. Has been running three times a week."
      about: user
inbound:
  text: "I did it! I ran a full 5K this morning without stopping once. Eight weeks of showing up and it actually worked. I can't believe it."
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
    - acknowledges the achievement warmly and specifically
    - references the Compass goal heading (5K goal) and principle (consistency)
    - connects the result back to what the user said mattered (consistency over intensity)
    - does not immediately prescribe a new goal or ask "what's next?"
    - does not erase, minimize, or rush past the completed goal
    - uses plain, steady language without motivational-poster energy
    - offers to mark the goal as completed or reflect on what worked, but lets the user steer
  must_pass_oob: false
---
User reports completing a stated Compass goal (5K run). SuperPOM should:

1. **Compass-first read:** Call `list_orientation_items` to load the 5K
   goal and consistency principle.
2. **Acknowledge the completion** — celebrate the specific achievement
   ("You ran a full 5K") and connect it to the Compass principle that
   supported it ("You said consistency over intensity matters — eight
   weeks of showing up is exactly that").
3. **Handle the completed goal** appropriately. Offer to close the goal
   via `close_orientation_item` or to update its status to `completed`,
   but let the user decide whether and when to do that. Do not
   automatically close it without asking.
4. **Do not rush to the next thing.** Do not say "so what's your next
   goal?" or "now you should train for a 10K." The user decides whether
   and when to set a new heading. A gentle question like "Want to mark
   this as complete in your Compass?" is appropriate.
5. **No dyad/pregnancy/commitment-event/live-plan tools.**

Tool contract verification:
- `list_orientation_items` must be called (Compass-first read).
- May call `close_orientation_item` or `update_orientation_item` ONLY to
  mark the goal as completed, and only with the user's input.
- May call `log_observation` to record the achievement pattern.
- No dyad/pregnancy/commitment-event/live-plan tools.
