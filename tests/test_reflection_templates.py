"""Focused tests for the reflection template registry and payload validation.

Covers the validation contract (T5):
- Valid entry and derivation payloads
- Missing required fields
- Unknown templates
- Incompatible versions
- Normalization behavior before storage
"""

from __future__ import annotations

import pytest

from app.services.reflection_templates import (
    IncompatibleTemplateVersionError,
    ReflectionTemplate,
    TemplateValidationError,
    UnknownTemplateError,
    _normalize_envelope,
    _validate_envelope_shape,
    get_template,
    list_template_keys,
    register_template,
    template_is_registered,
    validate_derivation_payload,
    validate_entry_payload,
)

# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


class TestRegistry:
    """Unit tests for the template registry lookup and registration."""

    # -- list_template_keys ---------------------------------------------------

    def test_list_template_keys_returns_all_builtins(self):
        keys = list_template_keys()
        assert isinstance(keys, list)
        # All built-in template keys from reflection_templates.py.
        expected = {
            "checkpoint",
            "daily_close",
            "daily_open",
            "decision_debrief",
            "freeform",
            "freeform_reflection",
            "monthly_close",
            "monthly_open",
            "weekly_close",
            "weekly_open",
        }
        missing = expected - set(keys)
        assert not missing, f"Expected built-in keys missing: {missing}"

    # -- template_is_registered -----------------------------------------------

    def test_template_is_registered_true_for_builtins(self):
        assert template_is_registered("freeform") is True
        assert template_is_registered("freeform_reflection") is True
        assert template_is_registered("daily_open") is True
        assert template_is_registered("weekly_close") is True

    def test_template_is_registered_false_for_unknown(self):
        assert template_is_registered("nonexistent") is False
        assert template_is_registered("") is False

    # -- get_template (happy path) --------------------------------------------

    def test_get_template_returns_builtin_with_explicit_version(self):
        tmpl = get_template("freeform", version=1)
        assert isinstance(tmpl, ReflectionTemplate)
        assert tmpl.key == "freeform"
        assert tmpl.version == 1

    def test_get_template_defaults_to_latest_with_version_none(self):
        tmpl = get_template("freeform")
        assert tmpl.key == "freeform"
        assert tmpl.version == 1

    def test_get_template_returns_freeform_reflection_alias(self):
        tmpl = get_template("freeform_reflection")
        assert isinstance(tmpl, ReflectionTemplate)
        assert tmpl.key == "freeform_reflection"
        assert tmpl.version == 1

    # -- get_template (error paths) -------------------------------------------

    def test_get_template_unknown_key_raises_unknown_template_error(self):
        with pytest.raises(UnknownTemplateError) as exc:
            get_template("nonexistent")
        assert "nonexistent" in str(exc.value)
        # Error message should mention registered templates
        assert "Registered templates" in str(exc.value)

    def test_get_template_incompatible_version_raises_incompatible_error(self):
        # version 999 does not exist for any builtin
        with pytest.raises(IncompatibleTemplateVersionError) as exc:
            get_template("freeform", version=999)
        assert "freeform" in str(exc.value)
        assert "999" in str(exc.value)
        assert "Latest version" in str(exc.value)

    # -- register_template ----------------------------------------------------

    def test_register_template_duplicate_identical_is_idempotent(self):
        """Re-registering the same descriptor should not raise."""
        # Use a fresh key to avoid needing to match built-in internals.
        fresh = ReflectionTemplate(
            key="test_idempotent",
            version=1,
            validate_payload=lambda p: None,
        )
        register_template(fresh)
        # Re-registering the identical descriptor is silently ignored.
        register_template(fresh)

    def test_register_template_duplicate_different_raises_value_error(self):
        """Registering a different descriptor for the same (key, version) must raise."""
        different = ReflectionTemplate(
            key="freeform",
            version=1,
            allowed_temporal_scopes=frozenset({"instant"}),
        )
        with pytest.raises(ValueError, match="already registered"):
            register_template(different)


# ---------------------------------------------------------------------------
# Envelope validation (shared shape checks)
# ---------------------------------------------------------------------------


