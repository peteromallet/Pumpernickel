"""Tests for reflection derivation candidates (T12).

Covers:
- Derivation kinds strictly limited to memory, observation, distillation, orientation.
- Forbidden kinds: actions, tasks, reminders, follow-ups rejected.
- Deterministic eligibility gates (kind, confidence, evidence, summary, assertion).
- Confidence/reason capture.
- Idempotent candidate production.
- Rejected candidates with reasons.
- Semantic boundaries: non-knowledge candidates rejected.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.reflections.derivation import (
    AssertionSource,
    DerivationCandidate,
    DerivationEngine,
    DerivationKind,
    DerivationResult,
    EligibilityResult,
    _FORBIDDEN_KINDS,
    check_eligibility,
)


# ── Kind enforcement tests ───────────────────────────────────────────────────


class TestDerivationKinds:
    """Verify derivation kinds are strictly limited to knowledge types."""

    def test_allowed_kinds_are_knowledge_only(self):
        """Only memory, observation, distillation, orientation are valid."""
        allowed = {k.value for k in DerivationKind}
        assert allowed == {"memory", "observation", "distillation", "orientation"}

    def test_forbidden_kinds_are_explicitly_blocked(self):
        """Actions, tasks, reminders, follow-ups are in the forbidden set."""
        assert "action" in _FORBIDDEN_KINDS
        assert "task" in _FORBIDDEN_KINDS
        assert "reminder" in _FORBIDDEN_KINDS
        assert "follow_up" in _FORBIDDEN_KINDS
        assert "follow-up" in _FORBIDDEN_KINDS

    def test_no_forbidden_kind_is_a_valid_derivation_kind(self):
        """No forbidden kind overlaps with allowed DerivationKind values."""
        allowed = {k.value for k in DerivationKind}
        overlap = allowed & _FORBIDDEN_KINDS
        assert not overlap, f"Forbidden kinds overlap with allowed: {overlap}"

    def test_checkin_schedule_nudge_are_forbidden(self):
        """Check-in, schedule, nudge, escalation are all forbidden."""
        assert "checkin" in _FORBIDDEN_KINDS
        assert "schedule" in _FORBIDDEN_KINDS
        assert "nudge" in _FORBIDDEN_KINDS
        assert "escalation" in _FORBIDDEN_KINDS


class TestAssertionSources:
    """Verify assertion sources are valid."""

    def test_assertion_sources(self):
        sources = {s.value for s in AssertionSource}
        assert sources == {"user_explicit", "user_implied", "agent_inferred"}


# ── Eligibility gate tests ───────────────────────────────────────────────────


class TestEligibilityGate:
    """Verify deterministic eligibility gate behavior."""

    # ── Positive: all gates pass ─────────────────────────────────────────

    def test_eligible_memory_candidate(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="I started a new job at Acme Corp",
            confidence=0.9,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is True
        assert len(result.reasons) >= 4  # kind, confidence, evidence, summary, assertion

    def test_eligible_observation_candidate(self):
        cand = DerivationCandidate(
            kind=DerivationKind.observation,
            assertion_source=AssertionSource.user_explicit,
            summary="User consistently mentions feeling tired after meetings",
            confidence=0.8,
            supporting_message_ids=[uuid4(), uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is True

    def test_eligible_distillation_candidate(self):
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Synthesized: work stress correlates with sleep quality complaints",
            confidence=0.6,
            supporting_message_ids=[uuid4(), uuid4()],  # ≥2 for multi-evidence
        )
        result = check_eligibility(cand)
        assert result.eligible is True

    def test_eligible_orientation_candidate(self):
        cand = DerivationCandidate(
            kind=DerivationKind.orientation,
            assertion_source=AssertionSource.user_explicit,
            summary="Goal: improve work-life balance",
            confidence=0.9,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is True

    # ── Negative: kind gate ──────────────────────────────────────────────

    def test_action_kind_rejected(self):
        """Actions cannot be derived from reflections."""
        cand = DerivationCandidate(
            kind="action",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Schedule a check-in",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        assert "forbidden" in result.reasons[0].lower()

    def test_task_kind_rejected(self):
        """Tasks cannot be derived from reflections."""
        cand = DerivationCandidate(
            kind="task",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Do X tomorrow",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False

    def test_reminder_kind_rejected(self):
        """Reminders cannot be derived from reflections."""
        cand = DerivationCandidate(
            kind="reminder",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Remind user about meeting",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False

    def test_follow_up_kind_rejected(self):
        """Follow-ups cannot be derived from reflections."""
        cand = DerivationCandidate(
            kind="follow_up",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Follow up on topic X",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False

    def test_checkin_kind_rejected(self):
        cand = DerivationCandidate(
            kind="checkin",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Check in about topic",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False

    def test_unknown_kind_rejected(self):
        """Unknown kinds that aren't even in the forbidden list should be rejected."""
        cand = DerivationCandidate(
            kind="fantasy",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Something imaginary",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        assert "not a recognized knowledge type" in result.reasons[0]

    # ── Negative: confidence gate ────────────────────────────────────────

    def test_zero_confidence_rejected(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="Something",
            confidence=0.0,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False

    def test_negative_confidence_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            DerivationCandidate(
                kind=DerivationKind.memory,
                assertion_source=AssertionSource.user_explicit,
                summary="Something",
                confidence=-0.1,
                supporting_message_ids=[uuid4()],
            )

    def test_over_one_confidence_rejected(self):
        with pytest.raises(ValueError, match="confidence"):
            DerivationCandidate(
                kind=DerivationKind.memory,
                assertion_source=AssertionSource.user_explicit,
                summary="Something",
                confidence=1.1,
                supporting_message_ids=[uuid4()],
            )

    # ── Negative: evidence gate ──────────────────────────────────────────

    def test_no_evidence_rejected(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="Something I know",
            confidence=0.9,
            supporting_message_ids=[],
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        assert "supporting_message_ids" in result.reasons[0]

    # ── Negative: summary gate ───────────────────────────────────────────

    def test_empty_summary_rejected(self):
        with pytest.raises(ValueError, match="summary"):
            DerivationCandidate(
                kind=DerivationKind.memory,
                assertion_source=AssertionSource.user_explicit,
                summary="",
                confidence=0.9,
                supporting_message_ids=[uuid4()],
            )

    def test_whitespace_only_summary_rejected(self):
        with pytest.raises(ValueError, match="summary"):
            DerivationCandidate(
                kind=DerivationKind.memory,
                assertion_source=AssertionSource.user_explicit,
                summary="   ",
                confidence=0.9,
                supporting_message_ids=[uuid4()],
            )

    # ── Negative: assertion gate ─────────────────────────────────────────

    def test_invalid_assertion_source_rejected(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source="made_up",  # type: ignore[arg-type]
            summary="Something",
            confidence=0.9,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        assert "assertion_source" in result.reasons[0]

    def test_agent_inferred_low_confidence_rejected(self):
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Inferred insight",
            confidence=0.4,  # Below 0.5 threshold for agent_inferred
            supporting_message_ids=[uuid4(), uuid4()],  # ≥2 to pass multi-evidence gate
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        assert "agent_inferred" in result.reasons[0].lower()

    def test_agent_inferred_medium_confidence_passes(self):
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Inferred insight with decent confidence",
            confidence=0.5,
            supporting_message_ids=[uuid4(), uuid4()],  # ≥2 to pass multi-evidence gate
        )
        result = check_eligibility(cand)
        assert result.eligible is True


# ── DerivationEngine tests ───────────────────────────────────────────────────


class TestDerivationEngine:
    """Verify the engine produces candidates conservatively and idempotently."""

    def test_produces_candidates_from_rich_input(self):
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4(), uuid4()],
            plaintext_summary="User reflected on work stress and sleep patterns.",
            extracted_topics=["work stress", "sleep"],
            explicit_user_statements=[
                "I've been really stressed at work",
                "My sleep has been terrible",
                "I think the stress is affecting my rest",
            ],
            detected_sentiment="negative",
        )
        assert isinstance(result, DerivationResult)
        assert len(result.candidates) > 0
        # All candidates should be DerivationCandidate instances
        for c in result.candidates:
            assert isinstance(c, DerivationCandidate)
            assert c.kind in DerivationKind
            assert c.confidence > 0.0
            assert c.supporting_message_ids

    def test_idempotent_production(self):
        """Same input → same candidates (same kinds, same summaries)."""
        engine = DerivationEngine()
        args = dict(
            source_message_ids=[uuid4()],
            plaintext_summary="Reflection on health.",
            extracted_topics=["exercise"],
            explicit_user_statements=["I've been running every morning"],
            detected_sentiment="positive",
        )
        result1 = engine.produce_candidates(**args)
        result2 = engine.produce_candidates(**args)

        assert len(result1.candidates) == len(result2.candidates)
        for c1, c2 in zip(result1.candidates, result2.candidates):
            assert c1.kind == c2.kind
            assert c1.summary == c2.summary
            assert c1.confidence == c2.confidence

    def test_no_topics_no_orientation(self):
        """Without extracted topics, no orientation candidates are produced."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4()],
            plaintext_summary="Basic reflection.",
            extracted_topics=[],
            explicit_user_statements=[],
        )
        kinds = {c.kind for c in result.candidates}
        assert DerivationKind.orientation not in kinds

    def test_no_statements_no_memory(self):
        """Without explicit statements, no memory candidate."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4()],
            plaintext_summary="No statements here.",
            extracted_topics=["topic"],
            explicit_user_statements=[],
        )
        kinds = {c.kind for c in result.candidates}
        assert DerivationKind.memory not in kinds

    def test_single_statement_no_observation(self):
        """Need at least 2 statements for an observation pattern."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4()],
            plaintext_summary="Single statement.",
            extracted_topics=["topic"],
            explicit_user_statements=["Just one thing"],
        )
        kinds = {c.kind for c in result.candidates}
        assert DerivationKind.observation not in kinds

    def test_no_sentiment_no_distillation(self):
        """Without sentiment, distillation is not produced."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4()],
            plaintext_summary="Reflection.",
            extracted_topics=["work"],
            explicit_user_statements=["I work a lot"],
            detected_sentiment=None,
        )
        kinds = {c.kind for c in result.candidates}
        assert DerivationKind.distillation not in kinds

    def test_rejected_candidates_are_tracked(self):
        """When a candidate is generated but fails eligibility, it goes to rejected."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4()],
            plaintext_summary="Reflection.",
            extracted_topics=["work"],
            explicit_user_statements=["I work a lot"],
            detected_sentiment="neutral",
        )
        # Some candidates might be rejected (e.g. topic-based orient with low confidence)
        # The rejected list should contain (candidate, eligibility_result) tuples
        for rejected_cand, eligibility_res in result.rejected:
            assert isinstance(rejected_cand, DerivationCandidate)
            assert isinstance(eligibility_res, EligibilityResult)
            assert eligibility_res.eligible is False

    def test_empty_input_produces_empty_candidates(self):
        """No evidence → no candidates."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4()],
            plaintext_summary="",
            extracted_topics=[],
            explicit_user_statements=[],
        )
        assert len(result.candidates) == 0


