"""Tests for the expanded failure-class taxonomy (Project C, C3).

The module is pure logic with no DB dependencies, so these are plain unit
tests.  Coverage matrix:

* Every ``FailureClass`` member has a ``FAILURE_POLICY`` entry.
* ``classify`` returns the correct enum for every key in the live
  inbound_queue ``FAILURE_REASON_TO_CLASS`` mapping.
* ``classify`` falls back to ``INFRA_BUG`` for unknown / None reasons.
* Retryability flags match the documented expectations.
* The seven enum members carry the expected string values (preserves the
  legacy three so DB CHECK constraints still pass when callers use the
  enum).
"""

from __future__ import annotations

import pytest

from app.services.failure_policy import (
    FAILURE_POLICY,
    FailureClass,
    RetryPolicy,
    classify,
    is_retryable,
)
from app.services.inbound_queue import FAILURE_REASON_TO_CLASS


# ── shape / coverage ─────────────────────────────────────────────────────────


def test_failure_class_has_exactly_seven_members() -> None:
    assert len(FailureClass) == 7


def test_failure_class_string_values_preserve_legacy_three() -> None:
    """The legacy column values must still parse via the enum."""
    assert FailureClass.RETRYABLE_PRE_SEND.value == "retryable_pre_send"
    assert FailureClass.TERMINAL_POST_SEND.value == "terminal_post_send"
    assert FailureClass.INFRA_BUG.value == "infra_bug"
    # String equality works because FailureClass inherits from str.
    assert FailureClass.INFRA_BUG == "infra_bug"


def test_every_enum_member_has_a_policy_entry() -> None:
    missing = [fc for fc in FailureClass if fc not in FAILURE_POLICY]
    assert missing == [], f"Missing FAILURE_POLICY entries for: {missing}"
    # And no stray policy entries pointing at non-members.
    extras = [k for k in FAILURE_POLICY if k not in FailureClass]
    assert extras == [], f"Stray FAILURE_POLICY entries: {extras}"


def test_policy_entries_have_sane_types() -> None:
    for fc, policy in FAILURE_POLICY.items():
        assert isinstance(policy, RetryPolicy), fc
        assert isinstance(policy.retryable, bool), fc
        assert isinstance(policy.default_retry_delay_seconds, int), fc
        assert policy.default_retry_delay_seconds >= 0, fc
        assert policy.max_attempts is None or policy.max_attempts > 0, fc


# ── classify() against the live inbound_queue table ──────────────────────────


def test_classify_covers_every_live_failure_reason() -> None:
    """Every key in inbound_queue.FAILURE_REASON_TO_CLASS resolves to a
    FailureClass — i.e. C3 is a superset of A1/A2."""
    for reason in FAILURE_REASON_TO_CLASS:
        fc = classify(reason)
        assert isinstance(fc, FailureClass), reason


def test_classify_specific_known_reasons() -> None:
    """Spot-check a handful so a future taxonomy drift is loud."""
    assert classify("provider_send_failed") == FailureClass.RETRYABLE_PRE_SEND
    assert classify("llm_timeout") == FailureClass.MODEL_PROVIDER_TIMEOUT
    assert classify("tool_validation_recoverable_exhausted") == (
        FailureClass.TOOL_VALIDATION_RECOVERABLE
    )
    assert classify("unsupported_chain_anthropic_to_deepseek") == (
        FailureClass.INFRA_BUG
    )
    assert classify("crashed_after_send") == FailureClass.TERMINAL_POST_SEND


def test_classify_fallback_for_unknown_reason() -> None:
    assert classify("totally-made-up-reason") == FailureClass.INFRA_BUG
    assert classify("") == FailureClass.INFRA_BUG


def test_classify_handles_none() -> None:
    # type: ignore[arg-type] — defensive guard for stale call sites.
    assert classify(None) == FailureClass.INFRA_BUG  # type: ignore[arg-type]


# ── retryability ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "fc,expected",
    [
        (FailureClass.RETRYABLE_PRE_SEND, True),
        (FailureClass.TERMINAL_POST_SEND, False),
        (FailureClass.INFRA_BUG, False),
        (FailureClass.MODEL_PROVIDER_BAD_REQUEST, True),
        (FailureClass.MODEL_PROVIDER_TIMEOUT, True),
        (FailureClass.TOOL_VALIDATION_RECOVERABLE, True),
        (FailureClass.DELIVERY_PROVIDER_FAILURE, True),
    ],
)
def test_is_retryable_matches_policy(fc: FailureClass, expected: bool) -> None:
    assert is_retryable(fc) is expected
    # Also accepts string form.
    assert is_retryable(fc.value) is expected


def test_is_retryable_rejects_unknown_string() -> None:
    assert is_retryable("not-a-class") is False


# ── inbound_queue.FAILURE_REASON_TO_CLASS legacy class is a subset ──────────


def test_legacy_classes_are_a_subset_of_the_enum() -> None:
    """Every value in the live legacy mapping is also a FailureClass value."""
    enum_values = {fc.value for fc in FailureClass}
    for legacy_class in FAILURE_REASON_TO_CLASS.values():
        assert legacy_class in enum_values, legacy_class
