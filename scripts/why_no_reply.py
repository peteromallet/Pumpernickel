#!/usr/bin/env python3
"""Diagnostic: why didn't the bot reply to <message_id>?

Project A3 (work item 5) of the agent-reliability-cleanup-revised plan.  Per
SD-004 this script is the schema acceptance test for the recovery-v2
lifecycle columns added by migration 0042 — the answer to "why didn't the
bot reply?" must fall out of the existing tables trivially.  If you find
yourself wanting a field that doesn't exist, the schema is wrong, not the
script.

Identifiers accepted
--------------------
``<message_id>`` may be either:

* a DB ``messages.id`` UUID, or
* a transport-side message id (Discord snowflake / WhatsApp message id),
  stored in ``messages.whatsapp_message_id`` (despite the column name,
  this column holds the provider-side id for all transports — see
  ``app/services/messaging.py``).

The script auto-detects which of the two was supplied.

Output
------
Human-readable sections (NOT JSON):

* Inbound row state
* Current/last attempt (linked bot_turns rows)
* Tool calls for the most recent linked turn
* Final outbound id, if any
* Retry eligibility (yes/no, with reason)
* Next retry time
* Recommended action

Usage
-----
    python scripts/why_no_reply.py <message_id>
    python scripts/why_no_reply.py --database-url postgres://... <message_id>

The script connects with ``statement_cache_size=0`` (Supabase pooler is
transaction-mode on port 6543 — see scripts/check_per_bot_panels.py).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID


# ── identifier detection ─────────────────────────────────────────────────────


def _looks_like_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _looks_like_transport_id(value: str) -> bool:
    """Discord snowflakes are decimal strings; WhatsApp ids are also strings.

    We treat anything that isn't a UUID as a transport-side id and look it
    up in ``whatsapp_message_id`` (the cross-transport provider-id column).
    """
    return bool(value) and not _looks_like_uuid(value)


# ── DB connection ────────────────────────────────────────────────────────────


async def _connect(database_url: str, schema: str | None) -> Any:
    import asyncpg  # local import so --help works without the dep installed

    pool = await asyncpg.create_pool(database_url, statement_cache_size=0)
    if schema and schema != "public":
        # Set search_path on each acquired connection.  We just stash the
        # schema; SchemaPool is not needed for one-shot diagnostics.
        async def _set_path(conn: Any) -> None:
            await conn.execute(f"SET search_path TO {schema}, public")

        # asyncpg pool doesn't expose post-acquire hooks via create_pool
        # arguments in older versions, so do it ad-hoc on first acquire.
        async with pool.acquire() as conn:
            await _set_path(conn)
    return pool


# ── lookups ──────────────────────────────────────────────────────────────────


async def _fetch_message(pool: Any, identifier: str) -> Any | None:
    """Return the messages row for *identifier*, or None.

    Tries UUID lookup first when *identifier* parses as one, otherwise
    looks up by ``whatsapp_message_id`` (the provider-side id column).
    """
    if _looks_like_uuid(identifier):
        row = await pool.fetchrow(
            """
            SELECT
                id, direction, sender_id, recipient_id, content,
                sent_at, in_reply_to, processing_state, charge,
                whatsapp_message_id, deleted_at,
                bot_id, topic_id,
                handled_at, handled_by_turn_id, handling_result,
                processing_started_at, processing_error, processing_attempts,
                next_retry_at, failure_class
            FROM messages
            WHERE id = $1::uuid
            """,
            identifier,
        )
        if row is not None:
            return row
        # Fall through to transport-id lookup in case the UUID is actually a
        # transport id that happens to look UUID-shaped (unlikely but cheap).

    row = await pool.fetchrow(
        """
        SELECT
            id, direction, sender_id, recipient_id, content,
            sent_at, in_reply_to, processing_state, charge,
            whatsapp_message_id, deleted_at,
            bot_id, topic_id,
            handled_at, handled_by_turn_id, handling_result,
            processing_started_at, processing_error, processing_attempts,
            next_retry_at, failure_class
        FROM messages
        WHERE whatsapp_message_id = $1
        ORDER BY sent_at DESC
        LIMIT 1
        """,
        identifier,
    )
    return row


async def _fetch_linked_turns(pool: Any, message_id: UUID) -> list[Any]:
    """Return bot_turns rows that reference this message via triggering_message_ids
    OR via handled_by_turn_id on the messages row.

    Sorted oldest-first so the last element is the most recent attempt.
    """
    return await pool.fetch(
        """
        SELECT
            id, bot_id, topic_id, triggered_by_message_id,
            triggering_message_ids, user_in_context,
            started_at, completed_at, model_version,
            tool_call_count, duration_ms, failure_reason,
            final_output_message_id
        FROM bot_turns
        WHERE triggering_message_ids @> ARRAY[$1::uuid]
           OR triggered_by_message_id = $1::uuid
        ORDER BY started_at ASC
        """,
        message_id,
    )


async def _fetch_tool_calls(pool: Any, turn_id: UUID) -> list[Any]:
    return await pool.fetch(
        """
        SELECT id, tool_name, arguments, result, called_at, duration_ms
        FROM tool_calls
        WHERE turn_id = $1::uuid
        ORDER BY called_at ASC
        """,
        turn_id,
    )


async def _fetch_outbound(pool: Any, message_id: UUID) -> Any | None:
    return await pool.fetchrow(
        """
        SELECT id, whatsapp_message_id, sent_at, direction, content
        FROM messages
        WHERE id = $1::uuid
        """,
        message_id,
    )


async def _fetch_ledger_attempts(pool: Any, message_id: UUID) -> list[Any]:
    """Return inbound_handling_attempts rows for *message_id*, oldest-first.

    Project C, C2.  The ledger is a dual-write target gated by
    ``ledger_dual_write_enabled``; on stale envs or pre-ledger rows this
    table may not exist or may have no entries.  Returns ``[]`` in both
    cases (the caller renders an explicit pre-ledger marker).
    """
    try:
        return await pool.fetch(
            """
            SELECT attempt_number, status, failure_class, failure_reason,
                   started_at, completed_at, next_retry_at, created_by,
                   bot_turn_id
            FROM inbound_handling_attempts
            WHERE message_id = $1::uuid
            ORDER BY attempt_number ASC, started_at ASC
            """,
            message_id,
        )
    except Exception:
        # asyncpg.UndefinedTableError or any transient — treat as "no ledger".
        return []


async def _fetch_coalescer_known_bots(pool: Any) -> set[str]:
    """Best-effort: which bots have any recent inbound activity, used as a
    proxy for "is this bot known to the system".  This is intentionally
    cheap and not authoritative — operators reading this script know to
    check the in-memory CoalescerRegistry on the running process if a row
    is in failed with no coalescer.
    """
    rows = await pool.fetch(
        """
        SELECT DISTINCT bot_id FROM messages WHERE bot_id IS NOT NULL
        """
    )
    return {r["bot_id"] for r in rows if r["bot_id"]}


# ── eligibility / recommendation logic ───────────────────────────────────────


# These limits mirror app/config.Settings defaults; the script is a
# diagnostic, so it reads sensible defaults rather than importing the
# pydantic settings object (which would require the full app env).
DEFAULT_MAX_RETRIES = 3
STALE_PROCESSING_SECONDS = 300  # matches recover_stale_processing default
TERMINAL_STATES = frozenset({"processed", "expired", "withheld"})
TERMINAL_FAILURE_CLASSES = frozenset({"terminal_post_send", "infra_bug"})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _coerce_aware(value: Any) -> datetime | None:
    """Coerce a possibly-naive datetime to UTC-aware.  ``messages.sent_at``
    and friends are ``timestamptz`` so asyncpg already hands back
    UTC-aware values; this is defence in depth for the fake-pool tests.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    return None


