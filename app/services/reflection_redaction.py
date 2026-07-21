"""Structured redaction helper for reflection-adjacent diagnostics.

Preserves opaque IDs, user/bot/topic/session scope, state, reason codes,
counts, timestamps, and coverage booleans while omitting sensitive payload
text (canonical_text, plaintext_searchable, payload, summary, correction_note,
raw message bodies, voice transcripts, decrypted reflection content).

This module is the single contract for every exposed reflection diagnostic
field — logs, retry diagnostics, admin output, eval output, and evidence
metadata must all pass through it before external exposure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

# ── Constants ────────────────────────────────────────────────────────────────

REDACTED = "[REDACTED]"
"""Placeholder used when a sensitive field value is suppressed."""

# Fields that are always safe to include verbatim in diagnostics.
# These are opaque identifiers, scope keys, state labels, reason codes,
# counts, timestamps, and coverage booleans.
_SAFE_FIELD_PATTERNS: tuple[str, ...] = (
    # Opaque identifiers
    "id",
    "entry_id",
    "session_id",
    "user_id",
    "bot_id",
    "topic_id",
    "supersedes_entry_id",
    "created_by_turn_id",
    "opened_by_message_id",
    "opened_by_turn_id",
    "source_id",
    "message_id",
    "dyad_id",
    "claimed_by",
    "worker_id",
    "processor_version",
    "idempotency_key",
    # Scope
    "bot",
    "user",
    "topic",
    "session",
    "_user",
    "_bot",
    "_session",
    "_topic",
    # State
    "status",
    "session_status",
    "state",
    "phase",
    "action",
    "decision",
    # Reason codes
    "reason",
    "failure_class",
    "failure_reason",
    "last_error",
    "error_code",
    "classification_source",
    # Counts
    "revision_number",
    "schema_version",
    "retry_count",
    "total_matched",
    "scanned",
    "finalized",
    "abandoned",
    "skipped_active",
    "skipped_idempotent",
    "errors",
    "limit",
    "lookback_days",
    "max_entries",
    # Timestamps
    "opened_at",
    "finalized_at",
    "processed_at",
    "abandoned_at",
    "created_at",
    "updated_at",
    "sent_at",
    "source_created_at",
    "source_updated_at",
    "period_start",
    "period_end",
    "idle_finalize_at",
    # Coverage / flags
    "include_internals",
    "current_only",
    "is_open_loop",
    "is_error",
    "match_type",
    "mode",
    "temporal_scope",
    "timezone",
    "template_key",
    "compass_enabled",
    "allow_cross_topic_peek",
    # Scores
    "rrf_score",
    "keyword_score",
    "keyword_rank",
    "semantic_rank",
    "semantic_degraded",
    "classification_confidence",
    # Structural metadata
    "source_type",
    "source_message_ids",
    "_reflection_source_message_ids",
    "_reflection_evidence",
    "evidence_metadata",
    "classification_metadata",
    "payload_fields",
    "fields_unsupported",
    # Fallback
    "error",
    "extra",
)

# Fields that are ALWAYS redacted because they carry sensitive payload text.
_SENSITIVE_FIELD_PATTERNS: tuple[str, ...] = (
    "plaintext_searchable",
    "canonical_text",
    "source_text",
    "payload",
    "summary",
    "summary_encrypted",
    "correction_note",
    "query",  # search query — user input that should not be logged raw
    "text",
    "correction_payload",
    "content",
    "body",
    "transcript",
    "decrypted_body",
    "raw_message",
    "searchable_content",
)


# ── Public API ───────────────────────────────────────────────────────────────


def redact_reflection_diagnostics(
    data: dict[str, Any] | None,
    *,
    safe_extra: set[str] | None = None,
    sensitive_extra: set[str] | None = None,
) -> dict[str, Any]:
    """Return a copy of *data* with sensitive payload fields redacted.

    Preserves fields matching ``_SAFE_FIELD_PATTERNS`` verbatim.  Replaces
    fields matching ``_SENSITIVE_FIELD_PATTERNS`` with ``REDACTED``.  Fields
    that match neither pattern are passed through unchanged (default-allow
    for new metadata keys added in future revisions).

    Args:
        data: The diagnostic dict to redact.  ``None`` returns an empty dict.
        safe_extra: Additional field names to treat as safe.
        sensitive_extra: Additional field names to treat as sensitive/redact.

    Returns:
        A new dict with sensitive values replaced by ``REDACTED``.
    """
    if data is None:
        return {}

    safe = set(_SAFE_FIELD_PATTERNS)
    if safe_extra:
        safe |= safe_extra

    sensitive = set(_SENSITIVE_FIELD_PATTERNS)
    if sensitive_extra:
        sensitive |= sensitive_extra

    result: dict[str, Any] = {}
    for key, value in data.items():
        key_lower = key.lower()
        if key_lower in sensitive or _is_sensitive_value(value, key_lower, sensitive):
            result[key] = REDACTED
        elif key_lower in safe:
            result[key] = _redact_nested_safe(value, safe, sensitive)
        else:
            # Default-allow: pass through but still redact nested containers
            result[key] = _redact_nested_safe(value, safe, sensitive)
    return result


def redact_exception_message(exc: BaseException) -> str:
    """Return a safe string representation of an exception.

    Strips the exception message down to its type name only, unless the
    exception is a known safe domain exception (e.g., ``SessionNotFoundError``)
    that only carries opaque identifiers.

    Args:
        exc: The exception to redact.

    Returns:
        A safe string suitable for logging or operator output.
    """
    exc_type = type(exc).__name__
    msg = str(exc)

    # Known-safe domain exceptions that only carry opaque IDs in their messages.
    # These come from app/services/reflections.py and only interpolate UUIDs
    # and type names — no payload text.
    _SAFE_EXCEPTION_TYPES: frozenset[str] = frozenset(
        {
            "SessionNotFoundError",
            "SessionNotCollectingError",
            "SessionNotFinalizingError",
            "SessionClaimConflictError",
            "SessionFinalizeConflictError",
            "EntryNotFoundError",
            "EntryRevisionConflictError",
            "EntryCorrectionError",
            "DerivationNotFoundError",
            "DerivationIdempotencyConflictError",
            "DerivationDecisionError",
        }
    )

    if exc_type in _SAFE_EXCEPTION_TYPES:
        return f"{exc_type}: {msg}"

    # For unknown exceptions, only expose the type name.
    # The full traceback is available via logging's exc_info mechanism
    # but the message itself may contain interpolated user content.
    return f"{exc_type} (message redacted)"


def redact_for_log_extra(
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Redact the ``extra`` dict passed to ``logger.{level}(..., extra={...})``.

    This is a convenience wrapper around :func:`redact_reflection_diagnostics`
    that also strips any key whose value looks like it could contain raw text.

    Args:
        extra: The ``extra`` keyword-argument dict from a logging call.

    Returns:
        A safe copy suitable for use as log ``extra``.
    """
    return redact_reflection_diagnostics(extra)


# ── Internal helpers ─────────────────────────────────────────────────────────


def _is_sensitive_value(
    value: Any,
    key_lower: str,
    sensitive: set[str],
) -> bool:
    """Heuristic: detect sensitive payload text in values of unknown keys."""
    if not isinstance(value, str):
        return False
    # Long free-text strings in unknown keys are likely payload.
    # This catches cases where a future field name doesn't match our
    # patterns but carries sensitive content anyway.
    if len(value) > 200:
        # But don't redact error messages from known-safe domains
        if key_lower in ("error", "error_message", "message"):
            return False
        return True
    return False


def _redact_nested_safe(
    value: Any,
    safe: set[str],
    sensitive: set[str],
) -> Any:
    """Recursively redact nested dicts and lists."""
    if isinstance(value, dict):
        return redact_reflection_diagnostics(
            value,
            safe_extra=safe,
            sensitive_extra=sensitive,
        )
    if isinstance(value, list):
        return [
            _redact_nested_safe(item, safe, sensitive) if isinstance(item, (dict, list)) else item
            for item in value
        ]
    if isinstance(value, (str, int, float, bool, UUID, datetime, type(None))):
        return value
    # Unknown type — convert to string representation (safe fallback)
    return repr(value)
