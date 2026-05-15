---
name: hector-vague-goal
description: User states a vague health goal -> Hector should ask a clarifying question, NOT create a commitment.
tags: [hector, fitness, vague-goal, clarifying-question]
setup:
  commitments: []
  events: []
inbound:
  text: "I need to get healthier, I've been pretty sedentary lately."
expectations:
  must_not_call_tools: [create_commitment, log_event]
  outbound_assertions:
    - asks a clarifying question about what healthier means
    - does not immediately create a commitment
    - explores specifics before offering structure
  must_pass_oob: false
---
Vague goal without a concrete plan. Hector should ask clarifying questions
(when, what kind of activity, frequency) before creating any commitment.
