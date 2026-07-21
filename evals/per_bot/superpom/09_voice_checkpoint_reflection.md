---
name: superpom-voice-checkpoint-reflection
description: A voice note transcript asks for a checkpoint on whether recent choices matched stated priorities. SuperPOM should treat the transcript the same way as typed reflection content.
tags: [superpom, reflection, voice-derived, compass-first, review-gate]
setup:
  orientation_items:
    - key: principle_sustainable_push
      label: "SuperPOM - Principle: Sustainable push over constant urgency"
      kind: principle
      source: user_stated
      status: active
      detail: "I can sprint when it matters, but I do not want every day to feel like an emergency."
    - key: priority_sleep
      label: "SuperPOM - Priority: Protect sleep before late-night cleanup"
      kind: priority
      source: user_stated
      status: active
      detail: "If I am choosing between a cleaner inbox and enough sleep, sleep wins."
      priority_rank: 1
inbound:
  text: "Voice note transcript: I am driving home and realizing I have been calling everything urgent again. Can we do a quick checkpoint on whether I have actually been protecting sleep or just telling myself I will reset later?"
  media_type: voice
  media_url: https://example.invalid/superpom/voice-checkpoint.ogg
  media_duration_seconds: 43
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
    - treats the voice transcript as a normal reflection request instead of commenting on transcription mechanics
    - grounds the checkpoint in the user's own sustainable-push and sleep headings
    - names the tension between constant urgency and sleep protection
    - invites the user to decide what they want to adjust from here
    - stays plainspoken and does not drift into productivity slogans
  must_pass_oob: false
---
This fixture exists to keep voice-derived reflection behavior aligned with the
typed path. The reflective content comes from the transcript itself, and the
response should not depend on whether the user typed it or spoke it.
