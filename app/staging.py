"""Dry-run staging utilities."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.bots.registry import get_relationship_topic_id, get_pregnancy_topic_id
from app.config import get_settings
from app.db import db_lifespan
from app.models.user import fetch_user_by_id
from app.services.hot_context import build_hot_context, render_hot_context
from app.services.hot_context_solo import build_hot_context_solo, render_hot_context_solo
from app.services.pregnancy import format_pregnancy_state
from app.services.prompts import render_system_prompt
from app.services.prompts_solo import render_solo_system_prompt
from app.services.turn_context import partner_of


@dataclass
class _App:
    state: Any


class _State:
    pool: Any = None


async def _replay(pool: Any, prompt_version: str, since: str, user_id: str) -> None:
    # TODO(InboundScope): staging replay is still mediator/dyad-specific: it
    # hardcodes the relationship topic and calls partner_of. Thread a real
    # bot/topic scope before using this to verify solo bots like Tante Rosi.
    user = await fetch_user_by_id(pool, UUID(user_id))
    partner = await partner_of(pool, user)
    rows = await pool.fetch(
        """
        SELECT id, content, sent_at
        FROM messages
        WHERE direction='inbound'
          AND sender_id=$1
          AND sent_at >= $2::timestamptz
        ORDER BY sent_at ASC
        """,
        user.id,
        since,
    )
    settings = get_settings()
    for row in rows:
        hot_context = await build_hot_context(pool, user, partner, [row["id"]], {"kind": "staging_replay"}, primary_topic_id=get_relationship_topic_id(), allow_cross_topic_peek=True, allow_cross_topic_status_injection=True)
        system_prompt = render_system_prompt(
            settings.assistant_name,
            user.name,
            partner.name,
            onboarding_state=user.onboarding_state,
            current_user_sharing_default=user.cross_thread_sharing_default,
            partner_sharing_default=partner.cross_thread_sharing_default,
        )
        rendered = render_hot_context(hot_context)
        candidate = (
            f"[dry-run:{prompt_version}] Would answer {user.name} after message {row['id']}: "
            f"{str(row.get('content') or '').strip()[:160]}"
        )
        print(json.dumps({
            "message_id": str(row["id"]),
            "sent_at": str(row["sent_at"]),
            "prompt_version": prompt_version,
            "prompt_preview": f"{system_prompt}\n\n{rendered}"[:1000],
            "would_send": candidate,
            "would_write": [
                {"table": "bot_turns", "action": "insert"},
                {"table": "messages", "action": "insert_outbound"},
                {"table": "tool_calls", "action": "dry_run_record_only"},
            ],
        }, default=str))


async def _rosi_replay(pool: Any, user_id: str) -> None:
    """Staging replay for Tante Rosi — exercises solo hot context with
    pregnancy state.  Seeds a test user with pregnancy_edd in second trimester,
    builds solo hot context, and renders it."""
    user = await fetch_user_by_id(pool, UUID(user_id))
    topic_id = get_pregnancy_topic_id()
    if topic_id is None:
        print(json.dumps({"error": "pregnancy topic id not cached — run migration 0033?"}))
        return

    # Exercise format_pregnancy_state directly as a smoke test.
    state = format_pregnancy_state(user)
    print(json.dumps({
        "user_id": str(user.id),
        "user_name": user.name,
        "pregnancy_edd": user.pregnancy_edd.isoformat() if user.pregnancy_edd else None,
        "pregnancy_dating_basis": user.pregnancy_dating_basis,
        "pregnancy_active": user.pregnancy_edd is not None and user.pregnancy_ended_at is None,
        "formatted_state": state,
    }, default=str))

    # Build and render solo hot context.
    hc = await build_hot_context_solo(
        pool,
        user,
        [],  # no triggering message ids for dry run
        {"kind": "staging_rosi_replay", "triggering_message_ids": [], "messages": []},
        primary_topic_id=topic_id,
        bot_id="tante_rosi",
        allow_cross_topic_peek=True,
    )
    rendered = render_hot_context_solo(hc)
    print(json.dumps({
        "hot_context_solo_preview": rendered[:1200],
    }, default=str))


async def _main_async(args: argparse.Namespace) -> None:
    app = _App(_State())
    async with db_lifespan(app):
        if args.command == "replay":
            await _replay(app.state.pool, args.prompt_version, args.since, args.user)
        elif args.command == "rosi-replay":
            await _rosi_replay(app.state.pool, args.user)


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.staging")
    sub = parser.add_subparsers(dest="command", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--prompt-version", required=True)
    replay.add_argument("--since", required=True)
    replay.add_argument("--user", required=True)
    rosi_replay = sub.add_parser("rosi-replay")
    rosi_replay.add_argument("--user", required=True)
    args = parser.parse_args()
    if args.command in ("replay", "rosi-replay"):
        asyncio.run(_main_async(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
