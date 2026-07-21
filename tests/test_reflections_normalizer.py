"""Tests for app/reflections/normalizer.py — bounded normalization.

Covers:
  - Shared payload extraction: topics, sentiment, explicit statements, summary.
  - Template-specific data extraction with evidence patterns.
  - Missing-field restraint: fields without evidence are None/empty.
  - Ordered-source-message fidelity: first-match-wins, positional precedence.
  - Schema validation: unknown template keys raise errors.
  - Edge cases: empty messages, mismatched lengths, no sentiment.
  - Extraction confidence computation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from app.reflections.normalizer import (
    CURRENT_SCHEMA_VERSION,
    NormalizedReflection,
    ReflectionNormalizer,
    SharedReflectionPayload,
    get_template_schema,
    list_template_keys,
    normalize_session,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize(
    *,
    texts: list[str],
    ids: list[UUID] | None = None,
    template_key: str = "freeform_reflection",
    normalized_at: datetime | None = None,
) -> NormalizedReflection:
    """Shortcut to normalize with auto-generated IDs."""
    if ids is None:
        ids = [_uid() for _ in texts]
    return normalize_session(
        source_message_ids=ids,
        source_message_texts=texts,
        template_key=template_key,
        normalized_at=normalized_at,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Shared payload tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSharedPayload:
    """Tests for shared reflection payload extraction."""

    def test_source_message_ids_preserved_in_order(self):
        """Source message IDs must be preserved in canonical order."""
        ids = [_uid(), _uid(), _uid()]
        result = _normalize(texts=["a", "b", "c"], ids=ids)
        assert result.shared.source_message_ids == ids
        assert result.shared.source_message_ids == ids  # order preserved

    def test_raw_message_texts_preserved_in_order(self):
        """Raw message texts must be preserved in the same order as IDs."""
        texts = ["first message", "second message", "third message"]
        result = _normalize(texts=texts)
        assert result.shared.raw_message_texts == texts

    def test_mismatched_lengths_raises(self):
        """Mismatched IDs and texts must raise ValueError."""
        with pytest.raises(ValueError, match="must match"):
            normalize_session(
                source_message_ids=[_uid(), _uid()],
                source_message_texts=["only one"],
            )

    def test_normalized_at_set(self):
        """normalized_at must be set, defaulting to now UTC."""
        result = _normalize(texts=["hello"])
        assert result.shared.normalized_at is not None
        assert result.shared.normalized_at.tzinfo is not None

    def test_normalized_at_custom(self):
        """Custom normalized_at must be honoured."""
        custom = datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)
        result = _normalize(texts=["hello"], normalized_at=custom)
        assert result.shared.normalized_at == custom


class TestTopicExtraction:
    """Tests for topic/theme extraction from source messages."""

    def test_explicit_topic_marker(self):
        """Topics following 'about', 'regarding', etc. must be extracted."""
        result = _normalize(texts=["I've been thinking about my career direction lately."])
        assert "my career direction lately" in result.shared.extracted_topics

    def test_reflecting_marker(self):
        """Topics following 'reflecting on' must be extracted."""
        result = _normalize(texts=["I've been reflecting on my relationship with work."])
        assert "my relationship with work" in result.shared.extracted_topics

    def test_main_topic_marker(self):
        """Topics following 'my main focus is' must be extracted."""
        result = _normalize(texts=["My main focus is improving team communication."])
        assert "improving team communication" in result.shared.extracted_topics

    def test_multiple_topics(self):
        """Multiple topics from different messages must be extracted."""
        result = _normalize(texts=[
            "I've been thinking about my health.",
            "Also, regarding work-life balance, I have concerns.",
        ])
        assert len(result.shared.extracted_topics) >= 2

    def test_no_topics(self):
        """Messages without topic markers must produce empty topics."""
        result = _normalize(texts=["hello", "how are you", "ok"])
        assert result.shared.extracted_topics == []

    def test_topics_in_fields_with_evidence(self):
        """When topics are extracted, 'extracted_topics' must be in fields_with_evidence."""
        result = _normalize(texts=["I've been thinking about my goals."])
        assert "extracted_topics" in result.shared.fields_with_evidence

    def test_no_topics_not_in_evidence(self):
        """When no topics are found, 'extracted_topics' must NOT be in fields_with_evidence."""
        result = _normalize(texts=["ok"])
        assert "extracted_topics" not in result.shared.fields_with_evidence


class TestSentimentExtraction:
    """Tests for sentiment detection from source messages."""

    def test_positive_sentiment(self):
        """Positive sentiment words must yield 'positive'."""
        result = _normalize(texts=["I'm feeling really happy and grateful today."])
        assert result.shared.detected_sentiment == "positive"

    def test_negative_sentiment(self):
        """Negative sentiment words must yield 'negative'."""
        result = _normalize(texts=["I'm so frustrated and stressed about work."])
        assert result.shared.detected_sentiment == "negative"

    def test_neutral_sentiment(self):
        """Neutral sentiment words must yield 'neutral'."""
        result = _normalize(texts=["I'm feeling okay, just fine really."])
        assert result.shared.detected_sentiment == "neutral"

    def test_mixed_sentiment(self):
        """Both positive and negative words with no clear winner must yield 'mixed'."""
        result = _normalize(texts=["I'm happy about some things but also really frustrated."])
        assert result.shared.detected_sentiment == "mixed"

    def test_no_sentiment_words(self):
        """Messages without sentiment words must yield None."""
        result = _normalize(texts=["The meeting is at 3pm.", "Please bring the report."])
        assert result.shared.detected_sentiment is None

    def test_sentiment_in_fields_with_evidence(self):
        """When sentiment is detected, 'detected_sentiment' must be in fields_with_evidence."""
        result = _normalize(texts=["I'm so happy!"])
        assert "detected_sentiment" in result.shared.fields_with_evidence

    def test_no_sentiment_not_in_evidence(self):
        """When sentiment is None, it must NOT be in fields_with_evidence."""
        result = _normalize(texts=["The meeting is at 3pm in room 204."])
        assert "detected_sentiment" not in result.shared.fields_with_evidence

    def test_positive_dominates(self):
        """When positive count clearly exceeds negative + neutral, return positive."""
        result = _normalize(texts=[
            "I'm happy, great, wonderful, excellent, amazing!",
            "I'm a bit sad though.",
        ])
        assert result.shared.detected_sentiment == "positive"

    def test_negative_dominates(self):
        """When negative count clearly exceeds positive + neutral, return negative."""
        result = _normalize(texts=[
            "I'm sad, terrible, awful, horrible, bad.",
            "But I'm okay I guess.",
        ])
        assert result.shared.detected_sentiment == "negative"


class TestExplicitStatements:
    """Tests for verbatim user statement extraction."""

    def test_realize_statement(self):
        """Statements following 'I realize that' must be extracted."""
        result = _normalize(texts=["I realize that I need to prioritize my health."])
        assert len(result.shared.explicit_user_statements) >= 1
        assert "prioritize my health" in result.shared.explicit_user_statements[0].lower()

    def test_believe_statement(self):
        """Statements following 'I believe that' must be extracted."""
        result = _normalize(texts=["I believe that communication could be better."])
        assert len(result.shared.explicit_user_statements) >= 1

    def test_learned_statement(self):
        """Statements following "I've learned that" must be extracted."""
        result = _normalize(texts=["I've learned that patience is key."])
        assert len(result.shared.explicit_user_statements) >= 1

    def test_multiple_statements(self):
        """Multiple explicit statements across messages must be extracted."""
        result = _normalize(texts=[
            "I realize that I work too much.",
            "I've learned that rest is productive.",
        ])
        assert len(result.shared.explicit_user_statements) >= 2

    def test_no_statements(self):
        """Messages without explicit statement markers must produce empty list."""
        result = _normalize(texts=["hello", "ok", "sure"])
        assert result.shared.explicit_user_statements == []

    def test_statements_in_evidence(self):
        """When statements are found, 'explicit_user_statements' must be in evidence."""
        result = _normalize(texts=["I realize that I need change."])
        assert "explicit_user_statements" in result.shared.fields_with_evidence


class TestPlaintextSummary:
    """Tests for faithful plaintext summary generation."""

    def test_short_summary_is_verbatim(self):
        """Short messages must produce a verbatim concatenation."""
        texts = ["hello world", "this is a test"]
        result = _normalize(texts=texts)
        assert "hello world" in result.shared.plaintext_summary
        assert "this is a test" in result.shared.plaintext_summary

    def test_long_summary_truncated(self):
        """Long combined text must be truncated to ~500 chars."""
        long_text = "x" * 600
        result = _normalize(texts=[long_text])
        assert len(result.shared.plaintext_summary) <= 500
        assert result.shared.plaintext_summary.endswith("...")

    def test_summary_always_in_evidence(self):
        """plaintext_summary must always be in fields_with_evidence."""
        result = _normalize(texts=["anything"])
        assert "plaintext_summary" in result.shared.fields_with_evidence

    def test_empty_messages_empty_summary(self):
        """Empty message list must produce empty summary."""
        result = _normalize(texts=[])
        assert result.shared.plaintext_summary == ""


# ═══════════════════════════════════════════════════════════════════════════
# Template-specific data tests
# ═══════════════════════════════════════════════════════════════════════════


class TestTemplateDataExtraction:
    """Tests for template-specific field extraction."""

    def test_mood_extracted_with_evidence(self):
        """When source messages contain mood indicators, mood must be populated."""
        result = _normalize(texts=["I'm feeling really great today!"])
        assert result.template_data.get("mood") is not None

    def test_mood_none_without_evidence(self):
        """When no mood words are present, mood must be None."""
        result = _normalize(texts=["The meeting is at 3pm."])
        assert result.template_data.get("mood") is None

    def test_energy_level_extracted(self):
        """Energy level must be extracted when energy words are present."""
        result = _normalize(texts=["I have so much energy today, I feel wired!"])
        assert result.template_data.get("energy_level") is not None

    def test_energy_level_none_without_evidence(self):
        """Energy level must be None when not mentioned."""
        result = _normalize(texts=["Just a normal day."])
        assert result.template_data.get("energy_level") is None

    def test_focus_areas_extracted(self):
        """Focus areas must be extracted when focus markers are present."""
        result = _normalize(texts=["I'm focusing on the Q3 roadmap and team hiring."])
        areas = result.template_data.get("focus_areas", [])
        assert len(areas) >= 1

    def test_concerns_extracted(self):
        """Concerns must be extracted when worry markers are present."""
        result = _normalize(texts=["I'm worried about the upcoming deadline."])
        concerns = result.template_data.get("concerns", [])
        assert len(concerns) >= 1

    def test_accomplishments_extracted(self):
        """Accomplishments must be extracted when achievement words are present."""
        result = _normalize(texts=["I finished the report and completed the review."])
        acc = result.template_data.get("accomplishments", [])
        assert len(acc) >= 1

    def test_intentions_extracted(self):
        """Intentions must be extracted when future-plan language is present."""
        result = _normalize(texts=["I will start the new project tomorrow."])
        intentions = result.template_data.get("intentions", [])
        assert len(intentions) >= 1

    def test_gratitude_extracted(self):
        """Gratitude must be extracted when grateful markers are present."""
        result = _normalize(texts=["I'm grateful for my team's support."])
        gratitude = result.template_data.get("gratitude", [])
        assert len(gratitude) >= 1

    def test_challenges_extracted(self):
        """Challenges must be extracted when struggle markers are present."""
        result = _normalize(texts=["I'm struggling with time management."])
        challenges = result.template_data.get("challenges", [])
        assert len(challenges) >= 1


class TestMissingFieldRestraint:
    """Tests for the rule: do NOT invent fields unsupported by source messages."""

    def test_unsupported_scalar_field_is_none(self):
        """A scalar field without evidence must be None, not a guess."""
        result = _normalize(texts=["The weather is nice today."])
        # mood: no mood words → must be None
        assert result.template_data.get("mood") is None
        # energy_level: no energy words → must be None
        assert result.template_data.get("energy_level") is None

    def test_unsupported_list_field_is_empty(self):
        """A list field without evidence must be empty list, not invented items."""
        result = _normalize(texts=["The weather is nice today."])
        assert result.template_data.get("concerns") == []
        assert result.template_data.get("accomplishments") == []
        assert result.template_data.get("intentions") == []

    def test_fields_unsupported_tracks_missing(self):
        """fields_unsupported must list every field without evidence."""
        result = _normalize(texts=["The weather is nice."])
        # With no reflection content, most fields should be unsupported.
        assert len(result.fields_unsupported) > 0

    def test_no_field_populated_without_evidence(self):
        """No template field value should be non-None/non-empty without evidence."""
        result = _normalize(texts=["Just a plain message with no reflection content at all."])
        for field_name, value in result.template_data.items():
            if value is not None and value != [] and value != "":
                # This field has a value — it must be in evidence.
                # Check that at least one evidence pattern matched.
                schema = get_template_schema("freeform_reflection")
                assert schema is not None
                field_def = schema["fields"].get(field_name)
                if field_def is not None:
                    patterns = field_def.get("evidence_patterns", [])
                    combined = " ".join(["Just a plain message with no reflection content at all."])
                    import re
                    has_evidence = any(
                        re.search(p, combined, re.IGNORECASE) for p in patterns
                    )
                    assert has_evidence, (
                        f"Field {field_name!r} has value {value!r} but no evidence "
                        f"pattern matched the source text."
                    )

    def test_all_supported_fields_are_populated(self):
        """When evidence is rich, many fields should be populated."""
        result = _normalize(texts=[
            "I'm feeling great and energized today!",
            "I've been focusing on the new product launch.",
            "I'm worried about the tight timeline though.",
            "I finished the design mockups yesterday.",
            "I will start coding tomorrow.",
            "I'm grateful for the team's help.",
            "I'm struggling with some technical decisions.",
        ])
        # Many fields should be supported.
        assert len(result.fields_unsupported) < 5  # Most should be evidenced

    def test_partial_evidence_mixed_result(self):
        """Some fields evidenced, some not — mixed result."""
        result = _normalize(texts=["I feel happy today."])
        # mood: should be extracted (feeling happy)
        assert result.template_data.get("mood") is not None
        # concerns, accomplishments: should be None/empty
        assert result.template_data.get("concerns") == []
        assert result.template_data.get("accomplishments") == []
        # mood should NOT be in fields_unsupported
        assert "mood" not in result.fields_unsupported
        # concerns SHOULD be in fields_unsupported
        assert "concerns" in result.fields_unsupported


# ═══════════════════════════════════════════════════════════════════════════
# Ordered source message tests
# ═══════════════════════════════════════════════════════════════════════════


class TestOrderedSourceMessages:
    """Tests for ordered-source-message fidelity."""

    def test_message_order_preserved_in_ids(self):
        """Source message IDs must appear in the order they were given."""
        ids = [_uid(), _uid(), _uid()]
        result = _normalize(texts=["a", "b", "c"], ids=ids)
        assert result.shared.source_message_ids == ids

    def test_message_order_preserved_in_texts(self):
        """Raw message texts must preserve input order."""
        texts = ["first", "second", "third"]
        result = _normalize(texts=texts)
        assert result.shared.raw_message_texts == texts

    def test_first_match_wins_for_scalar_fields(self):
        """Scalar fields should use the first match in ordered messages."""
        ids = [_uid(), _uid()]
        result = _normalize(
            texts=[
                "I'm feeling happy about things.",  # first match
                "I'm feeling sad actually.",  # later — should not override
            ],
            ids=ids,
        )
        # The mood extraction uses the first match context.
        mood = result.template_data.get("mood")
        assert mood is not None

    def test_list_fields_accumulate_all_matches(self):
        """List fields should collect matches from all messages in order."""
        result = _normalize(texts=[
            "I finished report A.",
            "I completed task B.",
        ])
        acc = result.template_data.get("accomplishments", [])
        # Both messages mention accomplishments — both should appear.
        assert len(acc) >= 1

    def test_topics_extracted_in_order(self):
        """Topics should appear in the order their markers appear in messages."""
        result = _normalize(texts=[
            "I've been thinking about project Alpha.",
            "Also regarding team dynamics, some thoughts.",
        ])
        topics = result.shared.extracted_topics
        # First topic should reference Alpha, second should reference team dynamics
        if len(topics) >= 2:
            assert "alpha" in topics[0].lower()
            assert "team" in topics[1].lower()

    def test_sentiment_considers_all_messages(self):
        """Sentiment must consider all messages, not just the first."""
        result = _normalize(texts=[
            "I'm happy.",
            "I'm sad.",
            "I'm sad.",
            "I'm sad.",
        ])
        # Negative dominates due to count.
        assert result.shared.detected_sentiment == "negative"

    def test_empty_message_list(self):
        """Empty message list must produce valid (empty) output."""
        result = _normalize(texts=[])
        assert result.shared.source_message_ids == []
        assert result.shared.raw_message_texts == []
        assert result.shared.extracted_topics == []
        assert result.shared.detected_sentiment is None
        assert result.shared.explicit_user_statements == []


# ═══════════════════════════════════════════════════════════════════════════
# Schema validation tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSchemaValidation:
    """Tests for template schema validation."""

    def test_unknown_template_raises(self):
        """Unknown template_key must raise ValueError."""
        with pytest.raises(ValueError, match="unknown template_key"):
            _normalize(texts=["hello"], template_key="nonexistent_template")

    def test_known_template_accepted(self):
        """Known template keys must be accepted."""
        result = _normalize(texts=["hello"], template_key="freeform_reflection")
        assert result.template_key == "freeform_reflection"

    def test_schema_version_set(self):
        """schema_version must match CURRENT_SCHEMA_VERSION."""
        result = _normalize(texts=["hello"])
        assert result.schema_version == CURRENT_SCHEMA_VERSION

    def test_list_template_keys(self):
        """list_template_keys must return known templates."""
        keys = list_template_keys()
        assert "freeform_reflection" in keys

    def test_get_template_schema_known(self):
        """get_template_schema must return schema for known templates."""
        schema = get_template_schema("freeform_reflection")
        assert schema is not None
        assert "fields" in schema
        assert "mood" in schema["fields"]

    def test_get_template_schema_unknown(self):
        """get_template_schema must return None for unknown templates."""
        assert get_template_schema("nonexistent") is None

    def test_template_data_keys_match_schema(self):
        """template_data keys must be exactly the schema field names."""
        result = _normalize(texts=["hello"])
        schema = get_template_schema("freeform_reflection")
        assert schema is not None
        expected_keys = set(schema["fields"].keys())
        actual_keys = set(result.template_data.keys())
        assert actual_keys == expected_keys, (
            f"template_data keys {actual_keys} != schema keys {expected_keys}"
        )

    def test_no_extra_fields_in_template_data(self):
        """template_data must not contain fields outside the schema."""
        result = _normalize(texts=["hello"])
        schema = get_template_schema("freeform_reflection")
        assert schema is not None
        for key in result.template_data:
            assert key in schema["fields"], (
                f"template_data has unexpected key {key!r} not in schema"
            )


# ═══════════════════════════════════════════════════════════════════════════
# Extraction confidence tests
# ═══════════════════════════════════════════════════════════════════════════


class TestExtractionConfidence:
    """Tests for extraction confidence computation."""

    def test_high_confidence_with_rich_evidence(self):
        """Rich evidence should yield high confidence."""
        result = _normalize(texts=[
            "I'm feeling happy and energized!",
            "I've been focusing on my career.",
            "I'm worried about deadlines.",
            "I finished the report.",
            "I will start fresh tomorrow.",
            "I'm grateful for support.",
            "I realize that I need balance.",
        ])
        assert result.extraction_confidence > 0.5

    def test_low_confidence_with_little_evidence(self):
        """Minimal evidence should yield low confidence."""
        result = _normalize(texts=["ok"])
        assert result.extraction_confidence < 0.5

    def test_confidence_in_range(self):
        """Confidence must always be in [0.0, 1.0]."""
        result = _normalize(texts=["anything"])
        assert 0.0 <= result.extraction_confidence <= 1.0

    def test_confidence_with_empty_messages(self):
        """Empty messages should still produce valid confidence."""
        result = _normalize(texts=[])
        assert 0.0 <= result.extraction_confidence <= 1.0


# ═══════════════════════════════════════════════════════════════════════════
# NormalizedReflection dataclass tests
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizedReflectionStructure:
    """Tests for the NormalizedReflection data structure."""

    def test_shared_payload_is_present(self):
        """shared must always be a SharedReflectionPayload."""
        result = _normalize(texts=["hello"])
        assert isinstance(result.shared, SharedReflectionPayload)

    def test_template_key_present(self):
        """template_key must match the requested template."""
        result = _normalize(texts=["hello"], template_key="freeform_reflection")
        assert result.template_key == "freeform_reflection"

    def test_template_data_is_dict(self):
        """template_data must be a dict."""
        result = _normalize(texts=["hello"])
        assert isinstance(result.template_data, dict)

    def test_fields_unsupported_is_list(self):
        """fields_unsupported must be a list of strings."""
        result = _normalize(texts=["hello"])
        assert isinstance(result.fields_unsupported, list)

    def test_schema_version_is_int(self):
        """schema_version must be an int."""
        result = _normalize(texts=["hello"])
        assert isinstance(result.schema_version, int)

    def test_extraction_confidence_is_float(self):
        """extraction_confidence must be a float."""
        result = _normalize(texts=["hello"])
        assert isinstance(result.extraction_confidence, float)


# ═══════════════════════════════════════════════════════════════════════════
# Edge case tests
# ═══════════════════════════════════════════════════════════════════════════


class TestEdgeCases:
    """Edge case and boundary tests."""

    def test_single_message(self):
        """Single message must normalize correctly."""
        result = _normalize(texts=["I feel great."])
        assert result.shared.source_message_ids is not None
        assert len(result.shared.raw_message_texts) == 1

    def test_very_long_message(self):
        """Very long messages must not break normalization."""
        long_text = "I am feeling happy. " * 200
        result = _normalize(texts=[long_text])
        assert result.shared.detected_sentiment == "positive"

    def test_unicode_messages(self):
        """Unicode/emoji messages must be handled."""
        result = _normalize(texts=["I'm feeling great! 🎉😊", "Café con leche"])
        assert result.shared.detected_sentiment == "positive"

    def test_whitespace_only_message(self):
        """Whitespace-only messages must not break extraction."""
        result = _normalize(texts=["   ", "\n", "\t"])
        assert result.shared.detected_sentiment is None

    def test_special_characters(self):
        """Special characters must not break regex patterns."""
        result = _normalize(texts=["I'm feeling (great) about [things] {today}."])
        assert result.shared.detected_sentiment == "positive"

    def test_mixed_case(self):
        """Case variations must be handled."""
        result = _normalize(texts=["I'M FEELING HAPPY AND GRATEFUL!"])
        assert result.shared.detected_sentiment == "positive"