class TestEnvelopeShape:
    """Tests for the shared envelope validator used by all templates."""

    def test_valid_empty_payload_passes(self):
        """Empty dict is always a valid envelope (all keys optional)."""
        _validate_envelope_shape({})  # no exception

    def test_valid_full_envelope_passes(self):
        payload = {
            "summary": "test",
            "facts": ["fact1"],
            "events": ["event1"],
            "decisions": ["dec1"],
            "priorities": ["p1"],
            "wins": ["win1"],
            "blockers": ["blocker1"],
            "open_loops": ["loop1"],
            "questions": ["q1"],
            "signals": {"a": 1},
            "template_data": {"k": "v"},
        }
        _validate_envelope_shape(payload)  # no exception

    def test_unknown_key_raises_template_validation_error(self):
        with pytest.raises(TemplateValidationError) as exc:
            _validate_envelope_shape({"unknown_field": "value"})
        assert "unknown_field" in str(exc.value)
        assert "Allowed keys" in str(exc.value)

    def test_list_key_wrong_type_raises(self):
        with pytest.raises(TemplateValidationError, match="must be a list"):
            _validate_envelope_shape({"facts": "not_a_list"})

    def test_dict_key_wrong_type_raises(self):
        with pytest.raises(TemplateValidationError, match="must be a dict"):
            _validate_envelope_shape({"signals": "not_a_dict"})

    def test_summary_wrong_type_raises(self):
        with pytest.raises(TemplateValidationError, match="must be a string"):
            _validate_envelope_shape({"summary": 123})

    def test_non_dict_raises(self):
        with pytest.raises(TemplateValidationError, match="must be a dict"):
            _validate_envelope_shape("not_a_dict")  # type: ignore[arg-type]

    def test_list_key_none_is_allowed(self):
        """None for list keys should not raise (absent/None is ok)."""
        _validate_envelope_shape({"facts": None})  # no exception

    def test_dict_key_none_is_allowed(self):
        _validate_envelope_shape({"signals": None})  # no exception

    def test_summary_none_is_allowed(self):
        _validate_envelope_shape({"summary": None})  # no exception


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class TestNormalization:
    """Tests for envelope normalization before storage."""

    def test_missing_list_keys_filled_with_empty_list(self):
        normalized = _normalize_envelope({"summary": "s"})
        assert normalized["facts"] == []
        assert normalized["events"] == []
        assert normalized["decisions"] == []
        assert normalized["priorities"] == []
        assert normalized["wins"] == []
        assert normalized["blockers"] == []
        assert normalized["open_loops"] == []
        assert normalized["questions"] == []

    def test_missing_dict_keys_filled_with_empty_dict(self):
        normalized = _normalize_envelope({"summary": "s"})
        assert normalized["signals"] == {}
        assert normalized["template_data"] == {}

    def test_missing_summary_filled_with_none(self):
        normalized = _normalize_envelope({})
        assert normalized["summary"] is None

    def test_all_eleven_keys_present_after_normalization(self):
        """Normalized payload always has exactly 11 keys (the shared envelope)."""
        normalized = _normalize_envelope({})
        assert set(normalized.keys()) == {
            "summary",
            "facts",
            "events",
            "decisions",
            "priorities",
            "wins",
            "blockers",
            "open_loops",
            "questions",
            "signals",
            "template_data",
        }
        assert len(normalized) == 11

    def test_unknown_keys_are_dropped(self):
        """Keys outside the shared envelope are silently removed."""
        normalized = _normalize_envelope({"unknown": "x", "summary": "s"})
        assert "unknown" not in normalized
        assert normalized["summary"] == "s"

    def test_explicit_none_list_becomes_empty_list(self):
        """An explicit None for a list key is normalized to [] (zero value)."""
        normalized = _normalize_envelope({"facts": None})
        assert normalized["facts"] == []

    def test_provided_values_are_preserved(self):
        payload = {"summary": "hello", "facts": ["a", "b"], "signals": {"x": 1}}
        normalized = _normalize_envelope(payload)
        assert normalized["summary"] == "hello"
        assert normalized["facts"] == ["a", "b"]
        assert normalized["signals"] == {"x": 1}

    def test_keys_are_sorted_in_output(self):
        """Normalized output has keys in sorted order."""
        normalized = _normalize_envelope({})
        keys = list(normalized.keys())
        assert keys == sorted(keys)


