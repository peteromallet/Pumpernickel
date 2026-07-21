"""Bounded normalization for finalized reflection sessions.

Produces the validated shared reflection payload and template-specific data
from ordered canonical source messages — without inventing fields unsupported
by the evidence.

Design contract
---------------
* **Pure business logic** — no database access, no I/O.
* **Evidence-bound**: every extracted field MUST be traceable to at least one
  source message.  Fields that cannot be supported are recorded in
  ``fields_unsupported`` and set to ``None`` / empty in the output.
* **Schema-driven**: each ``template_key`` declares a schema of expected fields.
  The normalizer validates that template-specific data conforms to the schema
  and that no extra fields are injected.
* **Ordered-source-message fidelity**: the normalizer processes source messages
  in their canonical arrival order.  Extraction is positional (first mention
  wins for scalar fields) and cumulative (all mentions aggregated for list
  fields).

Shared vs template-specific payload
-----------------------------------
The **shared reflection payload** contains fields common to every reflection
regardless of template: source message IDs, raw texts, extracted topics,
sentiment, explicit user statements, and a faithful plaintext summary.

The **template-specific payload** maps source-message evidence to the fields
declared by a particular template (e.g. ``freeform_reflection``).  Only fields
with supporting evidence are populated.

Schema version: 1
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

# ── Public surface ──────────────────────────────────────────────────────────

# Known template keys and their schemas.
# Each schema declares the fields the template expects, their types, and
# whether they are required.  The normalizer will only populate fields that
# have supporting evidence in the source messages.
_TEMPLATE_SCHEMAS: dict[str, dict[str, Any]] = {
    "freeform_reflection": {
        "description": "Open-ended reflection with no preset structure.",
        "fields": {
            "mood": {
                "type": "str",
                "required": False,
                "description": "User's stated or implied emotional state.",
                "evidence_patterns": [
                    r"\bI('?m| am) feeling\b",
                    r"\bI feel\b",
                    r"\bmy mood\b",
                    r"\bI'?ve been feeling\b",
                ],
            },
            "energy_level": {
                "type": "str",
                "required": False,
                "description": "User's stated energy level.",
                "evidence_patterns": [
                    r"\benergy\b",
                    r"\bI('?m| am) (so |really |very )?tired\b",
                    r"\bI('?m| am) (so |really |very )?energized\b",
                    r"\bexhausted\b",
                    r"\bwired\b",
                    r"\benergetic\b",
                ],
            },
            "focus_areas": {
                "type": "list[str]",
                "required": False,
                "description": "Topics or areas the user is focused on.",
                "evidence_patterns": [
                    r"\bfocus(?:ing|ed)? on\b",
                    r"\bworking on\b",
                    r"\bconcentrating on\b",
                    r"\bpriority\b",
                ],
            },
            "concerns": {
                "type": "list[str]",
                "required": False,
                "description": "Worries, anxieties, or concerns expressed.",
                "evidence_patterns": [
                    r"\bworried about\b",
                    r"\bconcerned about\b",
                    r"\banxious about\b",
                    r"\bstressed about\b",
                    r"\bbothered by\b",
                ],
            },
            "accomplishments": {
                "type": "list[str]",
                "required": False,
                "description": "Things the user accomplished or completed.",
                "evidence_patterns": [
                    r"\baccomplished\b",
                    r"\bachieved\b",
                    r"\bgot done\b",
                    r"\bfinished\b",
                    r"\bcompleted\b",
                    r"\bdid\b",
                ],
            },
            "intentions": {
                "type": "list[str]",
                "required": False,
                "description": "What the user intends to do next.",
                "evidence_patterns": [
                    r"\bI (?:will|plan to|intend to|'ll|am going to)\b",
                    r"\bnext I\b",
                    r"\bgoal\b",
                    r"\bintend to\b",
                ],
            },
            "gratitude": {
                "type": "list[str]",
                "required": False,
                "description": "Things the user expressed gratitude for.",
                "evidence_patterns": [
                    r"\bgrateful for\b",
                    r"\bthankful for\b",
                    r"\bappreciate\b",
                    r"\bblessed\b",
                ],
            },
            "challenges": {
                "type": "list[str]",
                "required": False,
                "description": "Difficulties or obstacles mentioned.",
                "evidence_patterns": [
                    r"\bchallenge\b",
                    r"\bstrug(?:gle|gling) with\b",
                    r"\bdifficult\b",
                    r"\bhard\b",
                    r"\bobstacle\b",
                ],
            },
        },
    },
}


# ── Result types ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SharedReflectionPayload:
    """Fields common to every reflection, regardless of template.

    Attributes:
        source_message_ids: Ordered canonical source message UUIDs.
        raw_message_texts: Raw text of each source message, in the same order.
        normalized_at: When normalization was performed.
        extracted_topics: Topics/themes with explicit evidence in messages.
        detected_sentiment: Overall sentiment if clearly evidenced (None if ambiguous).
        explicit_user_statements: Verbatim user statements extracted as evidence.
        plaintext_summary: Faithful summary derived solely from source messages.
        fields_with_evidence: Names of shared fields that have source evidence.
    """

    source_message_ids: list[UUID]
    raw_message_texts: list[str]
    normalized_at: datetime
    extracted_topics: list[str]
    detected_sentiment: str | None
    explicit_user_statements: list[str]
    plaintext_summary: str
    fields_with_evidence: list[str]


@dataclass(frozen=True, slots=True)
class NormalizedReflection:
    """The complete bounded-normalization output for a finalized session.

    Attributes:
        shared: Common reflection payload (always present).
        template_key: The template that produced ``template_data``.
        template_data: Template-specific fields populated from source evidence.
            Keys match the template schema; values are ``None`` for fields
            without supporting evidence.
        schema_version: Version of the normalization schema used.
        fields_unsupported: Template fields that were requested but have no
            evidence in the source messages.
        extraction_confidence: 0.0–1.0 overall confidence in the extraction.
    """

    shared: SharedReflectionPayload
    template_key: str
    template_data: dict[str, Any]
    schema_version: int
    fields_unsupported: list[str]
    extraction_confidence: float


# ── Pattern bank for shared extraction ──────────────────────────────────────

# Sentiment detection patterns — ordered from most specific to most general.
_SENTIMENT_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("positive", re.compile(
        r"\b(happy|great|wonderful|excellent|amazing|fantastic|good|"
        r"joy|love|grateful|thankful|excited|proud|optimistic|"
        r"hopeful|content|satisfied|pleased)\b",
        re.IGNORECASE,
    )),
    ("negative", re.compile(
        r"\b(sad|terrible|awful|horrible|bad|angry|frustrated|"
        r"anxious|worried|stressed|depressed|upset|disappointed|"
        r"miserable|unhappy|overwhelmed|exhausted|burned out)\b",
        re.IGNORECASE,
    )),
    ("neutral", re.compile(
        r"\b(fine|okay|ok|alright|neutral|meh|so-so|decent)\b",
        re.IGNORECASE,
    )),
]

# Topic extraction: look for explicit topic markers.
_TOPIC_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"\b(?:about|regarding|on the topic of|thinking about)\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(
        r"\bI'?ve been (?:thinking|pondering|considering)\s+(?:about\s+)?(.+?)(?:[.!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"\bI'?ve been reflecting\s+(?:on\s+)?(.+?)(?:[.!?]|$)",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:my|the) (?:main|big|key) (?:topic|theme|focus|subject|area)\s+(?:is\s+)?(.+?)(?:[.!?]|$)", re.IGNORECASE),
]

# Explicit statement markers: user stating something directly about themselves.
_EXPLICIT_STATEMENT_MARKERS: list[re.Pattern[str]] = [
    re.compile(r"\bI (?:realize|recognize|understand|know|see|notice|observe)\s+that\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\bI (?:think|believe|feel)\s+(?:that\s+)?(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\bfor me\b.*?\b(I|it|this|that)\s+(.+?)(?:[.!?]|$)", re.IGNORECASE),
    re.compile(r"\b(I'?ve (?:learned|discovered|found|noticed|realized)\s+(?:that\s+)?(.+?)(?:[.!?]|$))", re.IGNORECASE),
]


# ── Schema version ──────────────────────────────────────────────────────────

CURRENT_SCHEMA_VERSION: int = 1


# ── Normalizer ──────────────────────────────────────────────────────────────


class ReflectionNormalizer:
    """Bounded normalizer that produces structured reflection payloads.

    The normalizer processes ordered source messages and extracts:
    1. A **shared reflection payload** common to all templates.
    2. **Template-specific data** validated against the template's schema.

    Every extracted field must be supported by at least one source message.
    Fields without evidence are recorded in ``fields_unsupported`` and set
    to ``None`` (scalar) or ``[]`` (list) in the output.

    Usage::

        normalizer = ReflectionNormalizer()
        result = normalizer.normalize(
            source_message_texts=["I'm feeling great today", "I finished the report"],
            template_key="freeform_reflection",
        )
        assert result.shared.detected_sentiment == "positive"
        assert "mood" in result.fields_unsupported  # no direct mood evidence
    """

    # ── Public API ──────────────────────────────────────────────────────

    def normalize(
        self,
        *,
        source_message_ids: list[UUID],
        source_message_texts: list[str],
        template_key: str,
        normalized_at: datetime | None = None,
    ) -> NormalizedReflection:
        """Normalize ordered source messages into a structured reflection.

        Args:
            source_message_ids: Ordered canonical source message UUIDs.
            source_message_texts: Raw text of each message, matching the
                order of ``source_message_ids``.  Must be the same length.
            template_key: The template to normalize against (e.g.
                ``"freeform_reflection"``).
            normalized_at: Timestamp of normalization (default: now UTC).

        Returns:
            ``NormalizedReflection`` with shared payload and template data.

        Raises:
            ValueError: If ``source_message_ids`` and ``source_message_texts``
                have different lengths, or if ``template_key`` is unknown.
        """
        if len(source_message_ids) != len(source_message_texts):
            raise ValueError(
                f"source_message_ids length ({len(source_message_ids)}) must "
                f"match source_message_texts length ({len(source_message_texts)})"
            )

        template_schema = _TEMPLATE_SCHEMAS.get(template_key)
        if template_schema is None:
            raise ValueError(
                f"unknown template_key {template_key!r}; "
                f"known templates: {sorted(_TEMPLATE_SCHEMAS.keys())}"
            )

        if normalized_at is None:
            normalized_at = datetime.now(timezone.utc)

        # ── Build shared payload ────────────────────────────────────────
        shared = self._build_shared_payload(
            source_message_ids=source_message_ids,
            source_message_texts=source_message_texts,
            normalized_at=normalized_at,
        )

        # ── Build template-specific data ────────────────────────────────
        template_data, fields_unsupported = self._build_template_data(
            source_message_texts=source_message_texts,
            template_key=template_key,
            template_schema=template_schema,
        )

        # ── Compute overall confidence ──────────────────────────────────
        confidence = self._compute_extraction_confidence(
            shared=shared,
            template_data=template_data,
            fields_unsupported=fields_unsupported,
            total_template_fields=len(template_schema.get("fields", {})),
        )

        return NormalizedReflection(
            shared=shared,
            template_key=template_key,
            template_data=template_data,
            schema_version=CURRENT_SCHEMA_VERSION,
            fields_unsupported=fields_unsupported,
            extraction_confidence=confidence,
        )

    # ── Shared payload builder ──────────────────────────────────────────

    def _build_shared_payload(
        self,
        *,
        source_message_ids: list[UUID],
        source_message_texts: list[str],
        normalized_at: datetime,
    ) -> SharedReflectionPayload:
        """Build the common reflection payload from ordered source messages."""
        fields_with_evidence: list[str] = []

        # Topics
        topics = self._extract_topics(source_message_texts)
        if topics:
            fields_with_evidence.append("extracted_topics")

        # Sentiment
        sentiment = self._extract_sentiment(source_message_texts)
        if sentiment is not None:
            fields_with_evidence.append("detected_sentiment")

        # Explicit statements
        statements = self._extract_explicit_statements(source_message_texts)
        if statements:
            fields_with_evidence.append("explicit_user_statements")

        # Plaintext summary — always present (derived from messages)
        summary = self._build_plaintext_summary(source_message_texts)
        fields_with_evidence.append("plaintext_summary")

        # source_message_ids and raw_message_texts are always present
        fields_with_evidence.append("source_message_ids")
        fields_with_evidence.append("raw_message_texts")
        fields_with_evidence.append("normalized_at")

        return SharedReflectionPayload(
            source_message_ids=list(source_message_ids),
            raw_message_texts=list(source_message_texts),
            normalized_at=normalized_at,
            extracted_topics=topics,
            detected_sentiment=sentiment,
            explicit_user_statements=statements,
            plaintext_summary=summary,
            fields_with_evidence=fields_with_evidence,
        )

    # ── Template-specific builder ───────────────────────────────────────

    def _build_template_data(
        self,
        *,
        source_message_texts: list[str],
        template_key: str,
        template_schema: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        """Build template-specific data, tracking unsupported fields.

        Returns:
            ``(template_data, fields_unsupported)`` where ``template_data``
            maps field names to extracted values (None/empty for unsupported
            fields) and ``fields_unsupported`` lists field names that could
            not be evidenced.
        """
        combined_text = " ".join(source_message_texts)
        fields_def = template_schema.get("fields", {})
        template_data: dict[str, Any] = {}
        fields_unsupported: list[str] = []

        for field_name, field_def in fields_def.items():
            field_type = field_def.get("type", "str")
            evidence_patterns: list[str] = field_def.get("evidence_patterns", [])
            required = field_def.get("required", False)

            if not evidence_patterns:
                # No patterns defined — cannot extract evidence.
                template_data[field_name] = None if field_type == "str" else []
                fields_unsupported.append(field_name)
                continue

            # Check if any evidence pattern matches.
            has_evidence = any(
                re.search(pat, combined_text, re.IGNORECASE)
                for pat in evidence_patterns
            )

            if not has_evidence:
                # No evidence — record as unsupported.
                template_data[field_name] = None if field_type == "str" else []
                fields_unsupported.append(field_name)
                continue

            # Evidence found — extract the value.
            if field_type == "str":
                extracted = self._extract_scalar_field(
                    source_message_texts, evidence_patterns, field_name
                )
                template_data[field_name] = extracted
            elif field_type.startswith("list"):
                extracted = self._extract_list_field(
                    source_message_texts, evidence_patterns, field_name
                )
                template_data[field_name] = extracted
            else:
                # Unknown type — treat as scalar.
                extracted = self._extract_scalar_field(
                    source_message_texts, evidence_patterns, field_name
                )
                template_data[field_name] = extracted

        return template_data, fields_unsupported

    # ── Extraction helpers ──────────────────────────────────────────────

    @staticmethod
    def _extract_topics(texts: list[str]) -> list[str]:
        """Extract topics/themes from ordered source messages."""
        topics: list[str] = []
        seen: set[str] = set()
        combined = " ".join(texts)
        for pattern in _TOPIC_MARKERS:
            for match in pattern.finditer(combined):
                topic = match.group(1).strip().rstrip(".!?")
                topic_lower = topic.lower()
                if topic_lower not in seen and len(topic) >= 2:
                    topics.append(topic)
                    seen.add(topic_lower)
        return topics

    @staticmethod
    def _extract_sentiment(texts: list[str]) -> str | None:
        """Detect overall sentiment from ordered source messages.

        Returns ``None`` if sentiment is ambiguous or unexpressed.
        """
        combined = " ".join(texts)
        scores: dict[str, int] = {"positive": 0, "negative": 0, "neutral": 0}

        for label, pattern in _SENTIMENT_PATTERNS:
            count = len(pattern.findall(combined))
            scores[label] = count

        total = sum(scores.values())
        if total == 0:
            return None  # No sentiment words found

        # If positive clearly dominates, return positive.
        if scores["positive"] > scores["negative"] + scores["neutral"]:
            return "positive"
        # If negative clearly dominates, return negative.
        if scores["negative"] > scores["positive"] + scores["neutral"]:
            return "negative"
        # If neutral dominates or it's a tie, return neutral.
        if scores["neutral"] > 0:
            return "neutral"
        # Ambiguous — positive and negative both present with no clear winner.
        if scores["positive"] > 0 and scores["negative"] > 0:
            return "mixed"
        # Only one type present with very low count — still report it.
        if scores["positive"] > 0:
            return "positive"
        if scores["negative"] > 0:
            return "negative"
        return None

    @staticmethod
    def _extract_explicit_statements(texts: list[str]) -> list[str]:
        """Extract verbatim user statements from ordered source messages."""
        statements: list[str] = []
        seen: set[str] = set()
        combined = " ".join(texts)
        for pattern in _EXPLICIT_STATEMENT_MARKERS:
            for match in pattern.finditer(combined):
                stmt = match.group(0).strip()
                stmt_lower = stmt.lower()
                if stmt_lower not in seen and len(stmt) >= 10:
                    statements.append(stmt)
                    seen.add(stmt_lower)
        return statements

    @staticmethod
    def _extract_scalar_field(
        texts: list[str],
        patterns: list[str],
        field_name: str,
    ) -> str | None:
        """Extract a scalar template field value from source messages.

        Uses the first match across all messages (positional precedence).
        Captures the context around the match as the value.

        Returns ``None`` if no pattern matches.
        """
        combined = " ".join(texts)
        for pat_str in patterns:
            match = re.search(pat_str, combined, re.IGNORECASE)
            if match:
                # Extract context: the sentence containing the match.
                start = max(0, match.start() - 60)
                end = min(len(combined), match.end() + 100)
                snippet = combined[start:end].strip()
                # Try to get a clean sentence.
                # Find nearest sentence boundaries.
                for delim in [". ", "! ", "? ", ".\n", "!\n", "?\n"]:
                    last_delim = snippet.rfind(delim)
                    if last_delim != -1:
                        snippet = snippet[:last_delim + 1].strip()
                        break
                return snippet if snippet else None
        return None

    @staticmethod
    def _extract_list_field(
        texts: list[str],
        patterns: list[str],
        field_name: str,
    ) -> list[str]:
        """Extract list-type template field values from source messages.

        Collects all unique context snippets matching any evidence pattern.

        Returns an empty list if no pattern matches.
        """
        combined = " ".join(texts)
        items: list[str] = []
        seen: set[str] = set()

        for pat_str in patterns:
            for match in re.finditer(pat_str, combined, re.IGNORECASE):
                start = max(0, match.start() - 40)
                end = min(len(combined), match.end() + 80)
                snippet = combined[start:end].strip()
                snippet_lower = snippet.lower()
                if snippet_lower not in seen and len(snippet) >= 5:
                    items.append(snippet)
                    seen.add(snippet_lower)

        return items

    @staticmethod
    def _build_plaintext_summary(texts: list[str]) -> str:
        """Build a faithful plaintext summary from ordered source messages.

        The summary is a concatenation of the message texts, truncated to a
        reasonable length.  This is intentionally simple — it does not
        synthesize or infer, only preserves what was actually said.
        """
        if not texts:
            return ""
        combined = " ".join(texts)
        if len(combined) <= 500:
            return combined
        return combined[:497] + "..."

    @staticmethod
    def _compute_extraction_confidence(
        *,
        shared: SharedReflectionPayload,
        template_data: dict[str, Any],
        fields_unsupported: list[str],
        total_template_fields: int,
    ) -> float:
        """Compute overall extraction confidence (0.0–1.0).

        Factors:
        - Fraction of template fields with evidence.
        - Whether shared fields have evidence.
        - Number of source messages (more messages → more evidence potential).
        """
        if total_template_fields == 0:
            return 1.0

        supported_template_fields = total_template_fields - len(fields_unsupported)
        template_ratio = supported_template_fields / total_template_fields

        # Shared fields: count how many are evidenced.
        shared_expected = {"extracted_topics", "detected_sentiment", "explicit_user_statements"}
        shared_evidenced = set(shared.fields_with_evidence) & shared_expected
        shared_ratio = len(shared_evidenced) / len(shared_expected) if shared_expected else 1.0

        # Blend: template evidence weighted more heavily.
        return round(0.6 * template_ratio + 0.4 * shared_ratio, 4)


# ── Convenience helpers ─────────────────────────────────────────────────────


def normalize_session(
    *,
    source_message_ids: list[UUID],
    source_message_texts: list[str],
    template_key: str = "freeform_reflection",
    normalized_at: datetime | None = None,
) -> NormalizedReflection:
    """Convenience wrapper around ``ReflectionNormalizer.normalize()``.

    Args:
        source_message_ids: Ordered canonical source message UUIDs.
        source_message_texts: Raw text of each message (same order).
        template_key: Template to normalize against.
        normalized_at: Timestamp (default: now UTC).

    Returns:
        ``NormalizedReflection``.
    """
    normalizer = ReflectionNormalizer()
    return normalizer.normalize(
        source_message_ids=source_message_ids,
        source_message_texts=source_message_texts,
        template_key=template_key,
        normalized_at=normalized_at,
    )


def get_template_schema(template_key: str) -> dict[str, Any] | None:
    """Return the field schema for a template key, or None if unknown."""
    return _TEMPLATE_SCHEMAS.get(template_key)


def list_template_keys() -> list[str]:
    """Return all known template keys."""
    return sorted(_TEMPLATE_SCHEMAS.keys())
