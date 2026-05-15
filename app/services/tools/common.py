"""Shared helpers for tool implementations."""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from tool_schemas import (
    DateRange,
    DistillationRow,
    MemoryRow,
    MessageHit,
    OOBRow,
    ObservationRow,
    ThemeSummary,
    WatchItemRow,
)
from app.services.time_context import temporal_reference
from app.services.turn_context import TurnContext


# ---------------------------------------------------------------------------
# UUID / placeholder validation helpers
# ---------------------------------------------------------------------------

PLACEHOLDER_IDS: set[str] = frozenset({
    "pending", "unknown", "todo", "new", "none", "null", "n/a",
})

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _is_placeholder(value: str) -> bool:
    """Return True when *value* is a known placeholder or whitespace-only string."""
    stripped = value.strip()
    if not stripped:
        return True
    return stripped.lower() in PLACEHOLDER_IDS


def parse_required_uuid_field(
    value: str | None,
    *,
    field_name: str = "id",
    tool_name: str = "unknown_tool",
) -> UUID:
    """Validate *value* is a non-empty UUID string; raise ToolCallRejected otherwise.

    Rejects None, whitespace-only strings, known placeholders (pending, unknown, …),
    and strings that are not a well-formed UUID of exactly 36 characters.

    Returns a ``uuid.UUID`` instance on success.
    """
    # Deferred import to avoid circular dependency at module scope.
    from app.services.tools.write_tools import ToolCallRejected  # noqa: PLC0415

    if value is None:
        raise ToolCallRejected(
            tool_validation_error(
                tool_name=tool_name,
                field=field_name,
                reason=f"{field_name} is required but was missing (None).",
                correction_hint=(
                    f"Provide a valid {field_name} obtained from a previous tool result "
                    f"(e.g. list_commitments or create_commitment)."
                ),
            )
        )
    if not isinstance(value, str) or _is_placeholder(value):
        raise ToolCallRejected(
            tool_validation_error(
                tool_name=tool_name,
                field=field_name,
                reason=f"{field_name} is a placeholder or empty string, not a valid UUID.",
                correction_hint=(
                    "Never invent IDs. "
                    "Call list_commitments to find existing commitments; "
                    "if none match, call create_commitment and use the returned commitment_id."
                ),
            )
        )
    if len(value) != 36 or not UUID_RE.match(value):
        raise ToolCallRejected(
            tool_validation_error(
                tool_name=tool_name,
                field=field_name,
                reason=f"{field_name} is not a well-formed UUID (got {len(value)} chars).",
                correction_hint=(
                    "Provide a valid UUID string obtained from a previous tool result "
                    "(e.g. list_commitments, create_commitment)."
                ),
            )
        )
    return UUID(value)


def parse_optional_uuid_field(
    value: str | None,
    *,
    field_name: str = "id",
    tool_name: str = "unknown_tool",
) -> UUID | None:
    """Validate *value* when non-None; return a ``uuid.UUID`` or None.

    Follows the same rejection rules as :func:`parse_required_uuid_field` for
    non-None inputs.  Accepts ``None`` silently (measurement-only code paths
    for ``log_event``, etc.).
    """
    if value is None:
        return None
    return parse_required_uuid_field(
        value, field_name=field_name, tool_name=tool_name
    )


def tool_validation_error(
    *,
    tool_name: str,
    field: str,
    reason: str,
    correction_hint: str,
    retryable: bool = True,
    failure_class: str = "tool_validation_recoverable",
) -> dict[str, Any]:
    """Return a structured validation-error dict for ``ToolCallRejected.result``.

    Required caller keys
    --------------------
    * error      — machine-readable error code                      (\"invalid_uuid\")
    * is_error   — always True
    * error_code — same as error, human key for registry enrichment
    * field      — argument name that failed validation
    * reason     — short human-readable explanation
    * correction_hint — actionable guidance for the model
    * retryable  — whether the agent can retry inside the same turn
    * failure_class — durable-queue classification label
    """
    return {
        "error": f"{field}_validation_failed",
        "is_error": True,
        "error_code": "invalid_uuid",
        "field": field,
        "tool_name": tool_name,
        "reason": reason,
        "correction_hint": correction_hint,
        "retryable": retryable,
        "failure_class": failure_class,
    }


def value(row: Any, key: str, default: Any = None) -> Any:
    try:
        item = row[key]
    except (KeyError, TypeError, IndexError):
        return default
    return default if item is None else item


def list_value(row: Any, key: str) -> list[Any]:
    return list(value(row, key, []))


def add_date_range(
    clauses: list[str], params: list[Any], column: str, date_range: DateRange | None
) -> None:
    if date_range is None:
        return
    if date_range.start is not None:
        params.append(date_range.start)
        clauses.append(f"{column} >= ${len(params)}")
    if date_range.end is not None:
        params.append(date_range.end)
        clauses.append(f"{column} <= ${len(params)}")


def media_analysis_text(row_or_analysis: Any) -> str:
    analysis = value(row_or_analysis, "media_analysis", row_or_analysis)
    if not isinstance(analysis, dict):
        return ""
    text = (
        analysis.get("explanation")
        or analysis.get("description")
        or analysis.get("summary")
    )
    if not text:
        return ""
    media_type = analysis.get("kind") or value(row_or_analysis, "media_type", "media")
    return f"[{media_type}] {text}"