# ---------------------------------------------------------------------------
# Entry payload validation (happy paths)
# ---------------------------------------------------------------------------


class TestValidateEntryPayloadHappy:
    """Valid entry payloads across built-in templates."""

    def test_valid_freeform_payload(self):
        result = validate_entry_payload("freeform", {"summary": "Thinking about things"})
        assert result["summary"] == "Thinking about things"
        assert result["facts"] == []

    def test_valid_daily_open_payload(self):
        result = validate_entry_payload("daily_open", {"priorities": ["p1", "p2"]})
        assert result["priorities"] == ["p1", "p2"]

    def test_valid_daily_close_payload(self):
        result = validate_entry_payload("daily_close", {"wins": ["done"], "blockers": []})
        assert result["wins"] == ["done"]
        assert result["blockers"] == []

    def test_valid_weekly_open_payload(self):
        result = validate_entry_payload("weekly_open", {"priorities": ["big goal"]})
        assert result["priorities"] == ["big goal"]

    def test_valid_weekly_close_payload(self):
        result = validate_entry_payload("weekly_close", {"wins": ["shipped feature"]})
        assert result["wins"] == ["shipped feature"]

    def test_valid_monthly_open_payload(self):
        result = validate_entry_payload("monthly_open", {})
        assert result["summary"] is None

    def test_valid_monthly_close_payload(self):
        result = validate_entry_payload("monthly_close", {"summary": "Month in review"})
        assert result["summary"] == "Month in review"

    def test_valid_decision_debrief_payload(self):
        result = validate_entry_payload(
            "decision_debrief",
            {"decisions": ["chose A over B"], "signals": {"confidence": "high"}},
        )
        assert result["decisions"] == ["chose A over B"]

    def test_valid_checkpoint_payload(self):
        result = validate_entry_payload("checkpoint", {"open_loops": ["follow up X"]})
        assert result["open_loops"] == ["follow up X"]

    def test_empty_payload_is_valid_for_all_templates(self):
        """Empty payload should validate for every built-in template."""
        for key in list_template_keys():
            result = validate_entry_payload(key, {})
            assert isinstance(result, dict)
            assert len(result) == 11  # normalized envelope


# ---------------------------------------------------------------------------
# Entry payload validation (error paths)
# ---------------------------------------------------------------------------


