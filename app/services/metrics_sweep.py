"""Periodic metrics sweeper (Project A3 work item 6).

Emits two metrics on a 5-minute cadence:

* ``terminal_rows_without_outbound{bot}`` — gauge counting inbound rows
  whose ``processing_state`` reached a terminal value (``processed``,
  ``expired``) on their most recent turn but for which the linked
  ``bot_turns`` row has no ``final_output_message_id``.  This catches the
  silent-failure mode where a turn declared success but never produced
  outbound text.

* ``attempt_age_seconds{bot}`` — periodic histogram observations of the
  wall-clock latency between ``processing_started_at`` and
  ``handled_at`` for inbound rows handled in the last 5 minutes.  Three
  samples are emitted per (bot) per sweep — p50, p95, p99 — because the
  metrics layer (log-based) does not natively support bucket histograms.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.services import metrics

logger = logging.getLogger(__name__)

DEFAULT_SWEEP_SECONDS = 300  # 5 minutes


_TERMINAL_ROWS_WITHOUT_OUTBOUND_SQL = """
SELECT m.bot_id, COUNT(*) AS n
FROM messages m
LEFT JOIN bot_turns bt ON bt.id = m.handled_by_turn_id
WHERE m.direction = 'inbound'
  AND m.processing_state IN ('processed', 'expired')
  AND m.handling_result = 'replied'
  AND m.handled_at >= now() - interval '1 hour'
  AND m.handled_by_turn_id IS NOT NULL
  AND (bt.final_output_message_id IS NULL)
  AND m.bot_id IS NOT NULL
GROUP BY m.bot_id
"""

# Use percentile_cont so we get clean p50/p95/p99 from whatever number of
# samples landed in the last 5 minutes.  No bucketing needed: the
# downstream log-shipper can aggregate further if desired.
_ATTEMPT_AGE_SQL = """
SELECT
    bot_id,
    percentile_cont(0.50) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (handled_at - processing_started_at))
    ) AS p50,
    percentile_cont(0.95) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (handled_at - processing_started_at))
    ) AS p95,
    percentile_cont(0.99) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (handled_at - processing_started_at))
    ) AS p99,
    COUNT(*) AS n
FROM messages
WHERE direction = 'inbound'
  AND processing_started_at IS NOT NULL
  AND handled_at IS NOT NULL
  AND handled_at >= now() - interval '5 minutes'
  AND bot_id IS NOT NULL
GROUP BY bot_id
"""


async def sweep_once(pool: Any) -> None:
    """Run one sweep tick.  Catches per-query exceptions so a single failed
    query does not abort the entire sweep."""
    try:
        rows = await pool.fetch(_TERMINAL_ROWS_WITHOUT_OUTBOUND_SQL)
        for row in rows:
            metrics.gauge(
                "terminal_rows_without_outbound",
                float(row["n"]),
                bot=row["bot_id"],
            )
    except Exception:
        logger.exception("metrics_sweep: terminal_rows_without_outbound failed")

    try:
        rows = await pool.fetch(_ATTEMPT_AGE_SQL)
        for row in rows:
            bot_id = row["bot_id"]
            for quantile_label, key in (("p50", "p50"), ("p95", "p95"), ("p99", "p99")):
                val = row[key]
                if val is None:
                    continue
                metrics.observe(
                    "attempt_age_seconds",
                    float(val),
                    bot=bot_id,
                    quantile=quantile_label,
                )
    except Exception:
        logger.exception("metrics_sweep: attempt_age_seconds failed")


async def run_metrics_sweep_forever(
    pool: Any,
    *,
    interval_seconds: float = DEFAULT_SWEEP_SECONDS,
) -> None:
    """Background task: run :func:`sweep_once` on a fixed cadence forever."""
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            await sweep_once(pool)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("metrics_sweep tick failed")
