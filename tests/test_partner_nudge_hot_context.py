"""Render `## Incoming nudge from your partner` block when a scheduled
partner_nudge task fires.

Invariant 4: the `reason` field is audit-only and MUST NOT appear in
any rendered hot context. The trigger-metadata `context` dump is
suppressed for partner_nudge kinds to prevent leakage.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.hot_context_solo import (
    build_hot_context_solo,
    render_hot_context_solo,
)
from app.services.hot_context import HotContext

pytestmark = pytest.mark.anyio


SECRET_REASON = "PRIVATE_AUDIT_ONLY_REASON_DO_NOT_RENDER"
NUDGE_NOTE = "Pom asked me to see how you're doing today."


async def _build_solo_partner_nudge_hc(
    fake_pool, user, partner=None, *, nudge_note=NUDGE_NOTE, bot_id="tante_rosi"
):
    fake_pool.users[user.id] = {
        "id": user.id,
        "name": user.name,
        "phone": user.phone,
        "timezone": user.timezone,
    }
    if partner is not None:
        fake_pool.users[partner.id] = {
            "id": partner.id,
            "name": partner.name,
            "phone": partner.phone,
            "timezone": partner.timezone,
        }
        fake_pool.dyad_partners[user.id] = partner.id
    trigger_metadata = {
        "kind": "scheduled_task",
        "context": {
            "kind": "partner_nudge",
            "originating_user_id": str(partner.id) if partner else str(uuid4()),
            "originating_user_name": partner.name if partner else None,
            "nudge_note": nudge_note,
            "reason": SECRET_REASON,
            "source": "explicit_user_request",
            "scheduled_for": datetime.now(UTC).isoformat(),
        },
    }
    return await build_hot_context_solo(
        fake_pool,
        user,
        triggering_message_ids=[],
        trigger_metadata=trigger_metadata,
        primary_topic_id=get_relationship_topic_id(),
        bot_id=bot_id,
    )


async def test_solo_partner_nudge_renders_block_with_originator_and_note(fake_pool):
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    hc = await _build_solo_partner_nudge_hc(fake_pool, recipient, originator)
    rendered = render_hot_context_solo(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "Pom" in rendered
    assert NUDGE_NOTE in rendered


async def test_solo_partner_nudge_omits_audit_reason(fake_pool):
    """Invariant 4: `reason` is audit-only and must NEVER render."""
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    hc = await _build_solo_partner_nudge_hc(fake_pool, recipient, originator)
    rendered = render_hot_context_solo(hc)
    assert SECRET_REASON not in rendered
    # Also ensure the raw `- context:` jsonb dump did not happen — that
    # dump would have leaked `reason`.
    assert "PRIVATE_AUDIT_ONLY_REASON" not in rendered


async def test_solo_partner_nudge_falls_back_to_generic_note(fake_pool):
    recipient = User(uuid4(), "Hannah", "15555550101", "UTC")
    originator = User(uuid4(), "Pom", "15555550100", "UTC")
    hc = await _build_solo_partner_nudge_hc(
        fake_pool, recipient, originator, nudge_note=None
    )
    rendered = render_hot_context_solo(hc)
    assert "## Incoming nudge from your partner" in rendered
    assert "asked me to check in with you" in rendered


def test_dyadic_render_branch_emits_block_and_drops_reason():
    """Mediator renderer in hot_context.py must mirror the solo branch."""
    # Use a minimal HotContext-like dataclass instance — we only need
    # the rendering function to see the trigger_metadata fields.
    from app.services.hot_context import (
        _render_with_counts,
    )

    msg_id = uuid4()
    hc = HotContext(
        current_user={
            "id": uuid4(),
            "name": "Hannah",
            "timezone": "UTC",
            "phone": "15555550101",
            "cross_thread_sharing_default": None,
            "partner_share": None,
            "partner_sharing_state": "unavailable",
            "style_notes": "",
            "onboarding_state": "complete",
        },
        partner_user={
            "id": uuid4(),
            "name": "Pom",
            "timezone": "UTC",
            "phone": "15555550100",
            "cross_thread_sharing_default": None,
            "partner_share": None,
            "partner_sharing_state": "unavailable",
            "style_notes": "",
            "onboarding_state": "complete",
        },
        conversation_load={"period": "today", "total_count": 0, "inbound_count": 0,
                           "outbound_count": 0, "period_start": None,
                           "period_end": None, "timezone": "UTC"},
        active_oob=[],
        memories=[],
        active_themes=[],
        open_watch_items=[],
        observations=[],
        distillations=[],
        bridge_candidates=[],
        recent_reactions=[],
        recent_messages=[],
        partner_shareable_summaries=[],
        topic_status=None,
        cross_topic_peek=[],
        cross_topic_status=[],
        time_since_last_message=None,
        trigger_metadata={
            "kind": "scheduled_task",
            "triggering_message_ids": [msg_id],
            "context": {
                "kind": "partner_nudge",
                "originating_user_id": str(uuid4()),
                "nudge_note": NUDGE_NOTE,
                "reason": SECRET_REASON,
            },
            "messages": [
                {
                    "id": msg_id,
                    "charge": "routine",
                    "sent_at": datetime.now(UTC),
                    "content": "scheduled nudge fire",
                }
            ],
        },
    )
    rendered = _render_with_counts(hc, truncations={})
    assert "## Incoming nudge from your partner" in rendered
    assert NUDGE_NOTE in rendered
    assert SECRET_REASON not in rendered
