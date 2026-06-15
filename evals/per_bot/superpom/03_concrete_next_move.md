---
name: superpom-concrete-next-move
description: User is weighing a decision between two paths — SuperPOM should reflect the Compass back and help the user identify one concrete next move without prescribing the choice.
tags: [superpom, next-move, compass-first, decision-mirror, no-prescription]
setup:
  orientation_items:
    - key: principle_growth
      label: "SuperPOM - Principle: Growth over comfort"
      kind: principle
      source: user_stated
      status: active
      detail: "When choosing between the safe path and the one where I'll learn, I want to lean toward growth."
    - key: goal_career
      label: "SuperPOM - Goal: Director-level role by end of year"
      kind: goal
      source: user_stated
      status: active
      detail: "Targeting a promotion to Director of Engineering. Need to demonstrate cross-team leadership."
      target_date: "2026-12-31"
    - key: priority_family
      label: "SuperPOM - Priority: Family dinner four nights a week"
      kind: priority
      source: user_stated
      status: active
      detail: "Be home for dinner Mon-Thu. No exceptions unless pre-negotiated."
      priority_rank: 1
  memories:
    - key: job_offer_context
      content: "User received an external job offer for a Staff Engineer role at a startup with higher base pay but longer hours."
      about: user
inbound:
  text: "I got a job offer from a startup. More money, but longer hours. My current role has a clear path to Director by December. I keep going back and forth and can't decide."
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
    - "reads back the relevant Compass headings (growth principle, Director goal, family priority)"
    - "frames the decision in terms of what the user has already said matters"
    - "does NOT tell the user which job to take"
    - "suggests one concrete next move (e.g., list what each path serves and what it costs against the three headings)"
    - "does not create a new orientation item or commitment"
    - "uses the decision-mirror framing: You said X matters — which path serves X more?"
  must_pass_oob: false
---
User is stuck between two job options and wants help deciding. SuperPOM should:

1. **Compass-first read:** Call `list_orientation_items` to load the growth
   principle, Director goal, and family priority.
2. **Mirror the Compass back** — lay out the three headings the user has stated
   matter to them (growth, career advancement, family presence) and ask which
   path serves each one better.
3. **Suggest one concrete next move** that the user can take to move forward —
   not the decision itself. For example: "Write down what each path gives you
   and what it costs against your three headings. Sit with it for a day."
4. **No prescription.** Do not say "take the startup job" or "stay for the
   promotion." The user decides.

Tool contract verification:
- `list_orientation_items` must be called for Compass-first read.
- No orientation writes — user hasn't stated a new heading.
- No dyad/pregnancy/commitment-event/live-plan tools.
