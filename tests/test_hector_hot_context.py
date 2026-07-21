"""Snapshot tests for the ## Fitness block in solo hot context.

Mirrors tests/test_pregnancy_hot_context.py pattern.  Covers rendering
with active commitments, current-week adherence, recent events, ordering
relative to ## Topic status / ## Pregnancy / ## Cross-topic, non-Hector
bot absence, empty-block absence, and copy-constructor preservation.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services.hot_context_solo import (
    HotContextSolo,
    _render_solo_with_counts,
    render_hot_context_solo,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _make_minimal_hc(
    *,
    bot_id: str = "coach",
    fitness_block: str | None = None,
    workout_block: str | None = None,
    topic_status: dict | None = None,
    pregnancy_state: str | None = None,
    partner_pregnancy_state: str | None = None,
    cross_topic_peek: list | None = None,
) -> HotContextSolo:
    """Build a minimal HotContextSolo for render testing."""
    return HotContextSolo(
        current_user={
            "id": "u1",
            "name": "Alex",
            "timezone": "UTC",
            "onboarding_state": "completed",
            "style_notes": "",
        },
        partner_user={},
        conversation_load={
            "period": "today",
            "timezone": "UTC",
            "total_count": 0,
            "inbound_count": 0,
            "outbound_count": 0,
        },
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        recent_messages=[],
        time_since_last_message=None,
        trigger_metadata={
            "kind": "test",
            "triggering_message_ids": [],
            "messages": [],
        },
        fitness_block=fitness_block,
        workout_block=workout_block,
        topic_status=topic_status,
        pregnancy_state=pregnancy_state,
        partner_pregnancy_state=partner_pregnancy_state,
        cross_topic_peek=cross_topic_peek or [],
        bot_id=bot_id,
    )


def _sample_fitness_block() -> str:
    return (
        "Current focus: morning workout\n"
        "Active commitments:\n"
        "  - morning workout (pressure=low_key)\n"
        "  - protein tracking (pressure=very_gentle)\n"
        "This week:\n"
        "  - morning workout: Mon done \u00b7 Tue missed \u00b7 Wed pending\n"
        "  - protein tracking: 5/7 days logged\n"
        "Recent events:\n"
        "  - Mon: morning workout done\n"
        "  - Mon: protein tracking done"
    )


# ── render tests (no DB) ────────────────────────────────────────────────


class TestFitnessBlockRenderingDirect:
    """Use _render_solo_with_counts for direct render checks (no Settings)."""

    def test_hector_renders_fitness_block(self):
        hc = _make_minimal_hc(bot_id="hector", fitness_block=_sample_fitness_block())
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" in rendered
        assert "Current focus: morning workout" in rendered
        assert "Active commitments:" in rendered
        assert "This week:" in rendered
        assert "Recent events:" in rendered

    def test_non_hector_omits_fitness_block(self):
        """Non-Hector bots never get fitness_block from build_hot_context_solo,
        so the render should not show ## Fitness.  (The bot_id gate is on the
        build side, not the render side.)"""
        for bot_id in ("coach", "tante_rosi", "mediator"):
            hc = _make_minimal_hc(bot_id=bot_id, fitness_block=None)
            rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
            assert "## Fitness" not in rendered, (
                f"{bot_id} should not render ## Fitness"
            )

    def test_null_fitness_block_omits_section(self):
        hc = _make_minimal_hc(bot_id="hector", fitness_block=None)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" not in rendered

    def test_empty_string_fitness_block_omits_section(self):
        hc = _make_minimal_hc(bot_id="hector", fitness_block="")
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" not in rendered

    def test_no_fitness_for_hector_without_block(self):
        hc = _make_minimal_hc(bot_id="hector", fitness_block=None)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" not in rendered


class TestFitnessBlockOrdering:
    """## Fitness block appears after ## Topic status / ## Pregnancy
    and before ## Cross-topic activity."""

    def test_fitness_block_appears_after_topic_status(self):
        hc = _make_minimal_hc(
            bot_id="hector",
            fitness_block=_sample_fitness_block(),
            topic_status={
                "headline": "test focus",
                "last_updated_at": datetime.now(timezone.utc),
            },
        )
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        ts_idx = rendered.index("## Topic status")
        fit_idx = rendered.index("## Fitness")
        assert ts_idx < fit_idx, "## Fitness should appear after ## Topic status"

    def test_fitness_block_appears_after_pregnancy(self):
        hc = _make_minimal_hc(
            bot_id="hector",
            fitness_block=_sample_fitness_block(),
            pregnancy_state="17w2d (second trimester, EDD 2026-10-22, basis: lmp)",
        )
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        preg_idx = rendered.index("## Pregnancy")
        fit_idx = rendered.index("## Fitness")
        assert preg_idx < fit_idx, "## Fitness should appear after ## Pregnancy"

    def test_fitness_block_appears_before_cross_topic(self):
        hc = _make_minimal_hc(
            bot_id="hector",
            fitness_block=_sample_fitness_block(),
            cross_topic_peek=[
                {
                    "slug": "career",
                    "display_name": "Career",
                    "last_active_at": datetime.now(timezone.utc),
                }
            ],
        )
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        fit_idx = rendered.index("## Fitness")
        ct_idx = rendered.index("## Cross-topic activity")
        assert fit_idx < ct_idx, (
            "## Fitness should appear before ## Cross-topic activity"
        )