def evaluate_retry_eligibility(
    msg: dict[str, Any],
    *,
    now: datetime | None = None,
    max_retries: int = DEFAULT_MAX_RETRIES,
    known_bot_ids: set[str] | None = None,
) -> tuple[bool, str]:
    """Pure logic for the retry-eligibility decision.

    Returns ``(eligible, reason)``.  *eligible* is True only when the
    sweeper (``recover_retryable_failed`` / the raw-message recovery path
    in ``_recover_v2_inbound``) would pick this row up on its next tick.

    Mirrors the WHERE clauses in app/services/inbound_queue.py and
    app/services/recovery.py; this function is the schema acceptance
    test promised by SD-004.
    """
    now = now or _utcnow()
    state = msg.get("processing_state")
    failure_class = msg.get("failure_class")
    next_retry_at = _coerce_aware(msg.get("next_retry_at"))
    attempts = int(msg.get("processing_attempts") or 0)
    bot_id = msg.get("bot_id")

    if state in TERMINAL_STATES:
        if state == "processed":
            handling_result = msg.get("handling_result")
            return False, f"already terminal (processed/{handling_result})"
        return False, f"already terminal ({state})"

    if failure_class in TERMINAL_FAILURE_CLASSES:
        if failure_class == "terminal_post_send":
            return False, "terminal_post_send (reply already sent; not retryable)"
        return False, "infra_bug (manual intervention required; auto-recovery disabled)"

    if known_bot_ids is not None and bot_id and bot_id not in known_bot_ids:
        return False, f"no coalescer registered for bot_id={bot_id!r} (will stay in failed)"

    if state == "failed":
        if attempts >= max_retries:
            return False, f"retry cap reached ({attempts}/{max_retries})"
        if next_retry_at is not None and next_retry_at > now:
            return False, f"next_retry_at in the future ({next_retry_at.isoformat()})"
        return True, "retryable_pre_send within backoff window"

    if state == "raw":
        # Raw rows are eligible unless next_retry_at gates them.
        if next_retry_at is not None and next_retry_at > now:
            return False, f"next_retry_at in the future ({next_retry_at.isoformat()})"
        return True, "raw and eligible for claim"

    if state == "processing":
        started = _coerce_aware(msg.get("processing_started_at"))
        if started is None:
            return False, "in-flight (no processing_started_at recorded)"
        age = now - started
        if age > timedelta(seconds=STALE_PROCESSING_SECONDS):
            return True, (
                f"stale claim ({age.total_seconds():.0f}s old > "
                f"{STALE_PROCESSING_SECONDS}s; recovery will reset to raw)"
            )
        return False, f"actively processing ({age.total_seconds():.0f}s old)"

    if state == "deferred":
        return True, "deferred; eligible for re-claim"

    return False, f"unhandled processing_state={state!r}"


