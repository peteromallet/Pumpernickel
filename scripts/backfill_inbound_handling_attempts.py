#!/usr/bin/env python3
"""Backfill ``inbound_handling_attempts`` from existing ``messages`` rows.

Project C, C2.  Idempotent one-shot backfill that walks every inbound
``messages`` row lacking a ledger entry and inserts a single attempt row
per message:

* ``attempt_number = messages.processing_attempts`` (or 1 if NULL/0)
* ``created_by = 'backfill'``
* ``failure_class = 'unknown_legacy'`` (for rows that have an existing
  ``messages.failure_class`` we copy it verbatim — the legacy three-class
  taxonomy is a subset of the wider C3 enum)
* ``status = 'active'`` if ``processing_state IN ('raw','processing')``,
  else ``'failed'`` (anything else is treated as terminal-failed for the
  ledger; the read path is unchanged and still uses
  ``messages.processing_state`` directly)

The script is safe to re-run: it joins ``messages`` to
``inbound_handling_attempts`` with ``NOT EXISTS`` and only inserts where
no row exists.  Emits ``backfill_inbound_handling_attempts{status}``
counters via :mod:`app.services.metrics` for observability.

Usage
-----
    python scripts/backfill_inbound_handling_attempts.py
    python scripts/backfill_inbound_handling_attempts.py --database-url ...
    python scripts/backfill_inbound_handling_attempts.py --dry-run
    python scripts/backfill_inbound_handling_attempts.py --batch-size 500
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

logger = logging.getLogger("backfill_inbound_handling_attempts")


# ── core ─────────────────────────────────────────────────────────────────────


_SELECT_CANDIDATES_SQL = """
SELECT m.id, m.bot_id, m.topic_id,
       COALESCE(m.processing_attempts, 1) AS processing_attempts,
       m.processing_state,
       m.failure_class       AS msg_failure_class,
       m.processing_error    AS msg_processing_error,
       m.handled_by_turn_id  AS handled_by_turn_id,
       m.sent_at             AS sent_at,
       m.handled_at          AS handled_at
FROM messages m
WHERE m.direction = 'inbound'
  AND NOT EXISTS (
      SELECT 1 FROM inbound_handling_attempts a
      WHERE a.message_id = m.id
  )
ORDER BY m.sent_at NULLS LAST
LIMIT $1
"""


_INSERT_ATTEMPT_SQL = """
INSERT INTO inbound_handling_attempts
    (message_id, bot_turn_id, bot_id, topic_id, attempt_number,
     status, failure_class, failure_reason, started_at, completed_at,
     created_by)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'backfill')
ON CONFLICT DO NOTHING
"""


def _derive_status(processing_state: str | None) -> str:
    """Map messages.processing_state to a ledger status for backfill."""
    if processing_state in {"raw", "processing"}:
        return "active"
    return "failed"


def _derive_failure_class(processing_state: str | None, msg_failure_class: str | None) -> str | None:
    """Carry forward the messages-level failure_class or stamp 'unknown_legacy'.

    Terminal-success rows (processed/expired/withheld) get NULL; failed
    rows without an explicit class get 'unknown_legacy' so dashboards
    distinguish them from C3-classified failures.
    """
    if processing_state in {"processed", "expired", "withheld"}:
        return None
    if msg_failure_class:
        return msg_failure_class
    if processing_state == "failed":
        return "unknown_legacy"
    return None


async def backfill_once(
    pool: Any,
    *,
    batch_size: int = 1000,
    dry_run: bool = False,
) -> dict[str, int]:
    """Backfill one batch.  Returns counts keyed by ledger status."""
    counts = {"active": 0, "failed": 0, "skipped": 0}
    async with pool.acquire() as conn:
        rows = await conn.fetch(_SELECT_CANDIDATES_SQL, batch_size)
        if not rows:
            return counts
        for row in rows:
            status = _derive_status(row["processing_state"])
            failure_class = _derive_failure_class(
                row["processing_state"], row["msg_failure_class"]
            )
            completed_at = None if status == "active" else row["handled_at"]
            if dry_run:
                counts["skipped"] += 1
                continue
            try:
                await conn.execute(
                    _INSERT_ATTEMPT_SQL,
                    row["id"],
                    row["handled_by_turn_id"],
                    row["bot_id"] or "unknown",
                    row["topic_id"],
                    int(row["processing_attempts"]),
                    status,
                    failure_class,
                    row["msg_processing_error"],
                    row["sent_at"],
                    completed_at,
                )
                counts[status] = counts.get(status, 0) + 1
            except Exception:
                logger.exception(
                    "backfill insert failed for message_id=%s", row["id"]
                )
                counts["skipped"] += 1
    return counts


async def backfill_loop(
    pool: Any,
    *,
    batch_size: int,
    dry_run: bool,
    max_batches: int | None,
) -> dict[str, int]:
    totals = {"active": 0, "failed": 0, "skipped": 0}
    batches = 0
    while True:
        batch = await backfill_once(pool, batch_size=batch_size, dry_run=dry_run)
        any_progress = any(batch.values())
        for k, v in batch.items():
            totals[k] = totals.get(k, 0) + v
        batches += 1
        logger.info(
            "backfill batch=%d active=%d failed=%d skipped=%d",
            batches,
            batch.get("active", 0),
            batch.get("failed", 0),
            batch.get("skipped", 0),
        )
        if not any_progress:
            break
        if max_batches is not None and batches >= max_batches:
            break
    return totals


async def _connect(database_url: str, schema: str | None) -> Any:
    import asyncpg

    async def _init(conn: Any) -> None:
        if schema and schema != "public":
            await conn.execute(f"SET search_path TO {schema}, public")

    return await asyncpg.create_pool(
        database_url, statement_cache_size=0, init=_init
    )


def _emit_metrics(totals: dict[str, int]) -> None:
    """Emit one counter per ledger-status bucket.  Best-effort; the app
    metrics module is a structured-log shim and never raises."""
    try:
        from app.services import metrics

        for status, count in totals.items():
            if count <= 0:
                continue
            metrics.incr(
                "backfill_inbound_handling_attempts",
                value=count,
                status=status,
            )
    except Exception:
        logger.exception("metrics emission failed (non-fatal)")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill the mediator.inbound_handling_attempts ledger from "
            "existing messages rows.  Idempotent; safe to re-run."
        )
    )
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL"),
        help="DATABASE_URL override (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--database-schema",
        default=os.environ.get("DATABASE_SCHEMA", "public"),
        help="search_path schema (default: $DATABASE_SCHEMA or 'public')",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-batches", type=int, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Walk candidates and report counts without inserting.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if not args.database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    pool = await _connect(args.database_url, args.database_schema)
    try:
        totals = await backfill_loop(
            pool,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            max_batches=args.max_batches,
        )
    finally:
        await pool.close()
    logger.info(
        "backfill done: total active=%d failed=%d skipped=%d dry_run=%s",
        totals.get("active", 0),
        totals.get("failed", 0),
        totals.get("skipped", 0),
        args.dry_run,
    )
    if not args.dry_run:
        _emit_metrics(totals)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
