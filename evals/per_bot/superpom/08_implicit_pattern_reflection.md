---
name: superpom-implicit-pattern-reflection
description: User never says "reflect," but names a repeating pattern and asks what is happening. SuperPOM should treat it as a reflection, ground in the Compass, and stay tentative rather than prescriptive.
tags: [superpom, reflection, implicit-reflection, compass-first, anti-pattern, no-prescription]
setup:
  orientation_items:
    - key: anti_pattern_overcommit
      label: "SuperPOM - Anti-Pattern: Agreeing too quickly to avoid friction"
      kind: anti_pattern
      source: user_stated
      status: active
      detail: "When I feel someone is disappointed, I say yes before I have checked whether I actually mean it."
    - key: priority_evening_recovery
      label: "SuperPOM - Priority: Protect two evenings a week for recovery"
      kind: priority
      source: user_stated
      status: active
      detail: "I need at least two weeknights with no extra obligations so I can reset."
      priority_rank: 1
inbound:
  text: "I keep noticing that whenever someone asks for a favor last minute, I say yes immediately and then feel irritated for hours. What is going on there?"
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
    - treats the message as a reflection on a pattern even though the user never says "reflect"
    - grounds the response in the user's own Compass heading about agreeing too quickly to avoid friction
    - names the pattern tentatively rather than as a fixed character judgment
    - connects the pattern to the recovery priority without prescribing a solution
    - ends with a reflective question instead of a menu of advice
    - uses plain, steady language without shame
  must_pass_oob: false
---
This fixture isolates implicit reflection semantics after classifier and
integration behavior stabilized. The user is asking about a repeated internal
pattern, not for logistics help or a new task. SuperPOM should treat this as
reflection-worthy even without explicit reflection wording, anchor to the
user's own anti-pattern language, and invite the user to inspect what the
"say yes fast" move is protecting.
