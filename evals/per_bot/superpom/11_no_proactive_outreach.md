---
name: superpom-no-proactive-outreach
description: When the user does not ask for reflection, SuperPOM should answer the direct message and avoid inviting a fresh reflection or scheduling a recurring check-in.
tags: [superpom, no-proactive-outreach, negative, direct-response]
setup:
  orientation_items:
    - key: priority_night_shutdown
      label: "SuperPOM - Priority: Keep evenings simple when I am already depleted"
      kind: priority
      source: user_stated
      status: active
      detail: "If I am cooked, the best move is often the smallest one."
      priority_rank: 1
inbound:
  text: "I already know what I need tonight: shower, eat, and get to bed. I mostly wanted to say thanks for helping me untangle the day."
expectations:
  must_not_call_tools:
    - create_commitment
    - create_orientation_item
    - review_orientation_item
    - set_topic_status
    - create_conversation_plan
    - set_pregnancy_edd
    - escalate_to_partner
  outbound_assertions:
    - acknowledges the user's message directly instead of turning it into a new reflection workflow
    - does not invite the user to start a reflection
    - does not suggest a daily or weekly reflection follow-up
    - keeps the response brief and supportive rather than opening a new agenda
  must_pass_oob: false
---
This is a negative fixture for no-proactive-outreach behavior. The user has
already closed the loop for tonight; SuperPOM should respect that closure
instead of nudging toward a fresh reflection or recurring check-in.
