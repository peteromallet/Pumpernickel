"""Reflection derivation candidates — conservative, typed, eligibility-gated.

Design contract (T12)
---------------------
Derivations represent candidate knowledge that *may* be derived from a
reflection entry.  They are **not** writes to target knowledge services —
they are proposals that downstream M3 ledgering/decision logic evaluates.

**Strictly limited kinds**: memory, observation, distillation, orientation.
Actions, tasks, reminders, and follow-ups are **explicitly forbidden** as
derivation kinds.

**Deterministic eligibility gates**: before any candidate is emitted, it
passes through a deterministic, evidence-based eligibility check.  Candidates
that fail are rejected with a reason.  This ensures derivation never
accidentally feeds non-knowledge targets.

**Idempotent**: the same input produces the same candidates.  This module is
pure business logic — no database access, no I/O, no side effects.  Callers
are responsible for deduplication at write time via idempotency_key.

**Confidence/reason capture**: every candidate carries an assertion_source,
confidence score, eligibility_reasons, and supporting_message_ids so that
downstream consumers can audit and decide.

Schema version: 1
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import UUID


# ── Typed derivation kinds ───────────────────────────────────────────────────
#
# These are the ONLY allowed derivation kinds.  Actions, tasks, reminders,
# and follow-ups are explicitly absent from this enum — they are NOT knowledge
# types and MUST NOT be derived from reflections.


class DerivationKind(str, Enum):
    """Allowed derivation kinds — strictly knowledge types only.

    Actions, tasks, reminders, and follow-ups are NOT derivation kinds.
    A reflection captures structured evidence; it cannot prescribe action.
    """
    memory = "memory"           # Durable fact about the user or their world
    observation = "observation" # Learned pattern from repeated evidence
    distillation = "distillation" # Provisional synthesized explanation
    orientation = "orientation" # Compass heading: principle, manifestation, goal, priority, anti-pattern


# Forbidden kinds — these must be rejected at the eligibility gate.
_FORBIDDEN_KINDS: frozenset[str] = frozenset({
    "action",
    "task",
    "reminder",
    "follow_up",
    "follow-up",
    "checkin",
    "schedule",
    "nudge",
    "escalation",
})


# ── Assertion sources ────────────────────────────────────────────────────────


class AssertionSource(str, Enum):
    """Where did this derivation candidate's claim come from?

    Mirrors the migration CHECK constraint on mediator.reflection_derivations.
    """
    user_explicit = "user_explicit"   # User stated it directly
    user_implied = "user_implied"     # Strongly implied by user's words
    agent_inferred = "agent_inferred" # Inferred by the derivation engine


# ── Candidate model ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DerivationCandidate:
    """A typed, eligibility-gated candidate for knowledge derivation.

    This is a pure data object — no side effects, no database access.
    Callers (e.g. M3 ledgering) are responsible for deciding whether to
    apply, reinforce, defer, or reject each candidate.
    """

    kind: DerivationKind
    """The knowledge type this candidate would produce."""

    assertion_source: AssertionSource
    """Where the claim came from (user_explicit, user_implied, agent_inferred)."""

    summary: str
    """Short human-readable summary of the candidate knowledge claim."""

    confidence: float
    """Confidence score in [0.0, 1.0].  Required; must be explicit."""

    eligibility_reasons: list[str] = field(default_factory=list)
    """Why this candidate passed (or would fail) the eligibility gate."""

    supporting_message_ids: list[UUID] = field(default_factory=list)
    """Source message UUIDs that support this candidate."""

    detail: dict[str, Any] | None = None
    """Optional structured detail (e.g. field values for compass items)."""

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence}"
            )
        if not self.summary or not self.summary.strip():
            raise ValueError("summary must be a non-blank string")


# ── Eligibility gate ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    """Result of a deterministic eligibility check on a derivation candidate."""

    eligible: bool
    """True if the candidate passes all gates."""

    reasons: list[str] = field(default_factory=list)
    """Human-readable reasons for the eligibility decision.

    For eligible candidates, these are the positive reasons the gate was passed.
    For rejected candidates, these explain which gate failed and why.
    """


# ── Multi-evidence threshold ─────────────────────────────────────────────────
#
# Distillation derivations synthesize insights from multiple evidence signals.
# A single supporting message is not enough to claim a *pattern* — distillations
# require at least two distinct pieces of supporting evidence before they are
# eligible for application.

_MIN_DISTILLATION_EVIDENCE: int = 2
"""Minimum number of supporting message IDs required for a distillation candidate."""


def check_eligibility(candidate: DerivationCandidate) -> EligibilityResult:
    """Deterministic eligibility gate for derivation candidates.

    Evaluates, in order:
    1. **Kind gate**: the derivation kind MUST be one of the allowed knowledge
       types (memory, observation, distillation, orientation).  Actions, tasks,
       reminders, and follow-ups are rejected with a clear reason.
    2. **Confidence gate**: confidence must be in (0.0, 1.0] — zero-confidence
       candidates are not derivable.
    3. **Evidence gate**: at least one supporting_message_id must be present.
       Derivations without traceable evidence are not eligible.
    4. **Multi-evidence gate** (distillation only): distillation candidates
       require at least ``_MIN_DISTILLATION_EVIDENCE`` distinct supporting
       message IDs.  A single message does not constitute a synthesized
       pattern — multi-evidence is required for distillations.
    5. **Summary gate**: the candidate must carry a non-trivial summary.
    6. **Assertion gate**: the assertion_source must be a valid source, and
       agent_inferred candidates must have at least medium confidence (>=0.5).

    Returns an ``EligibilityResult`` with ``eligible=True`` and the reasons
    that were satisfied, or ``eligible=False`` with the failing reasons.

    This gate is **deterministic**: the same candidate always produces the
    same result.  It does NOT access any external state or database.
    """
    reasons: list[str] = []

    # Gate 1: Kind — must be an allowed knowledge type, not action/reminder/etc.
    kind_str = candidate.kind.value if isinstance(candidate.kind, DerivationKind) else str(candidate.kind)

    if kind_str in _FORBIDDEN_KINDS:
        return EligibilityResult(
            eligible=False,
            reasons=[f"derivation kind '{kind_str}' is forbidden — reflections cannot derive actions, tasks, reminders, or follow-ups"],
        )

    # Validate that the kind is a recognized DerivationKind
    try:
        DerivationKind(kind_str)
    except ValueError:
        allowed = sorted([k.value for k in DerivationKind])
        return EligibilityResult(
            eligible=False,
            reasons=[f"derivation kind '{kind_str}' is not a recognized knowledge type; allowed: {allowed}"],
        )

    reasons.append(f"kind={kind_str} (allowed knowledge type)")

    # Gate 2: Confidence must be positive
    if candidate.confidence <= 0.0 or candidate.confidence > 1.0:
        return EligibilityResult(
            eligible=False,
            reasons=[f"confidence {candidate.confidence} is not in (0.0, 1.0]"],
        )
    reasons.append(f"confidence={candidate.confidence:.2f}")

    # Gate 3: Must have supporting message evidence
    if not candidate.supporting_message_ids:
        return EligibilityResult(
            eligible=False,
            reasons=["no supporting_message_ids — derivations require traceable evidence"],
        )
    reasons.append(f"evidence={len(candidate.supporting_message_ids)} message(s)")

    # Gate 4 (distillation only): Multi-evidence threshold.
    # Distillations synthesize across evidence — a single message is
    # insufficient to claim a pattern.  This gate enforces the
    # multi-evidence requirement at eligibility time so under-evidenced
    # distillation candidates never reach the ledger.
    if kind_str == DerivationKind.distillation.value:
        distinct_evidence = len(set(candidate.supporting_message_ids))
        if distinct_evidence < _MIN_DISTILLATION_EVIDENCE:
            return EligibilityResult(
                eligible=False,
                reasons=[
                    f"distillation requires at least {_MIN_DISTILLATION_EVIDENCE} "
                    f"distinct supporting message IDs (multi-evidence requirement); "
                    f"got {distinct_evidence}"
                ],
            )
        reasons.append(
            f"distillation multi-evidence satisfied ({distinct_evidence} >= "
            f"{_MIN_DISTILLATION_EVIDENCE} messages)"
        )

    # Gate 5: Summary must be non-trivial
    if not candidate.summary or not candidate.summary.strip():
        return EligibilityResult(
            eligible=False,
            reasons=["summary is empty — derivations require a human-readable claim"],
        )
    reasons.append("summary present")

    # Gate 6: Assertion source must be valid; agent_inferred requires medium confidence
    source_str = candidate.assertion_source.value if isinstance(candidate.assertion_source, AssertionSource) else str(candidate.assertion_source)

    try:
        AssertionSource(source_str)
    except ValueError:
        valid = sorted([s.value for s in AssertionSource])
        return EligibilityResult(
            eligible=False,
            reasons=[f"assertion_source '{source_str}' is not valid; allowed: {valid}"],
        )

    if source_str == "agent_inferred" and candidate.confidence < 0.5:
        return EligibilityResult(
            eligible=False,
            reasons=[f"agent_inferred candidates require confidence >= 0.5, got {candidate.confidence:.2f}"],
        )
    reasons.append(f"assertion_source={source_str}")

    return EligibilityResult(eligible=True, reasons=reasons)


# ── Candidate production ─────────────────────────────────────────────────────


class DerivationEngine:
    """Produces typed derivation candidates from a normalized reflection entry.

    The engine is **idempotent**: calling ``produce_candidates`` with the same
    input always returns the same ordered list of candidates.  It does NOT
    access any external state or database.

    Candidates are produced conservatively:
    - Each candidate is checked against the deterministic eligibility gate.
    - Ineligible candidates are NOT emitted (they are silently dropped).
    - The caller can inspect ``rejected_candidates`` on the result for audit.
    """

    def produce_candidates(
        self,
        *,
        source_message_ids: list[UUID],
        plaintext_summary: str,
        extracted_topics: list[str] | None = None,
        explicit_user_statements: list[str] | None = None,
        detected_sentiment: str | None = None,
        template_data: dict[str, Any] | None = None,
    ) -> DerivationResult:
        """Produce derivation candidates from reflection evidence.

        Args:
            source_message_ids: Ordered canonical source message UUIDs.
            plaintext_summary: Normalized plaintext summary of the reflection.
            extracted_topics: Optional topics extracted by the normalizer.
            explicit_user_statements: Optional explicit user statements.
            detected_sentiment: Optional detected sentiment.
            template_data: Optional template-specific data with _normalizer_meta.

        Returns:
            ``DerivationResult`` with eligible and rejected candidates.
        """
        candidates: list[DerivationCandidate] = []
        rejected: list[tuple[DerivationCandidate, EligibilityResult]] = []

        topics = extracted_topics or []
        statements = explicit_user_statements or []

        # ── 1. Orientation candidate (compass heading) ──────────────────
        if topics:
            # A reflection with extracted topics may imply a focus area
            # that could become a compass item (manifestation, goal, or priority).
            for topic in topics[:3]:  # At most 3 orientation candidates
                cand = DerivationCandidate(
                    kind=DerivationKind.orientation,
                    assertion_source=AssertionSource.agent_inferred,
                    summary=f"User is focused on: {topic}",
                    confidence=_topic_confidence(topic, statements),
                    eligibility_reasons=[],
                    supporting_message_ids=list(source_message_ids),
                    detail={"topic": topic, "source": "reflection_topic"},
                )
                result = check_eligibility(cand)
                if result.eligible:
                    candidates.append(cand)
                else:
                    rejected.append((cand, result))

        # ── 2. Observation candidate ────────────────────────────────────
        if statements and len(statements) >= 2:
            # Multiple explicit statements suggest a recurring pattern
            cand = DerivationCandidate(
                kind=DerivationKind.observation,
                assertion_source=AssertionSource.user_explicit,
                summary=f"Pattern observed: {statements[0][:120]}",
                confidence=min(0.8, 0.5 + 0.1 * len(statements)),
                eligibility_reasons=[],
                supporting_message_ids=list(source_message_ids),
                detail={"statements": statements[:5]},
            )
            result = check_eligibility(cand)
            if result.eligible:
                candidates.append(cand)
            else:
                rejected.append((cand, result))

        # ── 3. Memory candidate ─────────────────────────────────────────
        if statements:
            # First explicit user statement may be a durable fact
            cand = DerivationCandidate(
                kind=DerivationKind.memory,
                assertion_source=AssertionSource.user_explicit,
                summary=statements[0][:200],
                confidence=0.9,
                eligibility_reasons=[],
                supporting_message_ids=list(source_message_ids),
                detail={"statement": statements[0]},
            )
            result = check_eligibility(cand)
            if result.eligible:
                candidates.append(cand)
            else:
                rejected.append((cand, result))

        # ── 4. Distillation candidate ───────────────────────────────────
        # A distillation emerges when we have multiple evidence signals
        # (topics + statements + sentiment) suggesting a synthesized insight.
        if topics and statements and detected_sentiment:
            cand = DerivationCandidate(
                kind=DerivationKind.distillation,
                assertion_source=AssertionSource.agent_inferred,
                summary=f"Synthesized insight about {topics[0]}: {plaintext_summary[:150]}",
                confidence=0.6,
                eligibility_reasons=[],
                supporting_message_ids=list(source_message_ids),
                detail={
                    "topics": topics,
                    "sentiment": detected_sentiment,
                    "statement_count": len(statements),
                },
            )
            result = check_eligibility(cand)
            if result.eligible:
                candidates.append(cand)
            else:
                rejected.append((cand, result))

        return DerivationResult(
            candidates=candidates,
            rejected=rejected,
        )


def _topic_confidence(topic: str, statements: list[str]) -> float:
    """Heuristic confidence that a topic is a genuine focus area.

    Pure deterministic logic — no external lookups.
    """
    if not statements:
        return 0.4

    # Count how many statements mention the topic (case-insensitive)
    topic_lower = topic.lower()
    mentions = sum(1 for s in statements if topic_lower in s.lower())

    if mentions >= 3:
        return 0.8
    elif mentions >= 2:
        return 0.7
    elif mentions >= 1:
        return 0.6
    return 0.4


# ── Result model ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DerivationResult:
    """Result of candidate production from a reflection entry.

    ``candidates`` contains only eligible candidates (those that passed
    the deterministic eligibility gate).  ``rejected`` contains candidates
    that were considered but failed the gate, paired with their
    ``EligibilityResult`` for audit.
    """

    candidates: list[DerivationCandidate] = field(default_factory=list)
    """Eligible candidates, in production order."""

    rejected: list[tuple[DerivationCandidate, EligibilityResult]] = field(default_factory=list)
    """Rejected candidates with their eligibility failure reasons."""
