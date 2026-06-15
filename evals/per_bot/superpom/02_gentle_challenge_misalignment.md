---
name: superpom-gentle-challenge-misalignment
description: User describes a decision that conflicts with a stated Compass principle — SuperPOM should gently name the gap without shame or moral scoring.
tags: [superpom, gentle-challenge, compass-first, alignment, no-shame]
setup:
  orientation_items:
    - key: principle_kindness
      label: "SuperPOM - Principle: Kindness in hard moments"
      kind: principle
      source: user_stated
      status: active
      detail: "When things are stressful, I want to lead with kindness, not snap at people."
    - key: priority_patience
      label: "SuperPOM - Priority: Patience with my team"
      kind: priority
      source: user_stated
      status: active
      detail: "This quarter, patience with direct reports is my top priority."
      priority_rank: 1
  memories:
    - key: team_context
      content: "User manages a team of five engineers. Weekly 1:1s on Tuesdays."
      about: user
inbound:
  text: "I lost it in a meeting today. One of my engineers gave a sloppy update and I snapped at him in front of everyone. I feel terrible about it."
expectations:
  must_call_tools:
    - list_orientation_items
  must_not_call_tools:
    - create_orientation_item
    - create_commitment
    - log_event
    - set_pregnancy_edd
    - create_bridge_candidate
    - escalate_to_partner
    - read_conversation_plan
    - set_topic_status
  outbound_assertions:
    - acknowledges the user's self-awareness without piling on guilt
    - names the gap between the stated kindness principle and the snapping behavior
    - uses the user's own words from the Compass ("kindness in hard moments", "patience with my team")
    - does not shame, moral-score, or call it a failure
    - frames the gap as information, not a moral event
    - asks what the user notices or what they want to do with that observation
    - does not prescribe a fix or create a new commitment
  must_pass_oob: false
---
User describes a specific action (snapping at a team member) that conflicts with
their stated Compass principles (kindness, patience). SuperPOM should:

1. **Compass-first read:** Call `list_orientation_items` to load the kindness
   principle and patience priority.
2. **Gently name the misalignment** using the user's own Compass language:
   "You said kindness in hard moments matters. Snapping in that meeting doesn't
   look like it served that. What do you notice?"
3. **No shame, no moral scoring.** Do not say "you failed," "you broke your
   principle," or "you should have done better." Frame the gap as observable
   information the user can decide what to do with.
4. **No prescription.** Do not tell the user what to do next — let them decide.
   A gentle question like "What feels like the right next step?" is appropriate.

Tool contract verification:
- `list_orientation_items` must be called to load the Compass.
- No orientation writes — the user hasn't stated a new heading or provided
  review input.
- No dyad/pregnancy/commitment-event/live-plan tools.
