#!/usr/bin/env python3
"""Observability sanity: group turn_audit_events and tool_calls by bot_id.

S2a added per-bot split panes; this script is the code-level check that the
panels still render correctly for a third (staging-only) bot. It connects to
DATABASE_URL with statement_cache_size=0 (Supabase pooler is transaction-mode
on port 6543, see S1 lesson #3) and prints two tables: events grouped by
bot_id and tool_calls grouped by bot_id, both windowed over a recent
interval.

Expected bot_ids after first prod deploy: mediator, coach, tante_rosi, superpom.

Usage:
    python scripts/check_per_bot_panels.py --help
    python scripts/check_per_bot_panels.py --hours 24
    python scripts/check_per_bot_panels.py --bot-id mediator --hours 6
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Group turn_audit_events and tool_calls by bot_id for per-bot panel sanity.",
    )
    parser.add_argument("--hours", type=int, default=24, help="window in hours (default: 24)")
    parser.add_argument("--bot-id", type=str, default=None, help="optional bot_id filter")
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.environ.get("DATABASE_URL"),
        help="DATABASE_URL override (default: $DATABASE_URL)",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if not args.database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2

    import asyncpg  # local import so --help works without the dep installed

    # Supabase pooler is transaction-mode on port 6543 — see S1 lesson #3.
    pool = await asyncpg.create_pool(args.database_url, statement_cache_size=0)
    try:
        events_bot_filter = "AND metadata->>'bot_id' = $2" if args.bot_id else ""
        tool_calls_bot_filter = "AND bt.bot_id = $2" if args.bot_id else ""
        params: list[Any] = [args.hours]
        if args.bot_id:
            params.append(args.bot_id)

        # turn_audit_events: no top-level bot_id column; extract from metadata JSONB.
        events = await pool.fetch(
            f"""
            SELECT COALESCE(metadata->>'bot_id', '<null>') AS bot_id, COUNT(*) AS event_count
            FROM turn_audit_events
            WHERE occurred_at >= now() - ($1 || ' hours')::interval
              {events_bot_filter}
            GROUP BY 1
            ORDER BY 1
            """,
            *params,
        )
        # tool_calls: no top-level bot_id column; JOIN through bot_turns.
        tool_calls = await pool.fetch(
            f"""
            SELECT COALESCE(bt.bot_id, '<null>') AS bot_id, COUNT(*) AS call_count
            FROM tool_calls tc
            JOIN bot_turns bt ON bt.id = tc.turn_id
            WHERE tc.called_at >= now() - ($1 || ' hours')::interval
              {tool_calls_bot_filter}
            GROUP BY 1
            ORDER BY 1
            """,
            *params,
        )
    finally:
        await pool.close()

    print(f"turn_audit_events (last {args.hours}h) by bot_id:")
    for row in events:
        print(f"  {row['bot_id']:<24} {row['event_count']}")
    print()
    print(f"tool_calls (last {args.hours}h) by bot_id:")
    for row in tool_calls:
        print(f"  {row['bot_id']:<24} {row['call_count']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())