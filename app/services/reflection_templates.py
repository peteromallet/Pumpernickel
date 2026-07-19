"""Reflection template registry and payload validators.

Keyed by stable template identifiers and versions, this module provides
domain-specific validation of reflection entry payloads and derivation
decision payloads. It is designed to be consumed by the reflection storage
APIs and does NOT generalize to arbitrary longitudinal-state abstractions.

Design constraints (see plan_v1 / SD-002):
  * Templates are registered in code; adding a template requires code +
    tests but NOT a schema migration.
  * ``template_key`` + version uniquely identify a template registration.
  * The shared payload envelope (summary, facts, events, decisions,
    priorities, wins, blockers, open_loops, questions, signals,
    template_data) is normalized before storage.
  * Derivation payloads carry derivation_kind, assertion_source, and
    decision; the validators enforce the enumeration contracts defined
    by the migration CHECK constraints.

Unknown templates and incompatible versions are rejected at validation
time with clear, actionable errors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ── Public exception surface ──────────────────────────────────────────────


class UnknownTemplateError(ValueError):
    """Raised when a template_key is not registered."""


class IncompatibleTemplateVersionError(ValueError):
    """Raised when a template version is not compatible with the payload."""


class TemplateValidationError(ValueError):
    """Raised when a payload does not satisfy its template's constraints."""


# ── Enumerations (mirrors migration CHECK constraints) ────────────────────

VALID_TEMPORAL_SCOPES: frozenset[str] = frozenset(
    {"instant", "day", "week", "month", "custom", "none"}
)

VALID_PHASES: frozenset[str] = frozenset(
    {"opening", "closing", "checkpoint", "prospective", "retrospective", "freeform"}
)

VALID_DERIVATION_KINDS: frozenset[str] = frozenset(
    {"memory", "observation", "distillation", "orientation"}
)

VALID_ASSERTION_SOURCES: frozenset[str] = frozenset(
    {"user_explicit", "user_implied", "agent_inferred"}
)

VALID_DECISIONS: frozenset[str] = frozenset(
    {"applied", "reinforced", "deferred", "rejected", "superseded"}
)

VALID_SESSION_STATUSES: frozenset[str] = frozenset(
    {"collecting", "finalizing", "processed", "abandoned", "processing_failed"}
)

# ── Shared payload envelope keys ──────────────────────────────────────────

# All templates share this envelope.  Every field is optional (may be absent
# or empty) to avoid forcing the processor to invent content.
_SHARED_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
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
)

# Envelope keys expected to be list-typed when present.
_LIST_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"facts", "events", "decisions", "priorities", "wins", "blockers", "open_loops", "questions"}
)

# Envelope keys expected to be dict-typed when present.
_DICT_ENVELOPE_KEYS: frozenset[str] = frozenset({"signals", "template_data"})


# ── Template descriptor ───────────────────────────────────────────────────


@dataclass(frozen=True)
class ReflectionTemplate:
    """A registered reflection template.

    Each template defines the constraints a reflection entry payload must
    satisfy when it carries this ``template_key`` + ``version`` pair.
    """

    key: str
    version: int = 1
    allowed_temporal_scopes: frozenset[str] = field(default_factory=lambda: VALID_TEMPORAL_SCOPES)
    allowed_phases: frozenset[str] = field(default_factory=lambda: VALID_PHASES)
    validate_payload: Callable[[dict[str, Any]], None] | None = None
    normalize_payload: Callable[[dict[str, Any]], dict[str, Any]] | None = None


# ── Registry ──────────────────────────────────────────────────────────────

# Private map: (template_key, version) -> ReflectionTemplate
_registry: dict[tuple[str, int], ReflectionTemplate] = {}

# Latest version per key for convenience.
_latest: dict[str, int] = {}


def register_template(template: ReflectionTemplate) -> None:
    """Register a new template.

    Raises ``ValueError`` if the same (key, version) pair is already
    registered with a different descriptor (duplicate identical entries
    are silently ignored).
    """
    pk = (template.key, template.version)
    existing = _registry.get(pk)
    if existing is not None:
        if existing == template:
            return  # Idempotent re-registration.
        raise ValueError(
            f"Template ({template.key!r}, v{template.version}) is already registered "
            f"with a different descriptor."
        )
    _registry[pk] = template
    _latest[template.key] = max(_latest.get(template.key, 0), template.version)


def get_template(key: str, version: int | None = None) -> ReflectionTemplate:
    """Look up a registered template by key and optional version.

    If *version* is ``None``, the latest registered version is returned.
    Raises ``UnknownTemplateError`` if the key is unknown, or
    ``IncompatibleTemplateVersionError`` if the version is not registered.
    """
    if key not in _latest:
        raise UnknownTemplateError(
            f"Unknown reflection template {key!r}. "
            f"Registered templates: {sorted(_latest.keys())}"
        )
    if version is None:
        version = _latest[key]
    pk = (key, version)
    template = _registry.get(pk)
    if template is None:
        raise IncompatibleTemplateVersionError(
            f"Template {key!r} version {version} is not registered. "
            f"Latest version: {_latest[key]}. "
            f"Available versions for this key: "
            f"{sorted(v for (k, v) in _registry if k == key)}"
        )
    return template