def current_scheduled_task(ctx: TurnContext) -> dict[str, Any] | None:
    metadata = ctx.trigger_metadata or {}
    if metadata.get("kind") != "scheduled_task":
        return None
    context = metadata.get("context")
    if not isinstance(context, dict):
        return None
    job_id = context.get("job_id")
    task_id = context.get("task_id")
    if not job_id or not task_id:
        return None
    return {
        "job_id": job_id,
        "task_id": task_id,
        "brief": context.get("brief"),
        "recurrence": context.get("recurrence"),
    }


def _time(value_: Any, timezone: str | None, now: Any = None) -> dict[str, str] | None:
    return temporal_reference(value_, timezone, now=now)


def message_hit(
    row: Any, *, timezone: str | None = None, now: Any = None
) -> MessageHit:
    content = value(row, "content", "") or media_analysis_text(row)
    return MessageHit(
        id=row["id"],
        sender_id=row["sender_id"],
        sent_at=row["sent_at"],
        sent_at_time=_time(row["sent_at"], timezone, now),
        content=content,
        charge=value(row, "charge", "routine"),
        direction=row["direction"],
    )


def theme_summary(
    row: Any, *, timezone: str | None = None, now: Any = None
) -> ThemeSummary:
    return ThemeSummary(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        sentiment=row["sentiment"],
        health=row["health"],
        last_reinforced_at=row["last_reinforced_at"],
        last_active_at=row["last_active_at"],
        last_reinforced_at_time=_time(row["last_reinforced_at"], timezone, now),
        last_active_at_time=_time(row["last_active_at"], timezone, now),
    )


def memory_row(row: Any, *, timezone: str | None = None, now: Any = None) -> MemoryRow:
    return MemoryRow(
        id=row["id"],
        about_user_id=row["about_user_id"],
        content=row["content"],
        status=row["status"],
        visibility=value(row, "visibility", "private"),
        shareable_summary=value(row, "shareable_summary"),
        related_theme_ids=list_value(row, "related_theme_ids"),
        created_at=row["created_at"],
        last_referenced_at=row["last_referenced_at"],
        created_at_time=_time(row["created_at"], timezone, now),
        last_referenced_at_time=_time(row["last_referenced_at"], timezone, now),
    )


def watch_item_row(
    row: Any, *, timezone: str | None = None, now: Any = None
) -> WatchItemRow:
    return WatchItemRow(
        id=row["id"],
        owner_user_id=row["owner_user_id"],
        content=row["content"],
        due_at=row["due_at"],
        status=row["status"],
        addressing_note=row["addressing_note"],
        created_at=row["created_at"],
        addressed_at=row["addressed_at"],
        related_theme_ids=list_value(row, "related_theme_ids"),
        due_at_time=_time(row["due_at"], timezone, now),
        created_at_time=_time(row["created_at"], timezone, now),
        addressed_at_time=_time(row["addressed_at"], timezone, now),
    )


def observation_row(
    row: Any, *, timezone: str | None = None, now: Any = None
) -> ObservationRow:
    return ObservationRow(
        id=row["id"],
        content=row["content"],
        about_user_id=row["about_user_id"],
        confidence=row["confidence"],
        significance=row["significance"],
        status=row["status"],
        related_theme_ids=list_value(row, "related_theme_ids"),
        supporting_message_ids=list_value(row, "supporting_message_ids"),
        created_at=row["created_at"],
        last_reinforced_at=row["last_reinforced_at"],
        surfaced_count=value(row, "surfaced_count", 0),
        created_at_time=_time(row["created_at"], timezone, now),
        last_reinforced_at_time=_time(row["last_reinforced_at"], timezone, now),
    )


def distillation_row(
    row: Any, *, timezone: str | None = None, now: Any = None
) -> DistillationRow:
    return DistillationRow(
        id=row["id"],
        content=row["content"],
        confidence=row["confidence"],
        status=row["status"],
        sensitivity=row["sensitivity"],
        visibility=row["visibility"],
        shareable_summary=value(row, "shareable_summary"),
        source_user_ids=list_value(row, "source_user_ids"),
        related_memory_ids=list_value(row, "related_memory_ids"),
        related_observation_ids=list_value(row, "related_observation_ids"),
        related_theme_ids=list_value(row, "related_theme_ids"),
        supporting_message_ids=list_value(row, "supporting_message_ids"),
        created_from_tool_call_id=value(row, "created_from_tool_call_id"),
        triggering_message_id=value(row, "triggering_message_id"),
        supersedes_distillation_id=value(row, "supersedes_distillation_id"),
        superseded_by_distillation_id=value(row, "superseded_by_distillation_id"),
        revision_note=value(row, "revision_note"),
        revision_count=value(row, "revision_count", 0),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        revised_at=value(row, "revised_at"),
        retired_at=value(row, "retired_at"),
        created_at_time=_time(row["created_at"], timezone, now),
        updated_at_time=_time(row["updated_at"], timezone, now),
        revised_at_time=_time(value(row, "revised_at"), timezone, now),
        retired_at_time=_time(value(row, "retired_at"), timezone, now),
    )


def oob_row(row: Any, *, timezone: str | None = None, now: Any = None) -> OOBRow:
    shareable_context = row["shareable_context"]
    return OOBRow(
        id=row["id"],
        owner_id=row["owner_id"],
        protected_summary=shareable_context or "[protected]",
        shareable_context=shareable_context,
        severity=row["severity"],
        status=row["status"],
        created_at=row["created_at"],
        review_at=row["review_at"],
        created_at_time=_time(row["created_at"], timezone, now),
        review_at_time=_time(row["review_at"], timezone, now),
    )