def recommended_action(
    msg: dict[str, Any],
    *,
    eligible: bool,
    eligible_reason: str,
    turns: list[dict[str, Any]],
    outbound_id: Any,
    now: datetime | None = None,
    known_bot_ids: set[str] | None = None,
) -> str:
    """Operator-facing one-liner.  Pure logic; tested directly."""
    now = now or _utcnow()
    state = msg.get("processing_state")
    failure_class = msg.get("failure_class")
    handling_result = msg.get("handling_result")

    if outbound_id is not None and state == "processed" and handling_result == "replied":
        return "no action — bot already replied (see final_output_message_id)"

    if state == "processed":
        return f"no action — terminal (handling_result={handling_result!r})"

    if state == "expired":
        return "no action — row expired past retention"

    if failure_class == "terminal_post_send":
        return "no action — reply already sent; do NOT manually requeue"

    if failure_class == "infra_bug":
        return "manual investigation required (infra_bug class; auto-recovery off)"

    bot_id = msg.get("bot_id")
    if known_bot_ids is not None and bot_id and bot_id not in known_bot_ids:
        return f"register/restart coalescer for bot_id={bot_id!r}; recovery cannot reach this row"

    if state == "processing":
        started = _coerce_aware(msg.get("processing_started_at"))
        if started is not None and (now - started) > timedelta(seconds=STALE_PROCESSING_SECONDS):
            return "stale claim; recovery sweep will reset to raw (or investigate worker)"
        return "in-flight; wait for current turn to complete"

    if eligible:
        next_retry = _coerce_aware(msg.get("next_retry_at"))
        if next_retry is not None and next_retry > now:
            return f"wait for retry at {next_retry.isoformat()}"
        return "wait for next recovery tick (eligible now)"

    # Not eligible, not terminal — explain.
    if "retry cap" in eligible_reason:
        return "retry cap reached; manual requeue via SQL or investigate root cause"
    if "next_retry_at" in eligible_reason:
        next_retry = _coerce_aware(msg.get("next_retry_at"))
        if next_retry is not None:
            return f"wait for retry at {next_retry.isoformat()}"

    return f"investigate manually: {eligible_reason}"


# ── rendering ────────────────────────────────────────────────────────────────


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    return str(value)


def _row_to_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    # asyncpg.Record supports dict()
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {k: row[k] for k in row.keys()}  # type: ignore[attr-defined]


