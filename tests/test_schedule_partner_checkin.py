"""Tests for `schedule_partner_checkin` and `cancel_partner_nudge`.

Covers the edge cases from megaplans/partner-nudge-brief.md:
no_dyad_partner, recipient pending, recipient opt_out, recipient opt_in
(happy path), schema invariant 2 (no user_id field), code-side 24h rate
limit, the unique partial index (older-than-24h seeded row), bilateral
nudges in 24h, and originator-only cancellation.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.bots.registry import get_relationship_topic_id
from app.models.user import User
from app.services.tools import write_tools
from app.services.turn_context import TurnContext
from tool_schemas import (
    CancelPartnerNudgeInput,
    SchedulePartnerCheckinInput,
)

pytestmark = pytest.mark.anyio


def _build_ctx(fake_pool, user, partner=None, bot_id="tante_rosi"):
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
    turn_id = uuid4()
    fake_pool.bot_turns[turn_id] = {
        "id": turn_id,
        "reasoning": "",
        "completed_at": None,
        "failure_reason": None,
    }
    return TurnContext(
        turn_id,
        fake_pool,
        user,
        partner,
        [uuid4()],
        current_step="schedule",
        bot_id=bot_id,
        user_id=user.id,
        primary_topic_id=get_relationship_topic_id(),
    )


def _set_partner_share(pool, user_id, bot_id, partner_share):
    pool.user_bot_state[(user_id, bot_id)] = {
        "user_id": user_id,
        "bot_id": bot_id,
        "partner_share": partner_share,
    }


def test_schema_has_no_user_id_field() -> None:
    """Invariant 2: SchedulePartnerCheckinInput must not accept a user_id."""
    schema = SchedulePartnerCheckinInput.model_json_schema()
    props = schema.get("properties", {})
    assert "user_id" not in props
    assert "target_user_id" not in props
    assert "recipient_user_id" not in props


async def test_no_dyad_partner_rejects_without_inserting(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    # No dyad_partners entry → resolve_dyad_partner returns None
    ctx = _build_ctx(fake_pool, user, partner=None, bot_id="tante_rosi")
    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.schedule_partner_checkin(
            ctx,
            SchedulePartnerCheckinInput(
                delay={"hours": 2},
                nudge_note="Pom asked me to check in.",
                reason="explicit user request",
            ),
        )
    assert exc.value.result["error"] == "no_dyad_partner"
    assert len(fake_pool.scheduled_jobs) == 0


async def test_recipient_pending_hard_blocks(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[user.id] = partner.id
    ctx = _build_ctx(fake_pool, user, partner, bot_id="tante_rosi")
    # No user_bot_state for partner → pending
    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.schedule_partner_checkin(
            ctx,
            SchedulePartnerCheckinInput(
                delay={"hours": 2},
                reason="explicit user request",
            ),
        )
    assert exc.value.result["error"] == "recipient_not_opted_in"
    assert exc.value.result["recipient_state"] == "pending"
    assert len(fake_pool.scheduled_jobs) == 0


async def test_recipient_opt_out_hard_blocks(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[user.id] = partner.id
    _set_partner_share(fake_pool, partner.id, "tante_rosi", "opt_out")
    ctx = _build_ctx(fake_pool, user, partner, bot_id="tante_rosi")
    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.schedule_partner_checkin(
            ctx,
            SchedulePartnerCheckinInput(
                delay={"hours": 2},
                reason="explicit user request",
            ),
        )
    assert exc.value.result["error"] == "recipient_not_opted_in"
    assert exc.value.result["recipient_state"] == "opt_out"
    assert len(fake_pool.scheduled_jobs) == 0


async def test_happy_path_inserts_partner_nudge_row(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[user.id] = partner.id
    _set_partner_share(fake_pool, partner.id, "tante_rosi", "opt_in")
    ctx = _build_ctx(fake_pool, user, partner, bot_id="tante_rosi")
    result = await write_tools.schedule_partner_checkin(
        ctx,
        SchedulePartnerCheckinInput(
            delay={"hours": 2},
            nudge_note="Pom asked me to see how you're doing today.",
            reason="explicit user request — pom asked about hannah",
        ),
    )
    assert result.action == "scheduled"
    assert result.recipient_user_id == partner.id
    job = fake_pool.scheduled_jobs[result.job_id]
    assert job["user_id"] == partner.id  # written against PARTNER's id
    assert job["bot_id"] == "tante_rosi"
    assert job["job_type"] == "scheduled_task"
    assert job["status"] == "pending"
    context = job["context"]
    assert context["kind"] == "partner_nudge"
    assert context["originating_user_id"] == str(user.id)
    assert context["nudge_note"] == "Pom asked me to see how you're doing today."
    assert context["reason"] == "explicit user request — pom asked about hannah"
    assert context["source"] == "explicit_user_request"


async def test_rate_limit_blocks_second_nudge_within_24h(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[user.id] = partner.id
    _set_partner_share(fake_pool, partner.id, "tante_rosi", "opt_in")
    ctx = _build_ctx(fake_pool, user, partner, bot_id="tante_rosi")
    await write_tools.schedule_partner_checkin(
        ctx,
        SchedulePartnerCheckinInput(
            delay={"hours": 2}, reason="first"
        ),
    )
    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.schedule_partner_checkin(
            ctx,
            SchedulePartnerCheckinInput(
                delay={"hours": 3}, reason="second within 24h"
            ),
        )
    assert exc.value.result["error"] == "rate_limited"
    assert exc.value.result["window_hours"] == 24


async def test_unique_index_blocks_stacked_pending_when_rate_limit_bypassed(
    fake_pool,
):
    """The 24h rate limit fires BEFORE the DB constraint, so to reach
    the index path we must seed an older-than-24h pending row. Then a
    new insert with the same originator/recipient/bot still hits the
    unique partial index and returns duplicate_pending_nudge.
    """
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[user.id] = partner.id
    _set_partner_share(fake_pool, partner.id, "tante_rosi", "opt_in")
    # Seed a pending nudge older than 24h so the rate limit count is 0
    # but the unique partial index would fire.
    old_job_id = uuid4()
    fake_pool.scheduled_jobs[old_job_id] = {
        "id": old_job_id,
        "user_id": partner.id,
        "job_type": "scheduled_task",
        "scheduled_for": datetime.now(UTC) + timedelta(hours=1),
        "context": {
            "kind": "partner_nudge",
            "originating_user_id": str(user.id),
            "nudge_note": "earlier nudge still pending",
            "reason": "older than 24h",
            "source": "explicit_user_request",
        },
        "status": "pending",
        "bot_id": "tante_rosi",
        "topic_id": get_relationship_topic_id(),
        "created_at": datetime.now(UTC) - timedelta(hours=48),
    }
    ctx = _build_ctx(fake_pool, user, partner, bot_id="tante_rosi")
    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.schedule_partner_checkin(
            ctx,
            SchedulePartnerCheckinInput(
                delay={"hours": 2}, reason="should hit unique index"
            ),
        )
    assert exc.value.result["error"] == "duplicate_pending_nudge"


async def test_bilateral_nudges_within_24h_both_succeed(fake_pool):
    """Different originator → different unique-index slot. Both partners
    can nudge each other in the same 24h window.
    """
    a = User(uuid4(), "Pom", "15555550100", "UTC")
    b = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[a.id] = b.id
    fake_pool.dyad_partners[b.id] = a.id
    _set_partner_share(fake_pool, a.id, "tante_rosi", "opt_in")
    _set_partner_share(fake_pool, b.id, "tante_rosi", "opt_in")
    ctx_a = _build_ctx(fake_pool, a, b, bot_id="tante_rosi")
    ctx_b = _build_ctx(fake_pool, b, a, bot_id="tante_rosi")
    r1 = await write_tools.schedule_partner_checkin(
        ctx_a,
        SchedulePartnerCheckinInput(delay={"hours": 2}, reason="A→B"),
    )
    r2 = await write_tools.schedule_partner_checkin(
        ctx_b,
        SchedulePartnerCheckinInput(delay={"hours": 3}, reason="B→A"),
    )
    assert r1.recipient_user_id == b.id
    assert r2.recipient_user_id == a.id
    assert r1.job_id != r2.job_id


async def test_cancel_partner_nudge_by_originator_succeeds(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[user.id] = partner.id
    _set_partner_share(fake_pool, partner.id, "tante_rosi", "opt_in")
    ctx = _build_ctx(fake_pool, user, partner, bot_id="tante_rosi")
    scheduled = await write_tools.schedule_partner_checkin(
        ctx,
        SchedulePartnerCheckinInput(delay={"hours": 2}, reason="to cancel"),
    )
    cancelled = await write_tools.cancel_partner_nudge(
        ctx, CancelPartnerNudgeInput(job_id=scheduled.job_id)
    )
    assert cancelled.action == "cancelled"
    assert fake_pool.scheduled_jobs[scheduled.job_id]["status"] == "cancelled"


async def test_cancel_partner_nudge_rejects_non_owner(fake_pool):
    a = User(uuid4(), "Pom", "15555550100", "UTC")
    b = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[a.id] = b.id
    fake_pool.dyad_partners[b.id] = a.id
    _set_partner_share(fake_pool, b.id, "tante_rosi", "opt_in")
    ctx_a = _build_ctx(fake_pool, a, b, bot_id="tante_rosi")
    scheduled = await write_tools.schedule_partner_checkin(
        ctx_a,
        SchedulePartnerCheckinInput(delay={"hours": 2}, reason="A→B"),
    )
    # B tries to cancel A's nudge.
    ctx_b = _build_ctx(fake_pool, b, a, bot_id="tante_rosi")
    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.cancel_partner_nudge(
            ctx_b, CancelPartnerNudgeInput(job_id=scheduled.job_id)
        )
    assert exc.value.result["error"] == "not_owner"


async def test_cancel_partner_nudge_rejects_non_pending(fake_pool):
    user = User(uuid4(), "Pom", "15555550100", "UTC")
    partner = User(uuid4(), "Hannah", "15555550101", "UTC")
    fake_pool.dyad_partners[user.id] = partner.id
    _set_partner_share(fake_pool, partner.id, "tante_rosi", "opt_in")
    ctx = _build_ctx(fake_pool, user, partner, bot_id="tante_rosi")
    scheduled = await write_tools.schedule_partner_checkin(
        ctx,
        SchedulePartnerCheckinInput(delay={"hours": 2}, reason="will cancel once"),
    )
    fake_pool.scheduled_jobs[scheduled.job_id]["status"] = "completed"
    with pytest.raises(write_tools.ToolCallRejected) as exc:
        await write_tools.cancel_partner_nudge(
            ctx, CancelPartnerNudgeInput(job_id=scheduled.job_id)
        )
    assert exc.value.result["error"] == "not_pending"