class TestFitnessBlockContent:
    """Detailed content tests for the fitness block rendering."""

    def test_commitments_appear(self):
        fb = (
            "Current focus: workouts\n"
            "Active commitments:\n"
            "  - workouts (pressure=low_key)\n"
            "  - meal prep (pressure=firm)\n"
            "This week:\n"
            "  - workouts: Mon done \u00b7 Tue pending\n"
            "Recent events:\n"
            "  - Mon: workouts done"
        )
        hc = _make_minimal_hc(bot_id="hector", fitness_block=fb)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "workouts (pressure=low_key)" in rendered
        assert "meal prep (pressure=firm)" in rendered

    def test_adherence_summary_appears(self):
        fb = (
            "Current focus: lift\n"
            "Active commitments:\n"
            "  - lift (pressure=low_key)\n"
            "This week:\n"
            "  - lift: Mon done \u00b7 Tue missed \u00b7 Wed excused\n"
            "Recent events:\n"
            "  - Mon: lift done\n"
            "  - Tue: lift missed"
        )
        hc = _make_minimal_hc(bot_id="hector", fitness_block=fb)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "Mon done" in rendered
        assert "Tue missed" in rendered
        assert "Wed excused" in rendered

    def test_recent_events_appear(self):
        fb = (
            "Current focus: nutrition\n"
            "Active commitments:\n"
            "  - nutrition (pressure=very_gentle)\n"
            "This week:\n"
            "  - nutrition: Mon done\n"
            "Recent events:\n"
            "  - Mon: nutrition done\n"
            "  - Sun: nutrition done"
        )
        hc = _make_minimal_hc(bot_id="hector", fitness_block=fb)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "nutrition done" in rendered

    def test_fitness_block_survives_clipping(self):
        """When clip_limit is tight, ## Fitness block header still appears."""
        fb = _sample_fitness_block()
        hc = _make_minimal_hc(bot_id="hector", fitness_block=fb)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=30)
        assert "## Fitness" in rendered


