#!/usr/bin/env python3
"""Synthetic prober — Project A3, work item 5 (revised plan).

Injects a synthetic ``inbound`` row for each known bot and asserts that
the row reaches a terminal-success state within a configurable SLO
(default 60s).  Emits one structured log line per bot probed and exits
non-zero if any bot failed.

The script is designed to be invoked by an external cron (Railway
scheduled job recommended — see ``docs/observability.md``).  It connects
with ``statement_cache_size=0`` so it works behind the Supabase pooler
(transaction-mode on port 6543).

Usage
-----
    python scripts/synthetic_prober.py
    python scripts/synthetic_prober.py --slo-seconds 90
    python scripts/synthetic_prober.py --bots mediator,hector
    python scripts/synthetic_prober.py --database-url postgres://...

Exit codes
----------
* 0 — every probed bot reached terminal success within the SLO.
* 1 — at least one probe failed to reach terminal success in time.
* 2 — argument / connection error.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("synthetic_prober")

DEFAULT_BOTS = ("mediator", "hector", "coach", "tante_rosi")
DEFAULT_SLO_SECONDS = 60.0
DEFAULT_POLL_INTERVAL = 1.0
SYNTHETIC_PROBE_CONTENT = "__synthetic_probe__"


# ── data classes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProbeResult:
    bot: str
    message_id: uuid.UUID
    reached_terminal: bool
    latency_seconds: float
    final_state: str | None
    handling_result: str | None


# ── DB helpers (extracted for unit-testability) ──────────────────────────────


async def insert_probe_message(
    pool: Any,
    *,
    bot_id: str,
    sender_id: uuid.UUID,
    topic_id: uuid.UUID,
    now: datetime,
) -> uuid.UUID:
    """Insert a synthetic inbound row and return its id."""
    row = await pool.fetchrow(
        """
        INSERT INTO messages (
            direction, sender_id, content, processing_state, sent_at,
            bot_id, topic_id
        )
        VALUES ('inbound', $1, $2, 'raw', $3, $4, $5)
        RETURNING id
        """,
        sender_id,
        SYNTHETIC_PROBE_CONTENT,
        now,
        bot_id,
        topic_id,
    )
    return row["id"]


async def fetch_message_state(
    pool: Any, message_id: uuid.UUID
) -> tuple[str | None, str | None]:
    """Return ``(processing_state, handling_result)`` for *message_id*."""
    row = await pool.fetchrow(
        """
        SELECT processing_state, handling_result
        FROM messages
        WHERE id = $1::uuid
        """,
        message_id,
    )
    if row is None:
        return (None, None)
    return (row["processing_state"], row["handling_result"])


async def resolve_synthetic_scope(
    pool: Any, bot_id: str
) -> tuple[uuid.UUID, uuid.UUID] | None:
    """Resolve a (sender_id, topic_id) pair to use for the synthetic probe.

    Strategy: pick the most-recent inbound message for ``bot_id`` and reuse
    its (sender_id, topic_id) so we don't have to invent a user/topic.
    Returns ``None`` when no inbound history exists for this bot — the
    caller treats that as a non-probable bot.
    """
    row = await pool.fetchrow(
        """
        SELECT sender_id, topic_id
        FROM messages
        WHERE direction = 'inbound'
          AND bot_id = $1
          AND sender_id IS NOT NULL
          AND topic_id IS NOT NULL
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        bot_id,
    )
    if row is None:
        return None
    return (row["sender_id"], row["topic_id"])


# ── probe driver ─────────────────────────────────────────────────────────────


def _is_terminal_success(
    processing_state: str | None, handling_result: str | None
) -> bool:
    """A probe is considered successful when the row reached a terminal
    state via a real reply path (not silent / failed / expired)."""
    if processing_state not in {"processed", "expired"}:
        return False
    return handling_result == "replied"


