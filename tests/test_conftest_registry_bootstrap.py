"""Tests for Step 4.6 — registry bootstrap and link_topic / _row_matches_topic.

Confirms that the test infrastructure seeded by T5 is sound before proceeding
to the read-rewrite work in T7–T13.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from app.bots.registry import get_relationship_topic_id
from tests.conftest import FakePool

# The UUID seeded by the autouse session-scoped fixture in conftest.py.
RELATIONSHIP_TOPIC = UUID("00000000-0000-4000-8000-000000000001")


def test_relationship_topic_seeded() -> None:
    """get_relationship_topic_id() returns the UUID seeded by the autouse fixture."""
    result = get_relationship_topic_id()
    assert result == RELATIONSHIP_TOPIC, (
        f"Expected seeded UUID {RELATIONSHIP_TOPIC}, got {result}"
    )


def test_link_topic_and_row_matches() -> None:
    """_row_matches_topic covers positive, negative, and unlinked-fallback cases."""
    pool = FakePool()
    T1 = UUID("aaaaaaa1-0000-4000-8000-000000000001")
    T2 = UUID("aaaaaaa2-0000-4000-8000-000000000002")
    X = uuid4()
    Y = uuid4()

    # Link row X to topic T1.
    pool.link_topic("themes", X, T1)

    # Positive: X matches T1.
    assert pool._row_matches_topic("themes", X, T1) is True, (
        "Row X linked to T1 should match T1"
    )

    # Negative: X does NOT match T2.
    assert pool._row_matches_topic("themes", X, T2) is False, (
        "Row X linked to T1 should NOT match T2"
    )

    # Unlinked fallback: Y (never linked) returns True for any topic.
    assert pool._row_matches_topic("themes", Y, T1) is True, (
        "Unlinked row Y should return True (backward-compat fallback)"
    )
    assert pool._row_matches_topic("themes", Y, T2) is True, (
        "Unlinked row Y should return True for any topic (backward-compat fallback)"
    )