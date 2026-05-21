"""Per-turn context shared by the agentic loop and tool implementations."""

from dataclasses import dataclass, field, replace
from collections.abc import Awaitable, Callable
from typing import Any, Literal
from uuid import UUID
from datetime import datetime

from app.models.user import User
from app.services.scope import InboundScope
from app.services.turn_plan import TurnPlan, TurnStep, make_turn_plan

PacedSendKind = Literal["final", "incremental_first", "incremental_next"]
BeforePacedSend = Callable[..., Awaitable[None]]


@dataclass
class TurnContext:
    turn_id: UUID
    pool: Any
    user: User
    partner: User | None  # type-only change from User; NO default, position 4 preserved
    triggering_message_ids: list[UUID]
    # Sprint 1 new optional fields (all default to None, no call-site changes required)
    bot_id: str | None = None
    transport: str | None = None
    user_id: UUID | None = None
    bot_spec: Any | None = None
    binding_id: UUID | None = None
    participants_shape: str | None = None
    primary_topic_id: UUID | None = None
    primary_topic_slug: str | None = None
    channel_id: str | None = None
    read_scopes: Any | None = None
    write_scopes: Any | None = None
    cross_topic_policy: str | None = None
    dyad_id: UUID | None = None
    # Existing fields preserved in original order
    current_step: TurnStep = "respond"
    turn_plan: TurnPlan = field(default_factory=lambda: make_turn_plan("quick_reply"))
    tool_call_log: list[str] = field(default_factory=list)
    trigger_charge: str | None = None
    explicit_partner_alert_requested: bool = False
    turn_started_at: datetime | None = None
    incremental_sending_enabled: bool = False
    protected_owner_ids: list[UUID] | None = None
    send_typing_indicator: bool = True
    before_paced_send: BeforePacedSend | None = None
    sent_message_parts: list[dict[str, Any]] | None = None
    hot_context_rendered: str | None = None
    trigger_metadata: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_scope(
        cls,
        *,
        scope: InboundScope,
        turn_id: UUID,
        pool: Any,
        user: User,
        partner: User | None,
        triggering_message_ids: list[UUID],
        bot_spec: Any | None = None,
        participants_shape: str | None = None,
        primary_topic_slug: str | None = None,
        read_scopes: Any | None = None,
        write_scopes: Any | None = None,
        cross_topic_policy: str | None = None,
        **overrides: Any,
    ) -> "TurnContext":
        """Build a TurnContext from the canonical inbound identity scope."""
        return cls(
            turn_id=turn_id,
            pool=pool,
            user=user,
            partner=partner,
            triggering_message_ids=triggering_message_ids,
            bot_id=scope.bot_id,
            transport=scope.transport,
            user_id=scope.user_id,
            bot_spec=bot_spec,
            binding_id=scope.binding_id,
            dyad_id=scope.dyad_id,
            participants_shape=participants_shape,
            primary_topic_id=scope.topic_id,
            primary_topic_slug=primary_topic_slug,
            channel_id=scope.channel_id,
            read_scopes=read_scopes,
            write_scopes=write_scopes,
            cross_topic_policy=cross_topic_policy,
            **overrides,
        )


def obs_fields(ctx_or_scope) -> dict[str, Any]:
    """Return structured logging extra dict with scope fields (None values filtered).

    Accepts TurnContext, InboundScope, or any object with matching identity
    attributes.
    """
    result: dict[str, Any] = {}
    for field in (
        "bot_id",
        "transport",
        "user_id",
        "topic_id",
        "primary_topic_id",
        "channel_id",
        "binding_id",
        "dyad_id",
    ):
        val = getattr(ctx_or_scope, field, None)
        if val is not None:
            key = (
                "topic_id"
                if field == "primary_topic_id" and "topic_id" not in result
                else field
            )
            result[key] = str(val) if not isinstance(val, (str, type(None))) else val
    return result


def scope_from_turn_context(ctx: TurnContext) -> InboundScope:
    """Rebuild the inbound identity scope carried by a TurnContext."""
    if ctx.bot_id is None or ctx.user_id is None or ctx.primary_topic_id is None:
        raise ValueError("TurnContext is missing bot_id, user_id, or primary_topic_id")
    return InboundScope(
        bot_id=ctx.bot_id,
        transport=ctx.transport,  # type: ignore[arg-type]
        user_id=ctx.user_id,
        topic_id=ctx.primary_topic_id,
        channel_id=ctx.channel_id,
        binding_id=ctx.binding_id,
        dyad_id=ctx.dyad_id,
    )


async def partner_of(pool: Any, user: User) -> User:
    rows = await pool.fetch(
        """
        SELECT id, name, phone, timezone, onboarding_state, pacing_preferences,
               pregnancy_edd, pregnancy_dating_basis, pregnancy_lmp_date, pregnancy_scan_date,
               pregnancy_scan_corrected_at, pregnancy_started_at, pregnancy_ended_at, pregnancy_outcome
        FROM users
        WHERE id <> $1
        """,
        user.id,
    )
    if len(rows) != 1:
        raise ValueError(
            f"expected exactly one partner for user {user.id}, found {len(rows)}"
        )
    row = rows[0]
    # §16.3 wi 7: prefer the canonical user_identities address; fall back to phone.
    from app.services.user_identity import resolve_user_address

    address = await resolve_user_address(pool, row["id"]) or row["phone"]
    return User(
        id=row["id"],
        name=row["name"],
        phone=address,
        timezone=row["timezone"],
        onboarding_state=(
            row["onboarding_state"] if "onboarding_state" in row else "pending"
        ),
        pacing_preferences=(
            dict(row["pacing_preferences"] or {}) if "pacing_preferences" in row else {}
        ),
        pregnancy_edd=row["pregnancy_edd"] if "pregnancy_edd" in row else None,
        pregnancy_dating_basis=(
            row["pregnancy_dating_basis"] if "pregnancy_dating_basis" in row else None
        ),
        pregnancy_lmp_date=(
            row["pregnancy_lmp_date"] if "pregnancy_lmp_date" in row else None
        ),
        pregnancy_scan_date=(
            row["pregnancy_scan_date"] if "pregnancy_scan_date" in row else None
        ),
        pregnancy_scan_corrected_at=(
            row["pregnancy_scan_corrected_at"]
            if "pregnancy_scan_corrected_at" in row
            else None
        ),
        pregnancy_started_at=(
            row["pregnancy_started_at"] if "pregnancy_started_at" in row else None
        ),
        pregnancy_ended_at=(
            row["pregnancy_ended_at"] if "pregnancy_ended_at" in row else None
        ),
        pregnancy_outcome=(
            row["pregnancy_outcome"] if "pregnancy_outcome" in row else None
        ),
    )


def replace_ctx(ctx: TurnContext, **overrides: Any) -> TurnContext:
    """Clone a TurnContext with field overrides via dataclasses.replace.

    Use this instead of constructing a new TurnContext when forking the
    context for a sub-flow (e.g. consult_perspective) so newly added fields
    are not silently dropped.
    """
    return replace(ctx, **overrides)
