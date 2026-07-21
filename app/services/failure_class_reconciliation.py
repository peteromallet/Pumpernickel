"""Deterministic failure-class reconciliation layer (M4 / T9).

Background
----------
The system maintains two **independent** failure taxonomies that model
different domains:

* **Reflection-session failures** (4 classes) — stored on
  ``mediator.reflection_sessions.failure_class``, enforced by CHECK
  constraint (migration 0066) and ``ReflectionStore.mark_session_failed()``.
* **Message-level failures** (7 classes: 3 legacy + 4 C3 additive) —
  stored on ``mediator.messages.failure_class``, legacy 3 enforced by
  CHECK constraint (migration 0046), full 7 defined in
  ``app/services/failure_policy.FailureClass``.

Strategy (M4 / T8)
------------------
**Keep both taxonomies independent.**  Do not merge, do not cross-map,
do not create a third taxonomy.

This module is the **single source of truth** for both taxonomies and
provides deterministic domain classification so that every emitted
failure_class value can be traced to exactly one taxonomy.

Usage
-----
    from app.services.failure_class_reconciliation import (
        classify_failure_domain,
        format_failure_class,
        validate_known_failure_class,
        REFLECTION_FAILURE_CLASSES,
        MESSAGE_FAILURE_CLASSES,
    )

    domain = classify_failure_domain("retryable_processor")  # "reflection"
    label  = format_failure_class("retryable_processor")     # "[R] retryable_processor"
    validate_known_failure_class("bogus_value")              # raises ValueError
"""

from __future__ import annotations

from typing import Final, Literal

# ── Canonical taxonomies ─────────────────────────────────────────────────────

REFLECTION_FAILURE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        "retryable_processor",   # transient processor error → retryable
        "terminal_input",        # bad / missing input → terminal
        "terminal_internal",     # internal bug → terminal
        "stale_claim",           # claim timed out → recoverable by sweeper
    }
)

MESSAGE_FAILURE_CLASSES: Final[frozenset[str]] = frozenset(
    {
        # Legacy three (enforced by messages CHECK constraint, migration 0046)
        "retryable_pre_send",
        "terminal_post_send",
        "infra_bug",
        # C3 additive (defined in FailureClass enum, not yet enforced by CHECK)
        "model_provider_bad_request",
        "model_provider_timeout",
        "tool_validation_recoverable",
        "delivery_provider_failure",
    }
)

# Combined set — every value that is a valid failure_class anywhere in the
# system.  Used by ``validate_known_failure_class()``.
_ALL_KNOWN_FAILURE_CLASSES: Final[frozenset[str]] = (
    REFLECTION_FAILURE_CLASSES | MESSAGE_FAILURE_CLASSES
)

FailureDomain = Literal["reflection", "message"]


# ── Domain classification ────────────────────────────────────────────────────


def classify_failure_domain(failure_class: str | None) -> FailureDomain | None:
    """Return the domain a *failure_class* value belongs to.

    Args:
        failure_class: The string value to classify (may be None).

    Returns:
        ``"reflection"`` if the value is a reflection-session failure class,
        ``"message"`` if it is a message-level failure class,
        ``None`` if the value is ``None`` or not recognised.
    """
    if failure_class is None:
        return None
    if not isinstance(failure_class, str):
        return None
    if failure_class in REFLECTION_FAILURE_CLASSES:
        return "reflection"
    if failure_class in MESSAGE_FAILURE_CLASSES:
        return "message"
    return None


# ── Validation ───────────────────────────────────────────────────────────────


def validate_known_failure_class(
    failure_class: str | None,
    *,
    caller: str = "",
) -> str | None:
    """Assert that *failure_class* belongs to a known taxonomy.

    Raises ``ValueError`` if the value is neither a recognised reflection
    failure class nor a recognised message failure class.

    Args:
        failure_class: The value to validate (may be None, which passes).
        caller: Optional caller label included in error messages.

    Returns:
        *failure_class* unchanged, for chaining.

    Raises:
        ValueError: If *failure_class* is not None and not recognised.
    """
    if failure_class is None:
        return None
    if not isinstance(failure_class, str):
        raise ValueError(
            f"failure_class must be a string or None, got {type(failure_class).__name__}"
        )
    if failure_class not in _ALL_KNOWN_FAILURE_CLASSES:
        prefix = f"{caller}: " if caller else ""
        raise ValueError(
            f"{prefix}unrecognised failure_class {failure_class!r}; "
            f"must be one of the known reflection or message failure classes. "
            f"Reflection: {sorted(REFLECTION_FAILURE_CLASSES)}. "
            f"Message: {sorted(MESSAGE_FAILURE_CLASSES)}."
        )
    return failure_class