def list_template_keys() -> list[str]:
    """Return sorted list of all registered template keys."""
    return sorted(_latest.keys())


def template_is_registered(key: str) -> bool:
    """Return True if *key* has at least one registered version."""
    return key in _latest


# ── Payload validation / normalization ────────────────────────────────────


def _validate_envelope_shape(payload: dict[str, Any]) -> None:
    """Validate the shared envelope structure.

    - Unknown keys are rejected.
    - List-typed keys must be lists (or absent/None).
    - Dict-typed keys must be dicts (or absent/None).
    - ``summary`` must be a string (or absent/None).
    """
    if not isinstance(payload, dict):
        raise TemplateValidationError(
            f"Payload must be a dict, got {type(payload).__name__}"
        )

    for key, value in payload.items():
        if key not in _SHARED_ENVELOPE_KEYS:
            raise TemplateValidationError(
                f"Unknown payload key {key!r}. "
                f"Allowed keys: {sorted(_SHARED_ENVELOPE_KEYS)}"
            )

    for key in _LIST_ENVELOPE_KEYS:
        value = payload.get(key)
        if value is not None and not isinstance(value, list):
            raise TemplateValidationError(
                f"Payload key {key!r} must be a list when present, "
                f"got {type(value).__name__}"
            )

    for key in _DICT_ENVELOPE_KEYS:
        value = payload.get(key)
        if value is not None and not isinstance(value, dict):
            raise TemplateValidationError(
                f"Payload key {key!r} must be a dict when present, "
                f"got {type(value).__name__}"
            )

    summary = payload.get("summary")
    if summary is not None and not isinstance(summary, str):
        raise TemplateValidationError(
            f"Payload key 'summary' must be a string when present, "
            f"got {type(summary).__name__}"
        )


def _normalize_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the shared envelope to a canonical shape.

    Returns a new dict with all envelope keys present, filling absent keys
    with their zero values (None / [] / {}).  Unknown keys are dropped.
    """
    normalized: dict[str, Any] = {}
    for key in sorted(_SHARED_ENVELOPE_KEYS):
        value = payload.get(key)
        if value is None:
            if key in _LIST_ENVELOPE_KEYS:
                normalized[key] = []
            elif key in _DICT_ENVELOPE_KEYS:
                normalized[key] = {}
            else:
                normalized[key] = None  # summary
        else:
            normalized[key] = value
    return normalized


def validate_entry_payload(
    template_key: str,
    payload: dict[str, Any],
    *,
    version: int | None = None,
) -> dict[str, Any]:
    """Validate and normalize a reflection entry payload.

    1. Look up the template (raises UnknownTemplateError /
       IncompatibleTemplateVersionError).
    2. Validate the shared envelope shape.
    3. Run the template-specific validator (if any).
    4. Normalize the envelope.
    5. Run the template-specific normalizer (if any).

    Returns the fully normalized payload dict.

    Raises ``TemplateValidationError``, ``UnknownTemplateError``, or
    ``IncompatibleTemplateVersionError`` on any failure.
    """
    template = get_template(template_key, version=version)

    _validate_envelope_shape(payload)

    if template.validate_payload is not None:
        template.validate_payload(payload)

    normalized = _normalize_envelope(payload)

    if template.normalize_payload is not None:
        normalized = template.normalize_payload(normalized)

    return normalized


def validate_derivation_payload(
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Validate and normalize a derivation decision payload.

    Validates that:
    - ``derivation_kind`` is a recognized enumeration value.
    - ``assertion_source`` is a recognized enumeration value.
    - ``decision`` is a recognized enumeration value.
    - ``confidence``, if present, is a float in [0, 1].
    - ``supporting_message_ids``, if present, is a list of strings.

    Returns the (minimally normalized) payload.
    Raises ``TemplateValidationError`` on any failure.
    """
    if not isinstance(payload, dict):
        raise TemplateValidationError(
            f"Derivation payload must be a dict, got {type(payload).__name__}"
        )

    # --- derivation_kind ---
    kind = payload.get("derivation_kind")
    if not kind or not isinstance(kind, str):
        raise TemplateValidationError(
            "Derivation payload requires a non-empty string 'derivation_kind'"
        )
    if kind not in VALID_DERIVATION_KINDS:
        raise TemplateValidationError(
            f"Invalid derivation_kind {kind!r}; "
            f"expected one of {sorted(VALID_DERIVATION_KINDS)}"
        )

    # --- assertion_source ---
    source = payload.get("assertion_source")
    if not source or not isinstance(source, str):
        raise TemplateValidationError(
            "Derivation payload requires a non-empty string 'assertion_source'"
        )
    if source not in VALID_ASSERTION_SOURCES:
        raise TemplateValidationError(
            f"Invalid assertion_source {source!r}; "
            f"expected one of {sorted(VALID_ASSERTION_SOURCES)}"
        )

    # --- decision ---
    decision = payload.get("decision", "deferred")
    if not isinstance(decision, str):
        raise TemplateValidationError(
            f"'decision' must be a string, got {type(decision).__name__}"
        )
    if decision not in VALID_DECISIONS:
        raise TemplateValidationError(
            f"Invalid decision {decision!r}; "
            f"expected one of {sorted(VALID_DECISIONS)}"
        )

    # --- confidence (optional) ---
    confidence = payload.get("confidence")
    if confidence is not None:
        if not isinstance(confidence, (int, float)):
            raise TemplateValidationError(
                f"'confidence' must be a number when present, "
                f"got {type(confidence).__name__}"
            )
        if confidence < 0 or confidence > 1:
            raise TemplateValidationError(
                f"'confidence' must be in [0, 1], got {confidence}"
            )

    # --- supporting_message_ids (optional) ---
    msg_ids = payload.get("supporting_message_ids")
    if msg_ids is not None and not isinstance(msg_ids, list):
        raise TemplateValidationError(
            f"'supporting_message_ids' must be a list when present, "
            f"got {type(msg_ids).__name__}"
        )

    # --- eligibility_reasons (optional) ---
    reasons = payload.get("eligibility_reasons")
    if reasons is not None and not isinstance(reasons, list):
        raise TemplateValidationError(
            f"'eligibility_reasons' must be a list when present, "
            f"got {type(reasons).__name__}"
        )

    # Return normalized (ensuring defaults for optional fields).
    normalized = dict(payload)
    if "decision" not in normalized:
        normalized["decision"] = "deferred"
    if "confidence" not in normalized:
        normalized["confidence"] = None
    if "supporting_message_ids" not in normalized:
        normalized["supporting_message_ids"] = []
    if "eligibility_reasons" not in normalized:
        normalized["eligibility_reasons"] = []

    return normalized