async def probe_bot(
    pool: Any,
    bot_id: str,
    *,
    slo_seconds: float = DEFAULT_SLO_SECONDS,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    now_fn: Any = None,
    sleep_fn: Any = None,
) -> ProbeResult:
    """Probe a single bot and return a :class:`ProbeResult`.

    The function inserts one synthetic inbound row, then polls
    ``messages`` until either:

    * the row reaches a terminal-success state (``processed`` with
      ``handling_result='replied'``), or
    * ``slo_seconds`` elapses.

    The injected (sender_id, topic_id) are reused from the most-recent
    inbound row for *bot_id*; if no such row exists the probe is reported
    as failed with ``reached_terminal=False`` and ``final_state=None``.
    """
    now_fn = now_fn or (lambda: datetime.now(UTC))
    sleep_fn = sleep_fn or asyncio.sleep

    scope = await resolve_synthetic_scope(pool, bot_id)
    started = time.monotonic()
    if scope is None:
        return ProbeResult(
            bot=bot_id,
            message_id=uuid.UUID(int=0),
            reached_terminal=False,
            latency_seconds=0.0,
            final_state=None,
            handling_result=None,
        )
    sender_id, topic_id = scope

    message_id = await insert_probe_message(
        pool,
        bot_id=bot_id,
        sender_id=sender_id,
        topic_id=topic_id,
        now=now_fn(),
    )

    final_state: str | None = None
    handling_result: str | None = None
    while True:
        elapsed = time.monotonic() - started
        final_state, handling_result = await fetch_message_state(pool, message_id)
        if _is_terminal_success(final_state, handling_result):
            return ProbeResult(
                bot=bot_id,
                message_id=message_id,
                reached_terminal=True,
                latency_seconds=elapsed,
                final_state=final_state,
                handling_result=handling_result,
            )
        if elapsed >= slo_seconds:
            return ProbeResult(
                bot=bot_id,
                message_id=message_id,
                reached_terminal=False,
                latency_seconds=elapsed,
                final_state=final_state,
                handling_result=handling_result,
            )
        await sleep_fn(poll_interval)


async def run_probes(
    pool: Any,
    bots: tuple[str, ...],
    *,
    slo_seconds: float,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
) -> list[ProbeResult]:
    """Probe each bot in *bots* concurrently and return their results."""
    coros = [
        probe_bot(
            pool,
            bot_id,
            slo_seconds=slo_seconds,
            poll_interval=poll_interval,
        )
        for bot_id in bots
    ]
    return list(await asyncio.gather(*coros))


def emit_results(results: list[ProbeResult]) -> None:
    """Emit one structured log line per probe result."""
    for r in results:
        logger.info(
            "synthetic_probe bot=%s latency_seconds=%.3f reached_terminal=%s",
            r.bot,
            r.latency_seconds,
            r.reached_terminal,
            extra={
                "metric": "synthetic_probe",
                "metric_kind": "event",
                "labels": {
                    "bot": r.bot,
                    "reached_terminal": str(r.reached_terminal),
                    "final_state": r.final_state or "",
                    "handling_result": r.handling_result or "",
                },
                "value": r.latency_seconds,
            },
        )


# ── CLI entry-point ──────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Synthetic prober: assert each known bot replies within an SLO."
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="Postgres URL (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--bots",
        default=",".join(DEFAULT_BOTS),
        help=f"Comma-separated bot ids to probe (default: {','.join(DEFAULT_BOTS)})",
    )
    parser.add_argument(
        "--slo-seconds",
        type=float,
        default=DEFAULT_SLO_SECONDS,
        help=f"Per-bot SLO in seconds (default: {DEFAULT_SLO_SECONDS})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL,
        help="Polling interval while waiting for terminal state (seconds).",
    )
    return parser.parse_args(argv)


async def _async_main(args: argparse.Namespace) -> int:
    import asyncpg  # local import so --help works without the dep installed

    if not args.database_url:
        logger.error("DATABASE_URL not set and --database-url not given")
        return 2

    pool = await asyncpg.create_pool(args.database_url, statement_cache_size=0)
    try:
        bots = tuple(b.strip() for b in args.bots.split(",") if b.strip())
        results = await run_probes(
            pool,
            bots,
            slo_seconds=args.slo_seconds,
            poll_interval=args.poll_interval,
        )
    finally:
        await pool.close()

    emit_results(results)
    failed = [r for r in results if not r.reached_terminal]
    if failed:
        logger.error(
            "synthetic prober failed bots=%s",
            ",".join(r.bot for r in failed),
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s:%(name)s: %(message)s",
        stream=sys.stdout,
    )
    args = _parse_args(argv)
    return asyncio.run(_async_main(args))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