class TestValidateEntryPayloadErrors:
    """Error paths for entry payload validation."""

    def test_unknown_template_raises_unknown_template_error(self):
        with pytest.raises(UnknownTemplateError):
            validate_entry_payload("nonexistent", {})

    def test_incompatible_version_raises(self):
        with pytest.raises(IncompatibleTemplateVersionError):
            validate_entry_payload("freeform", {}, version=999)

    def test_bad_envelope_unknown_key_raises(self):
        with pytest.raises(TemplateValidationError):
            validate_entry_payload("freeform", {"bad_key": True})

    def test_bad_envelope_wrong_type_raises(self):
        with pytest.raises(TemplateValidationError):
            validate_entry_payload("freeform", {"facts": "not_list"})

    def test_template_data_not_dict_raises(self):
        """Built-in templates validate that template_data is a dict if present."""
        with pytest.raises(TemplateValidationError, match="template_data"):
            validate_entry_payload("freeform", {"template_data": "not_a_dict"})

    def test_non_dict_payload_raises(self):
        with pytest.raises(TemplateValidationError, match="must be a dict"):
            validate_entry_payload("freeform", "not_a_dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Derivation payload validation (happy paths)
# ---------------------------------------------------------------------------


class TestValidateDerivationPayloadHappy:
    """Valid derivation decision payloads."""

    def test_minimal_valid_payload(self):
        result = validate_derivation_payload({
            "derivation_kind": "memory",
            "assertion_source": "user_explicit",
        })
        assert result["derivation_kind"] == "memory"
        assert result["assertion_source"] == "user_explicit"
        # Defaults
        assert result["decision"] == "deferred"
        assert result["confidence"] is None
        assert result["supporting_message_ids"] == []
        assert result["eligibility_reasons"] == []

    def test_full_valid_payload(self):
        payload = {
            "derivation_kind": "observation",
            "assertion_source": "agent_inferred",
            "decision": "applied",
            "confidence": 0.95,
            "supporting_message_ids": ["msg-1", "msg-2"],
            "eligibility_reasons": ["pattern match", "user confirmed"],
        }
        result = validate_derivation_payload(payload)
        assert result["derivation_kind"] == "observation"
        assert result["assertion_source"] == "agent_inferred"
        assert result["decision"] == "applied"
        assert result["confidence"] == 0.95
        assert result["supporting_message_ids"] == ["msg-1", "msg-2"]
        assert result["eligibility_reasons"] == ["pattern match", "user confirmed"]

    def test_all_valid_derivation_kinds(self):
        for kind in ("memory", "observation", "distillation", "orientation"):
            result = validate_derivation_payload({
                "derivation_kind": kind,
                "assertion_source": "user_explicit",
            })
            assert result["derivation_kind"] == kind

    def test_all_valid_assertion_sources(self):
        for source in ("user_explicit", "user_implied", "agent_inferred"):
            result = validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": source,
            })
            assert result["assertion_source"] == source

    def test_all_valid_decisions(self):
        for decision in ("applied", "reinforced", "deferred", "rejected", "superseded"):
            result = validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "user_explicit",
                "decision": decision,
            })
            assert result["decision"] == decision

    def test_confidence_boundary_zero(self):
        result = validate_derivation_payload({
            "derivation_kind": "memory",
            "assertion_source": "user_explicit",
            "confidence": 0.0,
        })
        assert result["confidence"] == 0.0

    def test_confidence_boundary_one(self):
        result = validate_derivation_payload({
            "derivation_kind": "memory",
            "assertion_source": "user_explicit",
            "confidence": 1.0,
        })
        assert result["confidence"] == 1.0

    def test_confidence_int_accepted(self):
        """Confidence as int 0 or 1 is also allowed."""
        result = validate_derivation_payload({
            "derivation_kind": "memory",
            "assertion_source": "user_explicit",
            "confidence": 1,
        })
        assert result["confidence"] == 1


# ---------------------------------------------------------------------------
# Derivation payload validation (error paths)
# ---------------------------------------------------------------------------


class TestValidateDerivationPayloadErrors:
    """Error paths for derivation payload validation."""

    def test_missing_derivation_kind_raises(self):
        with pytest.raises(TemplateValidationError, match="derivation_kind"):
            validate_derivation_payload({"assertion_source": "user_explicit"})

    def test_missing_assertion_source_raises(self):
        with pytest.raises(TemplateValidationError, match="assertion_source"):
            validate_derivation_payload({"derivation_kind": "memory"})

    def test_empty_derivation_kind_raises(self):
        with pytest.raises(TemplateValidationError, match="derivation_kind"):
            validate_derivation_payload({
                "derivation_kind": "",
                "assertion_source": "user_explicit",
            })

    def test_empty_assertion_source_raises(self):
        with pytest.raises(TemplateValidationError, match="assertion_source"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "",
            })

    def test_invalid_derivation_kind_raises(self):
        with pytest.raises(TemplateValidationError, match="derivation_kind"):
            validate_derivation_payload({
                "derivation_kind": "bogus",
                "assertion_source": "user_explicit",
            })

    def test_invalid_assertion_source_raises(self):
        with pytest.raises(TemplateValidationError, match="assertion_source"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "bogus",
            })

    def test_invalid_decision_raises(self):
        with pytest.raises(TemplateValidationError, match="decision"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "user_explicit",
                "decision": "bogus",
            })

    def test_confidence_below_zero_raises(self):
        with pytest.raises(TemplateValidationError, match="confidence"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "user_explicit",
                "confidence": -0.1,
            })

    def test_confidence_above_one_raises(self):
        with pytest.raises(TemplateValidationError, match="confidence"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "user_explicit",
                "confidence": 1.1,
            })

    def test_confidence_wrong_type_raises(self):
        with pytest.raises(TemplateValidationError, match="confidence"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "user_explicit",
                "confidence": "high",
            })

    def test_supporting_message_ids_not_list_raises(self):
        with pytest.raises(TemplateValidationError, match="supporting_message_ids"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "user_explicit",
                "supporting_message_ids": "not_a_list",
            })

    def test_eligibility_reasons_not_list_raises(self):
        with pytest.raises(TemplateValidationError, match="eligibility_reasons"):
            validate_derivation_payload({
                "derivation_kind": "memory",
                "assertion_source": "user_explicit",
                "eligibility_reasons": "not_a_list",
            })

    def test_non_dict_payload_raises(self):
        with pytest.raises(TemplateValidationError, match="must be a dict"):
            validate_derivation_payload("not_a_dict")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Derivation payload normalization (storage shape)