class TestFitnessBlockCopyConstructor:
    """The truncation path (render_hot_context_solo) must preserve fitness_block."""

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Set required env vars so render_hot_context_solo can init Settings."""
        monkeypatch.setenv("ANTHROPIC_API_KEY", "dummy-anthropic")
        monkeypatch.setenv("DATABASE_URL", "postgresql://user:***@localhost:5432/db")
        from app.config import get_settings

        get_settings.cache_clear()

    def test_copy_constructor_preserves_fitness_block(self):
        hc = _make_minimal_hc(
            bot_id="hector", fitness_block=_sample_fitness_block()
        )
        rendered = render_hot_context_solo(hc)
        assert "## Fitness" in rendered, "Copy constructor must preserve fitness_block"
        assert "Current focus: morning workout" in rendered

    def test_copy_constructor_preserves_null_fitness_block(self):
        hc = _make_minimal_hc(bot_id="hector", fitness_block=None)
        rendered = render_hot_context_solo(hc)
        assert "## Fitness" not in rendered


class TestWorkoutBlockSnapshotRendering:
    """Snapshot tests verify workout_block content appears/omits correctly."""

    def test_workout_block_alone_renders_in_fitness_section(self):
        """When only workout_block is set (no fitness_block), it appears under ## Fitness."""
        wb = "Recent workouts (7d):\n  2026-07-20: 1 workout(s) — running, 30min total"
        hc = _make_minimal_hc(bot_id="hector", workout_block=wb)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" in rendered
        assert "Recent workouts (7d):" in rendered
        assert "running" in rendered
        assert "30min total" in rendered

    def test_workout_and_fitness_block_both_render(self):
        """When both blocks are set, both appear under ## Fitness."""
        fb = _sample_fitness_block()
        wb = "Recent workouts (7d):\n  2026-07-20: 1 workout(s) — running, 30min total"
        hc = _make_minimal_hc(
            bot_id="hector", fitness_block=fb, workout_block=wb
        )
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" in rendered
        assert "Current focus: morning workout" in rendered  # from fitness_block
        assert "Recent workouts (7d):" in rendered  # from workout_block

    def test_workout_block_null_omits_workout_text(self):
        """When workout_block is None, no workout summary text appears."""
        fb = _sample_fitness_block()
        hc = _make_minimal_hc(
            bot_id="hector", fitness_block=fb, workout_block=None
        )
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" in rendered  # fitness_block still present
        assert "Recent workouts (7d)" not in rendered

    def test_workout_block_empty_string_omitted(self):
        """Empty workout_block should not produce content."""
        hc = _make_minimal_hc(bot_id="hector", workout_block="")
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" not in rendered  # no fitness_block either
        assert "Recent workouts (7d)" not in rendered

    def test_workout_block_content_excludes_device_data(self):
        """Snapshot: no device IDs, heart-rate, or raw data in rendered workout block."""
        wb = "Recent workouts (7d):\n  2026-07-20: 2 workout(s) — running, cycling, 60min total (1 projected)"
        hc = _make_minimal_hc(bot_id="hector", workout_block=wb)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Fitness" in rendered
        assert "Recent workouts (7d):" in rendered
        assert "projected" in rendered
        # Must NOT contain sensitive data
        assert "withings" not in rendered.lower()
        assert "scanwatch" not in rendered.lower()
        assert "bpm" not in rendered.lower()
        assert "device" not in rendered.lower()
        assert "heart" not in rendered.lower()

    def test_workout_block_does_not_mention_commitments(self):
        """workout_block is informational only — no commitment language."""
        wb = "Recent workouts (7d):\n  2026-07-20: 1 workout(s) — strength, 60min total"
        hc = _make_minimal_hc(bot_id="hector", workout_block=wb)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "commitment" not in rendered.lower()
        assert "missed" not in rendered.lower()
        assert "excused" not in rendered.lower()
        assert "create" not in rendered.lower()

    def test_non_hector_fitness_section_only_if_blocks_set(self):
        """Non-Hector bots' render still shows ## Fitness if blocks are set
        (the gate is on the build side, not render side). This test confirms
        the render path, while integration tests confirm build_hot_context_solo
        never sets workout_block for non-Hector."""
        for bot_id in ("coach", "mediator", "tante_rosi"):
            hc = _make_minimal_hc(bot_id=bot_id, workout_block=None, fitness_block=None)
            rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
            assert "## Fitness" not in rendered, (
                f"{bot_id} should not render ## Fitness when no blocks are set"
            )
            assert "Recent workouts (7d)" not in rendered

    def test_copy_constructor_preserves_workout_block(self):
        """render_hot_context_solo (truncation path) must preserve workout_block."""
        wb = "Recent workouts (7d):\n  2026-07-20: 1 workout(s) — yoga, 45min total"
        hc = _make_minimal_hc(bot_id="hector", workout_block=wb)
        rendered = render_hot_context_solo(hc)
        assert "## Fitness" in rendered
        assert "Recent workouts (7d):" in rendered
        assert "yoga" in rendered

    def test_copy_constructor_preserves_null_workout_block(self):
        hc = _make_minimal_hc(bot_id="hector", workout_block=None)
        rendered = render_hot_context_solo(hc)
        assert "## Fitness" not in rendered