# ── DerivationCandidate model tests ──────────────────────────────────────────


class TestDerivationCandidateModel:
    """Verify DerivationCandidate validation."""

    def test_valid_candidate_creation(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="I live in Berlin",
            confidence=0.95,
            supporting_message_ids=[uuid4()],
            eligibility_reasons=["explicit statement"],
            detail={"location": "Berlin"},
        )
        assert cand.kind == DerivationKind.memory
        assert cand.confidence == 0.95
        assert len(cand.supporting_message_ids) == 1

    def test_confidence_must_be_in_range(self):
        with pytest.raises(ValueError, match="confidence"):
            DerivationCandidate(
                kind=DerivationKind.memory,
                assertion_source=AssertionSource.user_explicit,
                summary="Valid",
                confidence=1.5,
                supporting_message_ids=[uuid4()],
            )

    def test_summary_must_not_be_empty(self):
        with pytest.raises(ValueError, match="summary"):
            DerivationCandidate(
                kind=DerivationKind.memory,
                assertion_source=AssertionSource.user_explicit,
                summary="",
                confidence=0.5,
                supporting_message_ids=[uuid4()],
            )

    def test_frozen_after_creation(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="Immutable",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        with pytest.raises(Exception):
            cand.confidence = 0.8  # type: ignore[misc]


# ── Semantic boundary tests ──────────────────────────────────────────────────


class TestSemanticBoundaries:
    """Verify derivation cannot produce action/task/reminder/follow-up candidates."""

    def test_no_action_candidate_emitted_by_engine(self):
        """The engine should never emit an action-like candidate."""
        engine = DerivationEngine()
        result = engine.produce_candidates(
            source_message_ids=[uuid4()],
            plaintext_summary="Do something",
            extracted_topics=["action item"],
            explicit_user_statements=["I need to do X"],
            detected_sentiment="neutral",
        )
        for c in result.candidates:
            assert c.kind != "action"
            assert c.kind in DerivationKind

    def test_rejected_forbidden_kind_has_clear_message(self):
        """Rejection message for forbidden kinds is user-actionable."""
        cand = DerivationCandidate(
            kind="task",  # type: ignore[arg-type]
            assertion_source=AssertionSource.agent_inferred,
            summary="Do the thing",
            confidence=0.5,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert "forbidden" in result.reasons[0].lower()
        assert "actions" in result.reasons[0] or "tasks" in result.reasons[0]


# ── Confidence and reason capture tests ──────────────────────────────────────


class TestConfidenceReasonCapture:
    """Verify confidence scores are captured and eligibility reasons are present."""

    def test_eligible_candidate_has_reasons(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="Fact",
            confidence=0.9,
            supporting_message_ids=[uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible
        assert len(result.reasons) > 0
        # Reasons should include kind, confidence, evidence, summary, assertion
        reason_text = " ".join(result.reasons)
        assert "memory" in reason_text
        assert "0.9" in reason_text  # confidence

    def test_rejected_candidate_has_reasons(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="Fact",
            confidence=0.9,
            supporting_message_ids=[],  # No evidence
        )
        result = check_eligibility(cand)
        assert not result.eligible
        assert len(result.reasons) == 1
        assert "supporting_message_ids" in result.reasons[0]

    def test_confidence_is_float(self):
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="Fact",
            confidence=0.75,
            supporting_message_ids=[uuid4()],
        )
        assert isinstance(cand.confidence, float)
        assert 0.0 <= cand.confidence <= 1.0


# ── Idempotent production tests ──────────────────────────────────────────────


class TestIdempotentProduction:
    """Verify candidate production is deterministic and idempotent."""

    def test_same_input_same_output_order(self):
        engine = DerivationEngine()
        args = dict(
            source_message_ids=[uuid4(), uuid4()],
            plaintext_summary="Same input test.",
            extracted_topics=["topic_a", "topic_b"],
            explicit_user_statements=["Statement 1", "Statement 2", "Statement 3"],
            detected_sentiment="mixed",
        )

        result1 = engine.produce_candidates(**args)
        result2 = engine.produce_candidates(**args)

        assert len(result1.candidates) == len(result2.candidates)
        assert len(result1.rejected) == len(result2.rejected)

        for i, (c1, c2) in enumerate(zip(result1.candidates, result2.candidates)):
            assert c1.kind == c2.kind, f"Candidate {i}: kind mismatch"
            assert c1.summary == c2.summary, f"Candidate {i}: summary mismatch"
            assert c1.confidence == c2.confidence, f"Candidate {i}: confidence mismatch"
            assert c1.assertion_source == c2.assertion_source, f"Candidate {i}: source mismatch"


# ── Multi-evidence distillation eligibility tests (T15) ──────────────────────


class TestMultiEvidenceDistillationEligibility:
    """Verify distillation requires ≥2 distinct supporting message IDs."""

    def test_distillation_single_evidence_rejected(self):
        """A distillation with only 1 supporting message is not eligible."""
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Pattern from one message is not a distillation",
            confidence=0.7,
            supporting_message_ids=[uuid4()],  # Only 1 — under-evidenced
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        assert "multi-evidence" in result.reasons[0].lower()
        assert "2" in result.reasons[0]
        assert "1" in result.reasons[0]

    def test_distillation_two_evidence_passes(self):
        """A distillation with 2 distinct supporting messages is eligible."""
        msg_a = uuid4()
        msg_b = uuid4()
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Valid multi-evidence distillation",
            confidence=0.6,
            supporting_message_ids=[msg_a, msg_b],
        )
        result = check_eligibility(cand)
        assert result.eligible is True
        reason_text = " ".join(result.reasons)
        assert "multi-evidence satisfied" in reason_text

    def test_distillation_duplicate_message_ids_count_once(self):
        """Duplicate message IDs count as 1 distinct piece of evidence."""
        same_msg = uuid4()
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Duplicated evidence is not multi-evidence",
            confidence=0.6,
            supporting_message_ids=[same_msg, same_msg],  # Same UUID twice
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        assert "1" in result.reasons[0]  # Only 1 distinct

    def test_distillation_three_evidence_passes(self):
        """A distillation with 3 supporting messages is eligible."""
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="Rich multi-evidence distillation",
            confidence=0.8,
            supporting_message_ids=[uuid4(), uuid4(), uuid4()],
        )
        result = check_eligibility(cand)
        assert result.eligible is True

    def test_memory_does_not_require_multi_evidence(self):
        """Memory derivations are not subject to the multi-evidence threshold."""
        cand = DerivationCandidate(
            kind=DerivationKind.memory,
            assertion_source=AssertionSource.user_explicit,
            summary="Single-message memory is fine",
            confidence=0.9,
            supporting_message_ids=[uuid4()],  # 1 is enough for memory
        )
        result = check_eligibility(cand)
        assert result.eligible is True

    def test_observation_does_not_require_multi_evidence(self):
        """Observation derivations are not subject to the multi-evidence threshold."""
        cand = DerivationCandidate(
            kind=DerivationKind.observation,
            assertion_source=AssertionSource.user_explicit,
            summary="Single-message observation is fine",
            confidence=0.8,
            supporting_message_ids=[uuid4()],  # 1 is enough for observation
        )
        result = check_eligibility(cand)
        assert result.eligible is True

    def test_multi_evidence_gate_comes_before_assertion_gate(self):
        """The multi-evidence gate is evaluated before the assertion gate.

        This ensures under-evidenced distillations are rejected with a clear
        evidence message, not a confusing assertion-source message.
        """
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source="made_up",  # type: ignore[arg-type]  # Invalid source
            summary="Should fail at evidence, not assertion",
            confidence=0.5,
            supporting_message_ids=[uuid4()],  # Only 1 — fails multi-evidence first
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        # Must fail at multi-evidence gate (gate 4), not assertion gate (gate 6)
        assert "multi-evidence" in result.reasons[0].lower()
        assert "assertion_source" not in result.reasons[0]

    def test_distillation_zero_evidence_rejected(self):
        """A distillation with zero evidence fails at the basic evidence gate."""
        cand = DerivationCandidate(
            kind=DerivationKind.distillation,
            assertion_source=AssertionSource.agent_inferred,
            summary="No evidence distillation",
            confidence=0.6,
            supporting_message_ids=[],
        )
        result = check_eligibility(cand)
        assert result.eligible is False
        # Fails at gate 3 (basic evidence), not gate 4 (multi-evidence)
        assert "supporting_message_ids" in result.reasons[0]