# ---------------------------------------------------------------------------


class TestDerivationNormalization:
    """Normalization of derivation payloads before storage."""

    def test_missing_optional_fields_get_defaults(self):
        result = validate_derivation_payload({
            "derivation_kind": "distillation",
            "assertion_source": "agent_inferred",
        })
        assert result["decision"] == "deferred"
        assert result["confidence"] is None
        assert result["supporting_message_ids"] == []
        assert result["eligibility_reasons"] == []

    def test_explicit_decision_default_works(self):
        """When decision is omitted, it defaults to 'deferred'."""
        result = validate_derivation_payload({
            "derivation_kind": "memory",
            "assertion_source": "user_explicit",
        })
        assert result["decision"] == "deferred"

    def test_all_six_keys_present_after_normalization(self):
        result = validate_derivation_payload({
            "derivation_kind": "memory",
            "assertion_source": "user_explicit",
        })
        assert set(result.keys()) == {
            "derivation_kind",
            "assertion_source",
            "decision",
            "confidence",
            "supporting_message_ids",
            "eligibility_reasons",
        }

    def test_extra_keys_are_preserved(self):
        """Extra keys not in the derivation spec are passed through."""
        result = validate_derivation_payload({
            "derivation_kind": "memory",
            "assertion_source": "user_explicit",
            "extra_field": "kept",
        })
        assert result["extra_field"] == "kept"


# ---------------------------------------------------------------------------
# Cross-cutting: validate_entry_payload → normalization
# ---------------------------------------------------------------------------


class TestEntryPayloadNormalizationIntegration:
    """End-to-end: validate_entry_payload returns normalized shape."""

    def test_normalized_output_has_all_eleven_keys(self):
        result = validate_entry_payload("freeform", {"summary": "test"})
        assert len(result) == 11
        assert "open_loops" in result
        assert result["open_loops"] == []

    def test_unknown_keys_rejected_at_entry_validation(self):
        """validate_entry_payload rejects unknown keys before normalization."""
        with pytest.raises(TemplateValidationError, match="Unknown payload key"):
            validate_entry_payload("freeform", {"summary": "s", "extra": "rejected"})

    def test_version_defaults_to_latest_during_validation(self):
        """When version is omitted, latest registered version is used automatically."""
        result = validate_entry_payload("freeform", {"summary": "v1 payload"})
        assert result["summary"] == "v1 payload"


# ---------------------------------------------------------------------------
# ReflectionTemplate descriptor behaviour
# ---------------------------------------------------------------------------


class TestReflectionTemplateDescriptor:
    """Behaviour of the ReflectionTemplate frozen dataclass itself."""

    def test_frozen_dataclass_is_hashable(self):
        t1 = ReflectionTemplate(key="test", version=1)
        t2 = ReflectionTemplate(key="test", version=1)
        assert hash(t1) == hash(t2)
        assert t1 == t2

    def test_defaults_are_sane(self):
        tmpl = ReflectionTemplate(key="test")
        assert tmpl.version == 1
        assert isinstance(tmpl.allowed_temporal_scopes, frozenset)
        assert isinstance(tmpl.allowed_phases, frozenset)
        assert tmpl.validate_payload is None
        assert tmpl.normalize_payload is None
