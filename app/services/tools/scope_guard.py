"""Scope authorization helpers for read/write tool gates (§6, §8).

Semantics (locked):
* None-permissive: when ctx.read_scopes / ctx.write_scopes is None, the gate
  returns None (allow). This preserves the ~15 legacy fixtures that never set
  scopes.
* 'own' resolves to ctx.primary_topic_slug at check time.
* Allow iff ('own' in topics AND target_slug == primary_topic_slug)
  OR target_slug in topics OR 'all' in topics. Else deny.
"""

from __future__ import annotations

from typing import Any

# Artifact-reading tools that take a `scope` parameter (10 tools).
ARTIFACT_READ_TOOLS: frozenset[str] = frozenset({
    "get_memories",
    "get_observations",
    "get_distillations",
    "get_oob",
    "summarize_oob_topics",
    "list_themes",
    "get_theme",
    "list_watch_items",
    "list_bridge_candidates",
    "get_self_model",
})

# Artifact-writing tools that consult ctx.write_scopes (21 tools).
# NB: transport (send/edit/delete/react), scheduling, log_feedback, escalate,
# and read-shaped tools are intentionally NOT in this set.
ARTIFACT_WRITE_TOOLS: frozenset[str] = frozenset({
    "update_user_style_notes",
    "update_cross_thread_sharing_default",
    "create_bridge_candidate",
    "update_bridge_candidate",
    "send_bridge_candidate",
    "add_memory",
    "update_memory",
    "supersede_memory",
    "create_theme",
    "update_theme",
    "add_watch_item",
    "update_watch_item",
    "address_watch_item",
    "log_observation",
    "update_observation",
    "add_distillation",
    "update_distillation",
    "revise_distillation",
    "add_oob",
    "update_oob",
    "lift_oob",
})


def _resolve_target(requested_scope: str, primary_topic_slug: str | None) -> str | None:
    """Resolve 'own' to ctx.primary_topic_slug; pass through anything else."""
    if requested_scope == "own":
        return primary_topic_slug
    return requested_scope


def check_read_scope(ctx: Any, requested_scope: str = "own") -> str | None:
    """Return None when the read is allowed; an error string when denied.

    None-permissive when ctx.read_scopes is None.
    """
    scopes = getattr(ctx, "read_scopes", None)
    if scopes is None:
        return None
    topics = scopes.topics
    if "all" in topics:
        return None
    target = _resolve_target(requested_scope, getattr(ctx, "primary_topic_slug", None))
    if target is None:
        return f"scope_denied: read scope='{requested_scope}' unresolved (no primary_topic_slug)"
    if "own" in topics and target == getattr(ctx, "primary_topic_slug", None):
        return None
    if target in topics:
        return None
    return (
        f"scope_denied: read scope='{requested_scope}' not in {sorted(topics)} "
        f"(primary={getattr(ctx, 'primary_topic_slug', None)})"
    )


def check_write_scope(ctx: Any) -> str | None:
    """Return None when the write is allowed; an error string when denied.

    The write-scope gate currently authorizes against the bot's primary topic
    (ctx.primary_topic_slug). Multi-topic writes are S6.

    None-permissive when ctx.write_scopes is None.
    """
    scopes = getattr(ctx, "write_scopes", None)
    if scopes is None:
        return None
    topics = scopes.topics
    if "all" in topics:
        return None
    primary = getattr(ctx, "primary_topic_slug", None)
    if "own" in topics and primary is not None:
        return None
    if primary is not None and primary in topics:
        return None
    return (
        f"write_scope_denied: primary='{primary}' not in {sorted(topics)}"
    )


# ── Multi-topic write helpers (S6) ──────────────────────────────────────────


class ToolCallRejected(ValueError):
    """Raised when a tool call fails scope/reason checks."""


def resolve_write_topic_slugs(ctx: Any, requested: list[str] | None) -> list[str]:
    """Resolve requested topic_slugs list for a write call.

    * None → [ctx.primary_topic_slug] (default single-topic path).
    * 'own' sentinel → ctx.primary_topic_slug.
    * Deduplicates AFTER resolving 'own' (so ['own','career'] with
      primary='career' → ['career']).
    * None-permissive: when write_scopes is None, all slugs pass and
      primary_topic_slug is not required (matches check_write_scope semantics).
    * Otherwise, every slug must be in write_scopes.topics OR resolved via
      'own' (slug == primary_topic_slug), raising ToolCallRejected on any
      out-of-scope slug (NO silent drop).
    """
    primary = getattr(ctx, "primary_topic_slug", None)

    # None-permissive: legacy fixtures without scopes
    scopes = getattr(ctx, "write_scopes", None)
    if scopes is None:
        if requested is None:
            if primary is not None:
                return [primary]
            return []
        resolved: list[str] = []
        for slug in requested:
            resolved.append(primary if slug == "own" else slug)
        seen: set[str] = set()
        unique: list[str] = []
        for slug in resolved:
            if slug not in seen and slug is not None:
                seen.add(slug)
                unique.append(slug)
        return unique

    if primary is None:
        raise ToolCallRejected("resolve_write_topic_slugs: ctx has no primary_topic_slug")

    if requested is None:
        return [primary]

    # Resolve 'own' sentinel
    resolved: list[str] = []
    for slug in requested:
        if slug == "own":
            resolved.append(primary)
        else:
            resolved.append(slug)

    # Deduplicate after resolution
    seen: set[str] = set()
    unique: list[str] = []
    for slug in resolved:
        if slug not in seen:
            seen.add(slug)
            unique.append(slug)

    # Authorization check
    scopes = getattr(ctx, "write_scopes", None)
    if scopes is None:
        return unique
    topics = scopes.topics
    if "all" in topics:
        return unique

    for slug in unique:
        if slug in topics:
            continue
        if "own" in topics and slug == primary:
            continue
        raise ToolCallRejected(
            f"write_scope_denied: topic_slug='{slug}' not in write_scopes.topics={sorted(topics)} "
            f"(primary={primary})"
        )

    return unique


def require_reason_for_cross_topic(
    slugs: list[str],
    primary: str,
    reason: str | None,
) -> None:
    """Raise ToolCallRejected if this is a cross-topic write without a non-empty reason.

    Cross-topic is defined as: (len(set(slugs)) > 1) OR slugs[0] != primary.
    A reason that is None or whitespace-only is rejected.
    """
    if len(slugs) == 0:
        return  # degenerate, should not happen
    is_cross_topic = (len(set(slugs)) > 1) or (slugs[0] != primary)
    if not is_cross_topic:
        return
    if reason is None or not reason.strip():
        raise ToolCallRejected(
            "cross_topic_write requires a non-empty reason; "
            f"slugs={slugs}, primary={primary}, reason={reason!r}"
        )


async def resolve_topic_ids(
    pool: Any,
    slugs: list[str],
) -> dict[str, Any]:
    """Resolve topic slugs to {slug: id} dict, raising for any missing slug.

    Returns a dict mapping each slug to the topic row dict (with 'id' and 'slug' keys).
    """
    rows = await pool.fetch(
        "SELECT id, slug FROM mediator.topics WHERE slug = ANY($1::text[])",
        slugs,
    )
    found: dict[str, Any] = {row["slug"]: row["id"] for row in rows}
    missing = [s for s in slugs if s not in found]
    if missing:
        raise ToolCallRejected(
            f"resolve_topic_ids: unknown topic slugs: {missing}"
        )
    return found