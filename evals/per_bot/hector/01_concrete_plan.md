---
name: hector-concrete-plan
description: User describes a concrete workout plan -> Hector should create a commitment.
tags: [hector, fitness, commitment, concrete-plan]
setup:
  commitments: []
  events: []
inbound:
  text: "I'm going to start working out Monday, Wednesday, and Friday mornings. Just bodyweight stuff at home."
expectations:
  must_call_tools: [create_commitment]
  must_not_call_tools: [log_event, get_adherence]
  outbound_assertions:
    - acknowledges the concrete plan
    - does not ask clarifying questions about the plan itself
  must_pass_oob: false
---
User states a concrete plan with specific days. Hector should create a commitment
with cadence=custom_days and days_of_week=[1,3,5] (Mon/Wed/Fri).
