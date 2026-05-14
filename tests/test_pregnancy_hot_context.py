"""Snapshot tests for pregnancy state in the solo hot context render.

Covers all render branches: no-pregnancy, active first/third trimester,
overdue, recent loss <90d, recent birth <90d, ended >90d (absent),
data-corruption (edd without dating_basis).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone, timedelta
from uuid import UUID, uuid4

import pytest

from app.models.user import User
from app.services.hot_context_solo import (
    HotContextSolo,
    build_hot_context_solo,
    _render_solo_with_counts,
    render_hot_context_solo,
)

# Fixed topic id for testing
_TOPIC_ID = UUID("00000000-0000-4000-8000-000000000001")


def _make_user(
    *,
    user_id: str = "u1",
    name: str = "Anna",
    pregnancy_edd: date | None = None,
    pregnancy_dating_basis: str | None = None,
    pregnancy_started_at: datetime | None = None,
    pregnancy_ended_at: datetime | None = None,
    pregnancy_outcome: str | None = None,
) -> User:
    """Build a User with specified pregnancy fields."""
    return User(
        id=user_id,
        name=name,
        phone="+155****0100",
        timezone="UTC",
        pregnancy_edd=pregnancy_edd,
        pregnancy_dating_basis=pregnancy_dating_basis,
        pregnancy_lmp_date=None,
        pregnancy_scan_date=None,
        pregnancy_scan_corrected_at=None,
        pregnancy_started_at=pregnancy_started_at,
        pregnancy_ended_at=pregnancy_ended_at,
        pregnancy_outcome=pregnancy_outcome,
    )


def _make_hc(pregnancy_state: str | None) -> HotContextSolo:
    """Build a minimal HotContextSolo with only the pregnancy_state set."""
    return HotContextSolo(
        current_user={
            "id": "u1",
            "name": "Anna",
            "timezone": "UTC",
            "onboarding_state": "pending",
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
        trigger_metadata={"kind": "test", "triggering_message_ids": [], "messages": []},
        pregnancy_state=pregnancy_state,
    )


class TestPregnancyHotContextSolo:
    """Solo hot context render with pregnancy state."""

    def test_no_pregnancy_state_omits_section(self):
        """When pregnancy_state is None, no '## Pregnancy' section appears."""
        hc = _make_hc(pregnancy_state=None)
        rendered = render_hot_context_solo(hc)
        assert "## Pregnancy" not in rendered

    def test_active_first_trimester_renders(self):
        """Active pregnancy in first trimester renders correctly."""
        # EDD 2026-12-01, today 2026-05-12 → about 11w (first trimester)
        hc = _make_hc(
            pregnancy_state="11w2d (first trimester, EDD 2026-12-01, basis: lmp)"
        )
        rendered = render_hot_context_solo(hc)
        assert "## Pregnancy" in rendered
        assert "11w2d (first trimester, EDD 2026-12-01, basis: lmp)" in rendered

    def test_active_third_trimester_renders(self):
        """Active pregnancy in third trimester renders correctly."""
        hc = _make_hc(
            pregnancy_state="30w0d (third trimester, EDD 2026-07-20, basis: scan)"
        )
        rendered = render_hot_context_solo(hc)
        assert "## Pregnancy" in rendered
        assert "30w0d (third trimester, EDD 2026-07-20, basis: scan)" in rendered

    def test_overdue_renders(self):
        """Overdue pregnancy (>42w) renders correctly."""
        hc = _make_hc(pregnancy_state="42w (overdue, EDD was 2026-03-01)")
        rendered = render_hot_context_solo(hc)
        assert "## Pregnancy" in rendered
        assert "42w (overdue, EDD was 2026-03-01)" in rendered

    def test_recent_loss_renders(self):
        """Recent loss (<90 days) renders with sensitivity."""
        hc = _make_hc(pregnancy_state="Recent loss (12 days ago). Handle with care.")
        rendered = render_hot_context_solo(hc)
        assert "## Pregnancy" in rendered
        assert "Recent loss (12 days ago). Handle with care." in rendered

    def test_recent_birth_renders(self):
        """Recent birth (<90 days) renders correctly."""
        hc = _make_hc(pregnancy_state="Birth 5 days ago (EDD was 2026-10-22).")
        rendered = render_hot_context_solo(hc)
        assert "## Pregnancy" in rendered
        assert "Birth 5 days ago (EDD was 2026-10-22)." in rendered

    def test_ended_older_than_90_days_absent(self):
        """Pregnancy ended >90 days ago should return None (omit section)."""
        hc = _make_hc(pregnancy_state=None)  # format_pregnancy_state returns None
        rendered = render_hot_context_solo(hc)
        assert "## Pregnancy" not in rendered

    def test_data_corruption_edd_without_dating_basis(self):
        """When edd is set but dating_basis is null, log warning + omit section."""
        user = _make_user(
            pregnancy_edd=date(2026, 10, 22),
            pregnancy_dating_basis=None,  # data corruption
        )
        from app.services.pregnancy import format_pregnancy_state

        result = format_pregnancy_state(user)
        assert result is None  # should omit section for safety

    def test_empty_pregnancy_state_not_rendered_in_solo(self):
        """Empty/None pregnancy_state should not appear in rendered output."""
        hc = _make_hc(pregnancy_state=None)
        rendered = _render_solo_with_counts(hc, {}, clip_limit=240)
        assert "## Pregnancy" not in rendered

    def test_pregnancy_section_after_topic_status(self):
        """Pregnancy section appears after topic_status and before cross-topic."""
        hc = _make_hc(
            pregnancy_state="17w2d (second trimester, EDD 2026-10-22, basis: lmp)"
        )
        # Add topic_status to verify ordering
        hc = HotContextSolo(
            current_user=hc.current_user,
            partner_user=hc.partner_user,
            conversation_load=hc.conversation_load,
            active_oob=hc.active_oob,
            memories=hc.memories,
            active_themes=hc.active_themes,
            open_watch_items=hc.open_watch_items,
            observations=hc.observations,
            recent_messages=hc.recent_messages,
            time_since_last_message=hc.time_since_last_message,
            trigger_metadata=hc.trigger_metadata,
            topic_status={
                "headline": "test",
                "last_updated_at": datetime.now(timezone.utc),
            },
            pregnancy_state=hc.pregnancy_state,
        )
        rendered = render_hot_context_solo(hc)
        # Pregnancy section should appear after topic_status
        ts_idx = rendered.index("## Topic status")
        preg_idx = rendered.index("## Pregnancy")
        assert ts_idx < preg_idx, "Pregnancy section should appear after topic status"

    def test_pregnancy_section_before_cross_topic(self):
        """Pregnancy section appears before cross-topic peek section."""
        hc = _make_hc(
            pregnancy_state="17w2d (second trimester, EDD 2026-10-22, basis: lmp)"
        )
        # Add cross_topic_peek to verify ordering
        hc = HotContextSolo(
            current_user=hc.current_user,
            partner_user=hc.partner_user,
            conversation_load=hc.conversation_load,
            active_oob=hc.active_oob,
            memories=hc.memories,
            active_themes=hc.active_themes,
            open_watch_items=hc.open_watch_items,
            observations=hc.observations,
            recent_messages=hc.recent_messages,
            time_since_last_message=hc.time_since_last_message,
            trigger_metadata=hc.trigger_metadata,
            cross_topic_peek=[
                {
                    "slug": "career",
                    "display_name": "Career",
                    "last_active_at": datetime.now(timezone.utc),
                }
            ],
            pregnancy_state=hc.pregnancy_state,
        )
        rendered = render_hot_context_solo(hc)
        preg_idx = rendered.index("## Pregnancy")
        ct_idx = rendered.index("## Cross-topic activity")
        assert (
            preg_idx < ct_idx
        ), "Pregnancy section should appear before cross-topic section"

    @pytest.mark.asyncio
    async def test_solo_partner_sharing_pending_only_with_dyad_partner(self, fake_pool):
        user = _make_user(user_id=str(uuid4()))
        partner = _make_user(user_id=str(uuid4()), name="Ben")
        fake_pool.users[user.id] = user.__dict__.copy()
        fake_pool.users[partner.id] = partner.__dict__.copy()
        fake_pool.dyad_partners[user.id] = partner.id

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_TOPIC_ID,
            bot_id="tante_rosi",
        )
        assert hc.current_user["partner_share"] is None
        assert hc.current_user["partner_sharing_state"] == "pending"
        rendered = render_hot_context_solo(hc)
        assert "- partner_sharing_state: pending" in rendered

        fake_pool.dyad_partners.clear()
        no_partner = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_TOPIC_ID,
            bot_id="tante_rosi",
        )
        assert no_partner.current_user["partner_sharing_state"] == "unavailable"

    @pytest.mark.asyncio
    async def test_solo_hot_context_does_not_surface_partner_rows(self, fake_pool):
        user = _make_user(user_id=str(uuid4()))
        partner = _make_user(user_id=str(uuid4()), name="Ben")
        fake_pool.users[user.id] = user.__dict__.copy()
        fake_pool.users[partner.id] = partner.__dict__.copy()
        fake_pool.dyad_partners[user.id] = partner.id
        fake_pool.user_bot_state[(user.id, "tante_rosi")] = {
            "user_id": user.id,
            "bot_id": "tante_rosi",
            "partner_share": "opt_in",
        }
        partner_memory_id = uuid4()
        fake_pool.memories[partner_memory_id] = {
            "id": partner_memory_id,
            "about_user_id": partner.id,
            "content": "partner private memory",
            "visibility": "dyad_shareable",
            "shareable_summary": "partner safe summary",
            "related_theme_ids": [],
            "status": "active",
            "created_at": datetime.now(timezone.utc),
            "last_referenced_at": None,
            "recorded_by_bot_id": "tante_rosi",
        }
        fake_pool.messages[uuid4()] = {
            "id": uuid4(),
            "direction": "inbound",
            "sender_id": partner.id,
            "recipient_id": None,
            "content": "partner raw message",
            "processing_state": "processed",
            "sent_at": datetime.now(timezone.utc),
            "charge": "routine",
            "deleted_at": None,
            "bot_id": "tante_rosi",
            "topic_id": _TOPIC_ID,
        }

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_TOPIC_ID,
            bot_id="tante_rosi",
        )
        rendered = render_hot_context_solo(hc)

        assert "partner private memory" not in rendered
        assert "partner safe summary" not in rendered
        assert "partner raw message" not in rendered

    @pytest.mark.asyncio
    async def test_partner_pregnancy_state_renders_for_non_pregnant_user(
        self, fake_pool
    ):
        user = _make_user(user_id=str(uuid4()), name="Peter")
        partner = _make_user(
            user_id=str(uuid4()),
            name="Hannah",
            pregnancy_edd=date(2027, 1, 12),
            pregnancy_dating_basis="lmp",
        )
        fake_pool.users[user.id] = user.__dict__.copy()
        fake_pool.users[partner.id] = partner.__dict__.copy()
        fake_pool.dyad_partners[user.id] = partner.id

        hc = await build_hot_context_solo(
            fake_pool,
            user,
            [],
            {"kind": "test"},
            primary_topic_id=_TOPIC_ID,
            bot_id="tante_rosi",
        )
        assert hc.pregnancy_state is None
        assert hc.partner_pregnancy_state is not None
        assert "- subject: Hannah" in hc.partner_pregnancy_state
        assert "- estimated_due_date: 2027-01-12" in hc.partner_pregnancy_state

        rendered = render_hot_context_solo(hc)
        assert "## Partner pregnancy" in rendered
        assert "- estimated_due_date: 2027-01-12" in rendered
        assert "`pregnancy_edd` is not set." not in rendered


class TestPregnancyHelperIntegration:
    """Integration tests: format_pregnancy_state called from hot_context_solo."""

    def test_format_pregnancy_state_active(self):
        """format_pregnancy_state returns correct string for active pregnancy."""
        user = _make_user(
            pregnancy_edd=date(2026, 10, 22),
            pregnancy_dating_basis="lmp",
            pregnancy_started_at=datetime(2026, 1, 15, tzinfo=timezone.utc),
        )
        from app.services.pregnancy import format_pregnancy_state

        # Use an explicit today to get deterministic output
        result = format_pregnancy_state(user, today=date(2026, 5, 12))
        assert result is not None
        assert "- gestational_age_today: 16w5d" in result
        assert "- pregnancy_week: week 17" in result
        assert "- trimester: second" in result
        assert "- estimated_due_date: 2026-10-22" in result
        assert "- dating_basis: lmp" in result

    def test_format_pregnancy_state_recent_loss(self):
        """format_pregnancy_state returns loss message for recent loss."""
        ended = datetime(2026, 5, 5, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2026, 10, 22),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=ended,
            pregnancy_outcome="loss",
        )
        from app.services.pregnancy import format_pregnancy_state

        result = format_pregnancy_state(user, today=date(2026, 5, 12))
        assert result is not None
        assert "Recent loss" in result
        assert "Handle with care" in result

    def test_format_pregnancy_state_recent_birth(self):
        """format_pregnancy_state returns birth message for recent birth."""
        ended = datetime(2026, 5, 10, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2026, 10, 22),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=ended,
            pregnancy_outcome="birth",
        )
        from app.services.pregnancy import format_pregnancy_state

        result = format_pregnancy_state(user, today=date(2026, 5, 12))
        assert result is not None
        assert "Birth" in result
        assert "EDD was 2026-10-22" in result

    def test_format_pregnancy_state_no_edd(self):
        """format_pregnancy_state returns None when EDD is null."""
        user = _make_user()
        from app.services.pregnancy import format_pregnancy_state

        assert format_pregnancy_state(user) is None

    def test_format_pregnancy_state_old_ended(self):
        """format_pregnancy_state returns None for ended >90 days ago."""
        ended = datetime(2026, 1, 1, tzinfo=timezone.utc)
        user = _make_user(
            pregnancy_edd=date(2026, 10, 22),
            pregnancy_dating_basis="lmp",
            pregnancy_ended_at=ended,
            pregnancy_outcome="birth",
        )
        from app.services.pregnancy import format_pregnancy_state

        result = format_pregnancy_state(user, today=date(2026, 5, 12))
        assert result is None  # >90 days ago

    def test_data_corruption_logs_warning(self, caplog):
        """EDD without dating_basis logs warning and returns None."""
        user = _make_user(
            pregnancy_edd=date(2026, 10, 22),
            pregnancy_dating_basis=None,
        )
        from app.services.pregnancy import format_pregnancy_state

        with caplog.at_level(logging.WARNING):
            result = format_pregnancy_state(user)
        assert result is None
        assert any("pregnancy_dating_basis" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Dyad hot context tests — partner pregnancy one-liner (§4.1 guarantee)
# ---------------------------------------------------------------------------


class TestPartnerPregnancyStateHelper:
    """Tests for the _render_partner_pregnancy_state helper in hot_context."""

    def test_no_pregnancy_returns_none(self):
        """When partner has no pregnancy_edd, return None."""
        from app.services.hot_context import _render_partner_pregnancy_state

        partner_user = {"name": "Eva", "pregnancy_edd": None}
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is None

    def test_active_pregnancy_one_liner(self):
        """Active pregnancy renders the one-line gestational summary."""
        from app.services.hot_context import _render_partner_pregnancy_state

        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_started_at": datetime(2026, 1, 15, tzinfo=timezone.utc),
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is not None
        assert "Eva" in result
        assert "pregnant" in result
        assert "EDD" in result
        assert "2026-10-22" in result

    def test_recent_loss_one_liner(self):
        """Recent loss (<90d) renders the sensitivity one-liner."""
        from app.services.hot_context import _render_partner_pregnancy_state

        ended = datetime.now(timezone.utc) - timedelta(days=12)
        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_ended_at": ended,
            "pregnancy_outcome": "loss",
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is not None
        assert "Eva" in result
        assert "loss" in result
        assert "Handle with care" in result

    def test_termination_renders_as_loss(self):
        """Termination renders as loss for sensitivity purposes."""
        from app.services.hot_context import _render_partner_pregnancy_state

        ended = datetime.now(timezone.utc) - timedelta(days=5)
        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_ended_at": ended,
            "pregnancy_outcome": "termination",
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is not None
        assert "loss" in result
        assert "Handle with care" in result

    def test_birth_outcome_omitted_from_dyad(self):
        """Birth outcome is NOT rendered in the dyad partner state (only
        loss/termination are surfaced)."""
        from app.services.hot_context import _render_partner_pregnancy_state

        ended = datetime.now(timezone.utc) - timedelta(days=5)
        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_ended_at": ended,
            "pregnancy_outcome": "birth",
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is None

    def test_loss_older_than_90_days_omitted(self):
        """Loss >90 days ago returns None (stale)."""
        from app.services.hot_context import _render_partner_pregnancy_state

        ended = datetime.now(timezone.utc) - timedelta(days=100)
        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_ended_at": ended,
            "pregnancy_outcome": "loss",
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is None

    def test_data_corruption_edd_without_dating_basis(self):
        """EDD set without dating_basis → return None (no crash)."""
        from app.services.hot_context import _render_partner_pregnancy_state

        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": None,
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is None

    def test_partner_fetch_carries_pregnancy_fields(self):
        """Smoke test: partner_user dict with pregnancy fields is handled
        correctly. Simulates what _user_profile returns."""
        from app.services.hot_context import _render_partner_pregnancy_state

        # Full partner_user as _user_profile would return for active pregnancy
        partner_user = {
            "id": "p1",
            "name": "Eva",
            "phone": None,
            "timezone": "UTC",
            "style_notes": "",
            "onboarding_state": "pending",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_lmp_date": None,
            "pregnancy_scan_date": None,
            "pregnancy_scan_corrected_at": None,
            "pregnancy_started_at": datetime(2026, 1, 15, tzinfo=timezone.utc),
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is not None
        assert "Eva" in result
        assert "pregnant" in result


class TestNoAutoBridgingGuarantee:
    """§4.1: The dyad pregnancy render MUST NOT auto-bridge symptoms, themes,
    weight, or observations."""

    def test_dyad_one_liner_contains_no_theme_data(self):
        """The one-liner should only contain name, weeks/days, and EDD."""
        from app.services.hot_context import _render_partner_pregnancy_state

        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_ended_at": None,
            "pregnancy_outcome": None,
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is not None
        # Must NOT contain theme/observation/weight language
        assert "theme" not in result.lower()
        assert "observation" not in result.lower()
        assert "weight" not in result.lower()
        assert "symptom" not in result.lower()
        # Must NOT contain raw pregnancy data like lmp_date or scan_date
        assert "lmp_date" not in result
        assert "scan_date" not in result

    def test_loss_one_liner_contains_no_theme_data(self):
        """The loss one-liner should only contain name, 'loss', days ago."""
        from app.services.hot_context import _render_partner_pregnancy_state

        ended = datetime.now(timezone.utc) - timedelta(days=12)
        partner_user = {
            "name": "Eva",
            "pregnancy_edd": date(2026, 10, 22),
            "pregnancy_dating_basis": "lmp",
            "pregnancy_ended_at": ended,
            "pregnancy_outcome": "loss",
        }
        result = _render_partner_pregnancy_state(partner_user, "Eva")
        assert result is not None
        assert "theme" not in result.lower()
        assert "observation" not in result.lower()
        assert "weight" not in result.lower()
