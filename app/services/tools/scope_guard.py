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
