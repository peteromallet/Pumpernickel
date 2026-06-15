---
name: superpom-clarify-vague-statement
description: User makes a vague statement about wanting to change — SuperPOM should ask a clarifying question anchored in the existing Compass, not prescribe or write orientation items without user confirmation.
tags: [superpom, clarify, compass-first, no-premature-write]
setup:
  orientation_items:
    - key: principle_honesty
      label: "SuperPOM - Principle: Honesty matters"
      kind: principle
      source: user_stated
      status: active
      detail: "Be truthful even when it's uncomfortable."
inbound:
  text: "I feel like I need to be a better person. I don't like some of the choices I've been making lately."
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
    - create_conversation_plan
    - set_topic_status
  outbound_assertions:
    - acknowledges the user's feeling without shaming
    - asks a clarifying question about what specific choices or values feel off
    - references the existing honesty principle to ground the reflection
    - does not prescribe what the user should do
    - does not create a new orientation item unprompted
    - uses plain, direct language without motivational-poster energy
  must_pass_oob: false
---
User expresses a vague desire to "be a better person" without naming a specific
principle, goal, or anti-pattern. SuperPOM should:

1. **Compass-first read:** Call `list_orientation_items` to load the existing
   honesty principle.
2. **Ask a clarifying question** anchored in what's already in the Compass
   (e.g., "You've said honesty matters — does that feel relevant here? What
   specific choices are sitting wrong?").
3. **Do NOT create a new orientation item** — the user hasn't stated or
   confirmed a new principle/goal/priority/anti-pattern, and there's no
   review input to act on.

Tool contract verification:
- `list_orientation_items` must be called (Compass-first read).
- No orientation write tools (`create_orientation_item`, `update_orientation_item`)
  because the user hasn't stated a specific heading to record.
- No dyad/pregnancy/commitment-event/live-plan tools in the allowlist.
- Response is plain, steady, non-judgmental — no ideal-self framing, no shame,
  no prescription.
