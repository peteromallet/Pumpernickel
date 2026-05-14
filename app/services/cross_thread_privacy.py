"""Shared cross-thread privacy decisions.

This module deliberately handles raw message visibility by explicit thread owner
only. Memories and observations need a future provenance field before they can
use the same raw cross-thread filter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal, Mapping
from uuid import UUID

PartnerShareState = Literal["unset", "opt_in", "opt_out"]
RawMessageVisibilityReason = Literal[
    "current_user_thread",
    "thread_owner_partner_share_opted_in",
    "thread_owner_partner_share_not_opted_in",
]

BRIDGE_TARGET_VISIBLE_STATUSES = frozenset({"ready", "sent", "addressed"})
RAW_PARTNER_CONTENT_REDACTION = "[raw partner content withheld by partner_share]"
RAW_PARTNER_CONTENT_OMISSION_REASON = "raw_partner_content_hidden_by_partner_share"


@dataclass(frozen=True)
class RawMessageVisibility:
    visible: bool
    partner_share: PartnerShareState
    reason: RawMessageVisibilityReason
    redaction: str | None = None
    omission_reason: str | None = None


def normalize_partner_share_for_privacy(value: Any) -> PartnerShareState:
    """Normalize partner_share values for display and privacy checks."""
    if isinstance(value, Enum):
        value = value.value
    if value in (None, "", "unset"):
        return "unset"
    if value == "opt_in":
        return "opt_in"
    if value == "opt_out":
        return "opt_out"
    return "unset"


def raw_message_visibility(
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_partner_share: Any,
) -> RawMessageVisibility:
    """Return whether a viewer can see raw content from the message's thread owner.

    Callers must pass the owner partner_share for the relevant message bot.
    Own-thread reads are always visible; partner reads require opt_in.
    """
    partner_share = normalize_partner_share_for_privacy(thread_owner_partner_share)
    if viewer_user_id == thread_owner_user_id:
        return RawMessageVisibility(
            visible=True,
            partner_share=partner_share,
            reason="current_user_thread",
        )
    if partner_share == "opt_in":
        return RawMessageVisibility(
            visible=True,
            partner_share=partner_share,
            reason="thread_owner_partner_share_opted_in",
        )
    return RawMessageVisibility(
        visible=False,
        partner_share=partner_share,
        reason="thread_owner_partner_share_not_opted_in",
        redaction=RAW_PARTNER_CONTENT_REDACTION,
        omission_reason=RAW_PARTNER_CONTENT_OMISSION_REASON,
    )


def can_view_raw_message(
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_partner_share: Any,
) -> bool:
    return raw_message_visibility(
        viewer_user_id=viewer_user_id,
        thread_owner_user_id=thread_owner_user_id,
        thread_owner_partner_share=thread_owner_partner_share,
    ).visible


def redact_raw_message_content(
    content: Any,
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_partner_share: Any,
) -> str:
    visibility = raw_message_visibility(
        viewer_user_id=viewer_user_id,
        thread_owner_user_id=thread_owner_user_id,
        thread_owner_partner_share=thread_owner_partner_share,
    )
    if visibility.visible:
        return "" if content is None else str(content)
    return visibility.redaction or RAW_PARTNER_CONTENT_REDACTION


def should_omit_raw_message(
    *,
    viewer_user_id: UUID,
    thread_owner_user_id: UUID,
    thread_owner_partner_share: Any,
) -> bool:
    return not can_view_raw_message(
        viewer_user_id=viewer_user_id,
        thread_owner_user_id=thread_owner_user_id,
        thread_owner_partner_share=thread_owner_partner_share,
    )


def is_bridge_status_target_visible(status: Any) -> bool:
    if isinstance(status, Enum):
        status = status.value
    return str(status) in BRIDGE_TARGET_VISIBLE_STATUSES


def bridge_candidate_visible_to_target(
    candidate: Mapping[str, Any],
    *,
    target_user_id: UUID | None = None,
) -> bool:
    status = candidate.get("status")
    if isinstance(status, Enum):
        status = status.value
    status = str(status)
    if not is_bridge_status_target_visible(status):
        return False
    if status == "ready":
        partner_path = candidate.get("partner_path", "message_partner")
        if isinstance(partner_path, Enum):
            partner_path = partner_path.value
        # Gate ready rows by path so source-only bookkeeping rows such as
        # hold_for_context or coach_in_person cannot leak through target lists.
        if partner_path != "message_partner":
            return False
    if target_user_id is None:
        return True
    return candidate.get("target_user_id") == target_user_id