# ── Built-in templates ────────────────────────────────────────────────────
#
# Registered at import time so consumers get a ready-to-use registry.
# Future templates (decision_debrief, custom workflows, etc.) are added the
# same way — a new ReflectionTemplate + register_template() call.


def _validate_template_data_is_dict(payload: dict[str, Any]) -> None:
    td = payload.get("template_data")
    if td is not None and not isinstance(td, dict):
        raise TemplateValidationError(
            f"'template_data' must be a dict when present, "
            f"got {type(td).__name__}"
        )


# -- freeform ---------------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="freeform",
        version=1,
        allowed_temporal_scopes=frozenset({"instant", "day", "week", "month", "custom", "none"}),
        allowed_phases=frozenset({"opening", "closing", "checkpoint", "prospective", "retrospective", "freeform"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- daily_open -------------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="daily_open",
        version=1,
        allowed_temporal_scopes=frozenset({"day"}),
        allowed_phases=frozenset({"opening", "prospective"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- daily_close ------------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="daily_close",
        version=1,
        allowed_temporal_scopes=frozenset({"day"}),
        allowed_phases=frozenset({"closing", "retrospective"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- weekly_open ------------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="weekly_open",
        version=1,
        allowed_temporal_scopes=frozenset({"week"}),
        allowed_phases=frozenset({"opening", "prospective"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- weekly_close -----------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="weekly_close",
        version=1,
        allowed_temporal_scopes=frozenset({"week"}),
        allowed_phases=frozenset({"closing", "retrospective"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- monthly_open -----------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="monthly_open",
        version=1,
        allowed_temporal_scopes=frozenset({"month"}),
        allowed_phases=frozenset({"opening", "prospective"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- monthly_close ----------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="monthly_close",
        version=1,
        allowed_temporal_scopes=frozenset({"month"}),
        allowed_phases=frozenset({"closing", "retrospective"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- decision_debrief -------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="decision_debrief",
        version=1,
        allowed_temporal_scopes=frozenset({"instant", "day", "custom", "none"}),
        allowed_phases=frozenset({"closing", "retrospective", "freeform"}),
        validate_payload=_validate_template_data_is_dict,
    )
)

# -- checkpoint -------------------------------------------------------------

register_template(
    ReflectionTemplate(
        key="checkpoint",
        version=1,
        allowed_temporal_scopes=frozenset({"instant", "day", "week", "month", "custom", "none"}),
        allowed_phases=frozenset({"checkpoint", "prospective", "retrospective"}),
        validate_payload=_validate_template_data_is_dict,
    )
)
