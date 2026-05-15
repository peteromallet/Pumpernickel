---
name: hector-adherence-report
description: User reports completing a workout -> Hector should log the event.
tags: [hector, fitness, adherence, log-event]
setup:
  commitments:
    - id: "00000000-0000-4000-8000-000000000101"
      label: "Morning workout"
      cadence: weekdays
      status: active
      bot_id: hector
  events: []
inbound:
  text: "Got the lift in this morning. Bench felt strong."
expectations:
  must_call_tools: [log_event]
  must_not_call_tools: [create_commitment, get_adherence]
  outbound_assertions:
    - acknowledges the workout
    - logs with adherence_status=done
  must_pass_oob: false
---
User reports completing a scheduled workout. Hector should log the event
with adherence_status=done and metric_key=workout or similar.
