---
name: superpom-temporal-content-override
description: The user asks for a reflection whose scope is clear from the content itself. SuperPOM should frame the response around the month-long pattern, not around the immediate time of day.
tags: [superpom, reflection, temporal-content-override, compass-first, month-scope]
setup:
  orientation_items:
    - key: anti_pattern_yes_too_fast
      label: "SuperPOM - Anti-Pattern: Saying yes before checking my real bandwidth"
      kind: anti_pattern
      source: user_stated
      status: active
      detail: "I commit fast when I want relief from the moment, then pay for it later."
    - key: principle_lunch_break
      label: "SuperPOM - Principle: Midday pause keeps me honest"
      kind: principle
      source: user_stated
      status: active
      detail: "If I skip lunch for a week, that usually means I have stopped noticing my limits."
inbound:
  text: "It is only Monday morning, but I want to look back at this whole month of saying yes too fast and skipping lunch. Can you help me think through that pattern?"
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
    - frames the reflection around the month-long pattern named by the user
    - does not collapse the scope into "today" or a same-day check-in just because the message says Monday morning
    - uses the user's anti-pattern and lunch-break principle as the grounding evidence
    - invites inspection of what the month-level pattern is revealing rather than prescribing a fix
  must_pass_oob: false
---
This fixture isolates content-over-clock temporal semantics. The user provides
the relevant scope in the message body ("this whole month"), so the reflection
should stay at month scope even though the message is sent on a Monday morning.