def render(
    *,
    identifier: str,
    msg: dict[str, Any],
    turns: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    final_outbound: dict[str, Any] | None,
    eligible: bool,
    eligible_reason: str,
    action: str,
    ledger_attempts: list[dict[str, Any]] | None = None,
) -> str:
    lines: list[str] = []
    lines.append(f"=== why_no_reply: {identifier} ===")
    lines.append("")

    # Inbound row
    lines.append("-- Inbound row state --")
    if not msg:
        lines.append(f"  no messages row found for identifier={identifier!r}")
        return "\n".join(lines)
    lines.append(f"  messages.id            = {_fmt(msg.get('id'))}")
    lines.append(f"  direction              = {_fmt(msg.get('direction'))}")
    lines.append(f"  whatsapp_message_id    = {_fmt(msg.get('whatsapp_message_id'))}")
    lines.append(f"  bot_id                 = {_fmt(msg.get('bot_id'))}")
    lines.append(f"  topic_id               = {_fmt(msg.get('topic_id'))}")
    lines.append(f"  sender_id              = {_fmt(msg.get('sender_id'))}")
    lines.append(f"  sent_at                = {_fmt(msg.get('sent_at'))}")
    lines.append(f"  processing_state       = {_fmt(msg.get('processing_state'))}")
    lines.append(f"  processing_attempts    = {_fmt(msg.get('processing_attempts'))}")
    lines.append(f"  processing_started_at  = {_fmt(msg.get('processing_started_at'))}")
    lines.append(f"  processing_error       = {_fmt(msg.get('processing_error'))}")
    lines.append(f"  handling_result        = {_fmt(msg.get('handling_result'))}")
    lines.append(f"  handled_at             = {_fmt(msg.get('handled_at'))}")
    lines.append(f"  handled_by_turn_id     = {_fmt(msg.get('handled_by_turn_id'))}")
    lines.append(f"  failure_class          = {_fmt(msg.get('failure_class'))}")
    lines.append(f"  next_retry_at          = {_fmt(msg.get('next_retry_at'))}")
    lines.append("")

    # Turns
    lines.append("-- Current/last attempt (bot_turns) --")
    if not turns:
        lines.append("  no bot_turns rows linked to this message yet")
    else:
        for idx, t in enumerate(turns):
            tag = "(latest)" if idx == len(turns) - 1 else ""
            lines.append(f"  turn[{idx}] {tag}")
            lines.append(f"    id                       = {_fmt(t.get('id'))}")
            lines.append(f"    bot_id                   = {_fmt(t.get('bot_id'))}")
            lines.append(f"    started_at               = {_fmt(t.get('started_at'))}")
            lines.append(f"    completed_at             = {_fmt(t.get('completed_at'))}")
            lines.append(f"    failure_reason           = {_fmt(t.get('failure_reason'))}")
            lines.append(f"    final_output_message_id  = {_fmt(t.get('final_output_message_id'))}")
            lines.append(f"    tool_call_count          = {_fmt(t.get('tool_call_count'))}")
            lines.append(f"    triggering_message_ids   = {_fmt(t.get('triggering_message_ids'))}")
    lines.append("")

    # Tool calls (latest turn only)
    lines.append("-- Tool calls (most recent turn) --")
    if not tool_calls:
        lines.append("  no tool_calls rows for the most recent turn")
    else:
        for tc in tool_calls:
            result = tc.get("result")
            err = None
            if isinstance(result, dict):
                err = result.get("error") or result.get("error_detail")
            lines.append(
                f"  {_fmt(tc.get('called_at'))}  "
                f"{_fmt(tc.get('tool_name')):<32}  "
                f"duration_ms={_fmt(tc.get('duration_ms'))}  "
                f"error={_fmt(err)}"
            )
    lines.append("")

    # Final outbound
    lines.append("-- Final outbound --")
    if final_outbound is None:
        lines.append("  no final outbound message id (no reply was sent)")
    else:
        lines.append(f"  outbound messages.id     = {_fmt(final_outbound.get('id'))}")
        lines.append(f"  outbound whatsapp_msg_id = {_fmt(final_outbound.get('whatsapp_message_id'))}")
        lines.append(f"  outbound sent_at         = {_fmt(final_outbound.get('sent_at'))}")
    lines.append("")

    # Retry eligibility
    lines.append("-- Retry eligibility --")
    lines.append(f"  eligible       = {'yes' if eligible else 'no'}")
    lines.append(f"  reason         = {eligible_reason}")
    lines.append(f"  next_retry_at  = {_fmt(msg.get('next_retry_at'))}")
    lines.append("")

    # Ledger attempt history (Project C, C2).  Additive — the existing
    # schema-based output above is unchanged.
    lines.append("-- Ledger attempt history --")
    if not ledger_attempts:
        lines.append(
            "  no ledger entries (pre-ledger row, see messages.failure_class "
            "for current state)"
        )
    else:
        lines.append(
            f"  {'attempt':>7}  {'status':<10}  {'failure_class':<28}  "
            f"{'started_at':<32}  {'completed_at':<32}  {'created_by':<10}"
        )
        for entry in ledger_attempts:
            lines.append(
                f"  {entry.get('attempt_number', '?'):>7}  "
                f"{_fmt(entry.get('status')):<10}  "
                f"{_fmt(entry.get('failure_class')):<28}  "
                f"{_fmt(entry.get('started_at')):<32}  "
                f"{_fmt(entry.get('completed_at')):<32}  "
                f"{_fmt(entry.get('created_by')):<10}"
            )
    lines.append("")

    # Recommendation
    lines.append("-- Recommended action --")
    lines.append(f"  {action}")
    return "\n".join(lines)


