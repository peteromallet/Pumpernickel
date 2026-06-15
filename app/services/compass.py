"""Compass read model and snapshot builder.

Compass is the product/service read layer for user orientation state. It
provides a deterministic snapshot of a user's reviewed orientation items
(principles, goals, priorities, anti-patterns) scoped to explicit topics.

This module owns Compass-specific snapshot/render logic while storage
validation remains in ``UserOrientationStore``. No durable ``compass_*``
tables exist — Compass is purely a read/render layer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as dt_date, datetime
from typing import Any, Callable
from uuid import UUID

from app.services import user_orientation as uo

logger = logging.getLogger(__name__)


# ── Read models ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CompassItem:
    """A single orientation item that has passed the Compass visibility check.

    Carries the orientation item data plus its evidence/progress links so
    renderers have everything they need without additional store calls.
    """

    item: uo.OrientationItem
    links: tuple[uo.OrientationLink, ...] = field(default_factory=tuple)

    @property
    def id(self) -> UUID:
        return self.item.id

    @property
    def kind(self) -> str:
        return self.item.kind

    @property
    def status(self) -> str:
        return self.item.status

    @property
    def label(self) -> str:
        return self.item.label

    @property
    def detail(self) -> str | None:
        return self.item.detail

    @property
    def priority_rank(self) -> int | None:
        return self.item.priority_rank

    @property
    def target_date(self) -> dt_date | None:
        return self.item.target_date

    @property
    def completed_at(self) -> datetime | None:
        return self.item.completed_at

    @property
    def closed_reason(self) -> str | None:
        return self.item.closed_reason

    @property
    def outcome_note(self) -> str | None:
        return self.item.outcome_note


@dataclass(frozen=True, slots=True)
class CompassSnapshot:
    """Immutable read model for a user's Compass orientation state.

    Built from explicit user_id and topic_ids only — the ``"all"`` sentinel
    is never accepted. Every item has passed ``is_compass_visible()`` before
    inclusion, so renderers can trust that unreviewed, rejected, superseded,
    and partner-owned rows are absent.

    Items are grouped by kind for straightforward rendering:
      * ``principles``
      * ``priorities`` (sorted by priority_rank, then created_at)
      * ``anti_patterns``
      * ``active_goals`` (status == 'active')
      * ``completed_goals`` (status in {'completed', 'retired'})

    Each ``CompassItem`` includes its evidence/progress links so renderers
    do not need to re-query the store.
    """

    user_id: UUID
    topic_ids: frozenset[UUID]
    principles: tuple[CompassItem, ...] = field(default_factory=tuple)
    priorities: tuple[CompassItem, ...] = field(default_factory=tuple)
    anti_patterns: tuple[CompassItem, ...] = field(default_factory=tuple)
    active_goals: tuple[CompassItem, ...] = field(default_factory=tuple)
    completed_goals: tuple[CompassItem, ...] = field(default_factory=tuple)

    @property
    def is_empty(self) -> bool:
        """True if no compass-visible items exist across all categories."""
        return not any(
            (
                self.principles,
                self.priorities,
                self.anti_patterns,
                self.active_goals,
                self.completed_goals,
            )
        )

    @property
    def total_items(self) -> int:
        """Total number of compass-visible items (for stats/logging)."""
        return (
            len(self.principles)
            + len(self.priorities)
            + len(self.anti_patterns)
            + len(self.active_goals)
            + len(self.completed_goals)
        )


# ── Builder ────────────────────────────────────────────────────────────────


async def build_compass_snapshot(
    store: uo.UserOrientationStore,
    *,
    user_id: UUID,
    topic_ids: frozenset[UUID],
) -> CompassSnapshot:
    """Build a CompassSnapshot from the UserOrientationStore.

    Args:
        store: An initialized UserOrientationStore.
        user_id: Explicit user whose orientation to read. Required.
        topic_ids: Explicit, non-empty set of allowed topic UUIDs. The
            ``"all"`` sentinel is never accepted. Rows without a topic_id
            (NULL) are excluded.

    Returns:
        A CompassSnapshot with all compass-visible items grouped by kind.

    Raises:
        ValueError: If user_id is None or topic_ids is empty.
    """
    if user_id is None:
        raise ValueError("build_compass_snapshot: user_id must not be None")
    if not topic_ids:
        raise ValueError(
            "build_compass_snapshot: topic_ids must be a non-empty frozenset"
        )

    topic_id_list: list[UUID] = list(topic_ids)

    # ── Fetch orientation items within the explicit topic/user scope ──
    # list_items already excludes unreviewed, rejected, and superseded by
    # default. We pass include_unreviewed=False / include_rejected=False
    # (the defaults) to match the Compass default visibility.
    all_items = await store.list_items(
        user_id=user_id,
        topic_ids=topic_id_list,
        include_unreviewed=False,
        include_rejected=False,
    )

    # ── Double-filter through is_compass_visible() ────────────────────
    # This is the canonical Compass visibility gate. Although list_items
    # already excludes some rows, is_compass_visible() applies the full
    # policy (e.g., bot_proposed without review, source-based rules).
    visible_items: list[uo.OrientationItem] = []
    for item in all_items:
        item_dict = _item_to_dict(item)
        if uo.is_compass_visible(item_dict):
            visible_items.append(item)

    # ── Fetch evidence links for every visible item ───────────────────
    items_with_links: list[CompassItem] = []
    for item in visible_items:
        links = await store.get_links(user_id=user_id, item_id=item.id)
        items_with_links.append(
            CompassItem(item=item, links=tuple(links))
        )

    # ── Group by kind ────────────────────────────────────────────────
    principles: list[CompassItem] = []
    goals_active: list[CompassItem] = []
    goals_completed: list[CompassItem] = []
    priorities: list[CompassItem] = []
    anti_patterns: list[CompassItem] = []

    for ci in items_with_links:
        kind = ci.kind
        if kind == "principle":
            principles.append(ci)
        elif kind == "goal":
            if ci.status == "active":
                goals_active.append(ci)
            elif ci.status in ("completed", "retired"):
                goals_completed.append(ci)
            # Pending, rejected, superseded should already be filtered out
            # by is_compass_visible(), but we skip them defensively.
        elif kind == "priority":
            priorities.append(ci)
        elif kind == "anti_pattern":
            anti_patterns.append(ci)

    # Sort priorities by rank (ascending, NULLs last), then by created_at.
    priorities.sort(
        key=lambda ci: (
            0 if ci.priority_rank is not None else 1,
            ci.priority_rank or 0,
            ci.item.created_at,
        )
    )

    # Sort goals by created_at for deterministic ordering.
    goals_active.sort(key=lambda ci: ci.item.created_at)
    goals_completed.sort(key=lambda ci: ci.item.created_at)

    # Sort principles and anti_patterns by created_at.
    principles.sort(key=lambda ci: ci.item.created_at)
    anti_patterns.sort(key=lambda ci: ci.item.created_at)

    return CompassSnapshot(
        user_id=user_id,
        topic_ids=frozenset(topic_ids),
        principles=tuple(principles),
        priorities=tuple(priorities),
        anti_patterns=tuple(anti_patterns),
        active_goals=tuple(goals_active),
        completed_goals=tuple(goals_completed),
    )


# ── Helpers ────────────────────────────────────────────────────────────────


def _item_to_dict(item: uo.OrientationItem) -> dict[str, Any]:
    """Convert an OrientationItem to a dict for is_compass_visible()."""
    return {
        "status": item.status,
        "review_state": item.review_state,
        "source": item.source,
    }


# ── Renderer ───────────────────────────────────────────────────────────────


class CompassRenderer:
    """Renders a CompassSnapshot as deterministic markdown.

    Produces a ``## Compass`` section with subsections for Principles,
    Priorities, Anti-patterns, Active Goals, and Completed/Retired Goals.
    Goal metadata (target_date, completed_at, closed_reason, outcome_note)
    and compact linked commitment/event evidence are rendered without
    mutating or duplicating goal lifecycle state.

    Usage::

        renderer = CompassRenderer()
        markdown = renderer.render(snapshot)
    """

    def render(self, snapshot: CompassSnapshot) -> str:
        """Render a CompassSnapshot as a markdown string.

        Returns an empty string if the snapshot is empty (no visible items).
        Otherwise returns a ``## Compass`` heading followed by populated
        subsections in deterministic order.
        """
        if snapshot.is_empty:
            return ""

        lines: list[str] = ["## Compass"]

        # Render each section in a fixed, deterministic order.
        self._render_section(
            lines, "Principles", snapshot.principles,
            _renderer=self._render_generic_item,
        )
        self._render_section(
            lines, "Priorities", snapshot.priorities,
            _renderer=self._render_priority_item,
        )
        self._render_section(
            lines, "Anti-patterns", snapshot.anti_patterns,
            _renderer=self._render_generic_item,
        )
        self._render_section(
            lines, "Active Goals", snapshot.active_goals,
            _renderer=self._render_goal_item,
        )
        self._render_section(
            lines, "Completed / Retired Goals", snapshot.completed_goals,
            _renderer=self._render_goal_item,
        )

        return "\n".join(lines)

    # ── Section rendering ──────────────────────────────────────────────

    @staticmethod
    def _render_section(
        lines: list[str],
        heading: str,
        items: tuple[CompassItem, ...],
        *,
        _renderer: Callable[[CompassItem], list[str]],
    ) -> None:
        """Append a markdown subsection if items is non-empty."""
        if not items:
            return
        lines.append("")
        lines.append(f"### {heading}")
        for item in items:
            item_lines = _renderer(item)
            lines.extend(item_lines)

    # ── Per-kind item renderers ────────────────────────────────────────

    @staticmethod
    def _render_generic_item(item: CompassItem) -> list[str]:
        """Render a principle or anti-pattern as a markdown bullet.

        Format:
            - **<label>**: <detail>
        """
        label = item.label
        detail = item.detail
        if detail:
            return [f"- **{label}**: {detail}"]
        return [f"- **{label}**"]

    @staticmethod
    def _render_priority_item(item: CompassItem) -> list[str]:
        """Render a priority as a numbered markdown item with rank.

        Format:
            1. **<label>** (priority <rank>)
        """
        label = item.label
        rank = item.priority_rank
        if rank is not None:
            return [f"1. **{label}** (priority {rank})"]
        return [f"1. **{label}**"]

    @staticmethod
    def _render_goal_item(item: CompassItem) -> list[str]:
        """Render a goal (active or completed/retired) with metadata and
        evidence links.

        Format:
            - **<label>**
              - Target: <target_date>
              - Completed: <completed_at> — <closed_reason>
              - Outcome: <outcome_note>
              - Evidence: <compact link list>
        """
        result: list[str] = [f"- **{item.label}**"]

        detail = item.detail
        if detail:
            result.append(f"  - Detail: {detail}")

        target_date = item.target_date
        if target_date is not None:
            result.append(f"  - Target: {target_date.isoformat()}")

        completed_at = item.completed_at
        if completed_at is not None:
            ts = completed_at.isoformat()
            reason = item.closed_reason
            if reason:
                result.append(f"  - Completed: {ts} — {reason}")
            else:
                result.append(f"  - Completed: {ts}")

        outcome_note = item.outcome_note
        if outcome_note is not None:
            result.append(f"  - Outcome: {outcome_note}")

        evidence = CompassRenderer._render_evidence(item)
        if evidence:
            result.append(f"  - Evidence: {evidence}")

        return result

    # ── Evidence rendering ─────────────────────────────────────────────

    @staticmethod
    def _render_evidence(item: CompassItem) -> str | None:
        """Render evidence/progress links as a compact comma-separated list.

        Format:
            `commitments:<uuid>` (evidence), `events:<uuid>` (progress)

        Returns None if there are no links.
        """
        if not item.links:
            return None

        parts: list[str] = []
        for link in sorted(
            item.links,
            key=lambda lk: (lk.target_table, lk.relation, lk.created_at),
        ):
            compact = f"`{link.target_table}:{link.target_id}` ({link.relation})"
            parts.append(compact)

        return ", ".join(parts)


# Re-export for convenience at module level.
__all__ = [
    "CompassItem",
    "CompassSnapshot",
    "CompassRenderer",
    "build_compass_snapshot",
]