def validate_reflection_failure_class(
    failure_class: str | None,
    *,
    caller: str = "",
) -> str | None:
    """Assert that *failure_class* is a valid reflection-session failure class.

    This is a domain-specific validator for call sites that MUST use the
    reflection taxonomy (e.g. ``ReflectionStore.mark_session_failed()``).

    Args:
        failure_class: The value to validate (may be None, which passes).
        caller: Optional caller label included in error messages.

    Returns:
        *failure_class* unchanged, for chaining.

    Raises:
        ValueError: If *failure_class* is not None and not a recognised
                    reflection failure class.
    """
    if failure_class is None:
        return None
    if not isinstance(failure_class, str):
        raise ValueError(
            f"failure_class must be a string or None, got {type(failure_class).__name__}"
        )
    if failure_class not in REFLECTION_FAILURE_CLASSES:
        prefix = f"{caller}: " if caller else ""
        raise ValueError(
            f"{prefix}invalid reflection failure_class {failure_class!r}; "
            f"expected one of {sorted(REFLECTION_FAILURE_CLASSES)}"
        )
    return failure_class


# ── Display formatting ───────────────────────────────────────────────────────


def format_failure_class(
    failure_class: str | None,
    *,
    with_domain: bool = True,
) -> str:
    """Return an operator-friendly display string for a failure class.

    Args:
        failure_class: The raw failure_class value (may be None).
        with_domain: If True, prefix with ``[R]`` (reflection) or ``[M]``
                     (message) domain tag.

    Returns:
        A display string, e.g. ``"[R] retryable_processor"`` or
        ``"[M] infra_bug"``.  Returns ``"none"`` for None.
    """
    if failure_class is None:
        return "none"
    domain = classify_failure_domain(failure_class)
    if with_domain:
        tag = {"reflection": "[R]", "message": "[M]"}.get(domain or "", "[?]")
        return f"{tag} {failure_class}"
    return str(failure_class)


# ── Operator-facing category labels ──────────────────────────────────────────


# Human-readable short labels for admin UI / diagnostic output.
# Keys are the raw failure_class string values.
FAILURE_CLASS_LABELS: Final[dict[str, str]] = {
    # Reflection taxonomy
    "retryable_processor":   "Retryable (Processor)",
    "terminal_input":        "Terminal (Bad Input)",
    "terminal_internal":     "Terminal (Internal Bug)",
    "stale_claim":           "Stale Claim",
    # Message taxonomy — legacy
    "retryable_pre_send":    "Retryable (Pre-Send)",
    "terminal_post_send":    "Terminal (Post-Send)",
    "infra_bug":             "Infra Bug",
    # Message taxonomy — C3 additive
    "model_provider_bad_request":       "Model Provider Bad Request",
    "model_provider_timeout":           "Model Provider Timeout",
    "tool_validation_recoverable":      "Tool Validation Recoverable",
    "delivery_provider_failure":        "Delivery Provider Failure",
}


def get_failure_class_label(failure_class: str | None) -> str:
    """Return a human-readable label for a failure class value.

    Args:
        failure_class: The raw value (may be None).

    Returns:
        A short label like ``"Retryable (Processor)"``, or ``"Unknown"`` /
        ``"None"`` for unrecognised / None values.
    """
    if failure_class is None:
        return "None"
    return FAILURE_CLASS_LABELS.get(failure_class, "Unknown")


__all__ = [
    "REFLECTION_FAILURE_CLASSES",
    "MESSAGE_FAILURE_CLASSES",
    "FailureDomain",
    "classify_failure_domain",
    "validate_known_failure_class",
    "validate_reflection_failure_class",
    "format_failure_class",
    "get_failure_class_label",
    "FAILURE_CLASS_LABELS",
]