# ── orchestration ────────────────────────────────────────────────────────────


async def diagnose(pool: Any, identifier: str) -> str:
    msg_row = await _fetch_message(pool, identifier)
    if msg_row is None:
        return render(
            identifier=identifier,
            msg={},
            turns=[],
            tool_calls=[],
            final_outbound=None,
            eligible=False,
            eligible_reason="message not found",
            action=(
                f"no messages row matches identifier={identifier!r} "
                "(neither as messages.id UUID nor as whatsapp_message_id)"
            ),
        )

    msg = _row_to_dict(msg_row)
    message_id: UUID = msg["id"]

    turns_rows = await _fetch_linked_turns(pool, message_id)
    turns = [_row_to_dict(r) for r in turns_rows]

    tool_calls: list[dict[str, Any]] = []
    if turns:
        latest_turn_id = turns[-1].get("id")
        if latest_turn_id is not None:
            tc_rows = await _fetch_tool_calls(pool, latest_turn_id)
            tool_calls = [_row_to_dict(r) for r in tc_rows]

    final_outbound: dict[str, Any] | None = None
    # Prefer turns' final_output_message_id; fall back to none.
    for t in reversed(turns):
        fid = t.get("final_output_message_id")
        if fid is not None:
            ob = await _fetch_outbound(pool, fid)
            if ob is not None:
                final_outbound = _row_to_dict(ob)
            break

    known_bots = await _fetch_coalescer_known_bots(pool)

    ledger_rows = await _fetch_ledger_attempts(pool, message_id)
    ledger_attempts = [_row_to_dict(r) for r in ledger_rows]

    eligible, reason = evaluate_retry_eligibility(
        msg,
        known_bot_ids=known_bots,
    )
    action = recommended_action(
        msg,
        eligible=eligible,
        eligible_reason=reason,
        turns=turns,
        outbound_id=(final_outbound or {}).get("id"),
        known_bot_ids=known_bots,
    )

    return render(
        identifier=identifier,
        msg=msg,
        turns=turns,
        tool_calls=tool_calls,
        final_outbound=final_outbound,
        eligible=eligible,
        eligible_reason=reason,
        action=action,
        ledger_attempts=ledger_attempts,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Diagnose why a Discord bot did or did not reply to an inbound "
            "message.  Accepts either a messages.id UUID or a transport-side "
            "id (Discord snowflake / WhatsApp message id)."
        ),
    )
    parser.add_argument(
        "message_id",
        help="messages.id UUID OR transport-side message id (e.g. Discord snowflake)",
    )
    parser.add_argument(
        "--database-url",
        type=str,
        default=os.environ.get("DATABASE_URL"),
        help="DATABASE_URL override (default: $DATABASE_URL)",
    )
    parser.add_argument(
        "--database-schema",
        type=str,
        default=os.environ.get("DATABASE_SCHEMA", "public"),
        help="search_path schema (default: $DATABASE_SCHEMA or 'public')",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    if not args.database_url:
        print("error: DATABASE_URL is not set", file=sys.stderr)
        return 2
    pool = await _connect(args.database_url, args.database_schema)
    try:
        out = await diagnose(pool, args.message_id)
    finally:
        await pool.close()
    print(out)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
