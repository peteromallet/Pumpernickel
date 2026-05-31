"""Extract a REAL-data corpus from the production messages table.

  ⚠️  PRIVACY WARNING  ⚠️
  This script reads REAL, intimate user messages from the production database
  and writes them to disk in plaintext YAML. The output file
  (default: eval/retrieval/real_corpus.yaml) is GITIGNORED and must NEVER be
  committed. Treat the output as sensitive: keep it local, and delete it when
  you are finished labeling (see eval/retrieval/REAL_GOLDEN_SET.md).

This is part of the launch gate for the OpenAI hosted hybrid retriever: it lets
a human build a real-data golden set to validate the retriever against actual
production messages (not just the synthetic corpus).

The output conforms exactly to the CorpusMessage schema in
eval/retrieval/schema.py and can be loaded by eval/retrieval/loader.py.

Connection: reads DIRECT_DATABASE_URL the same way DbBackedRetriever does
(prefer app.config.get_settings().direct_database_url, fall back to the raw
env var), and lazily imports psycopg so the offline harness never needs DB
dependencies just to show --help.

Bounded by default for privacy: --limit defaults to 300 and is NEVER unbounded.

Usage:
    python -m eval.retrieval.extract_real_corpus \
        [--limit N] [--since YYYY-MM-DD] \
        [--topic <uuid>] [--thread-root <uuid>] \
        [--out eval/retrieval/real_corpus.yaml]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NO_TOPIC_SENTINEL = "no_topic"
DEFAULT_OUT = "eval/retrieval/real_corpus.yaml"
DEFAULT_LIMIT = 300


def _get_db_url() -> str:
    """Resolve DIRECT_DATABASE_URL the same way DbBackedRetriever does."""
    db_url = None
    try:
        import importlib

        _cfg = importlib.import_module("app.config")
        db_url = _cfg.get_settings().direct_database_url
    except (ImportError, ModuleNotFoundError, AttributeError):
        db_url = None
    if not db_url:
        db_url = os.environ.get("DIRECT_DATABASE_URL")
    if not db_url:
        raise SystemExit(
            "DIRECT_DATABASE_URL must be set (or app.config must provide it) "
            "to extract a real corpus."
        )
    return db_url


def _build_query(
    *,
    limit: int,
    since: str | None,
    topic: str | None,
    thread_root: str | None,
) -> tuple[str, list[Any]]:
    """Build the SELECT against the messages table with privacy-safe filters."""
    where = ["deleted_at IS NULL", "search_suppressed_at IS NULL"]
    params: list[Any] = []

    if since is not None:
        where.append("sent_at >= %s")
        params.append(since)
    if topic is not None:
        where.append("topic_id = %s")
        params.append(topic)
    if thread_root is not None:
        # The chain rooted at thread_root: either the root itself or any
        # message that (transitively) replies into it. We can't express the
        # transitive walk in SQL cheaply, so fetch the candidate root plus its
        # direct/indirect replies via a recursive CTE.
        where.append(
            "id IN ("
            "WITH RECURSIVE chain AS ("
            "  SELECT id FROM messages WHERE id = %s "
            "  UNION ALL "
            "  SELECT m.id FROM messages m JOIN chain c ON m.in_reply_to = c.id"
            ") SELECT id FROM chain)"
        )
        params.append(thread_root)

    where_clause = " AND ".join(where)
    sql = (
        "SELECT id, content, sender_id, recipient_id, direction, topic_id, "
        "sent_at, in_reply_to, media_analysis "
        f"FROM messages WHERE {where_clause} "
        "ORDER BY sent_at DESC LIMIT %s"
    )
    params.append(limit)
    return sql, params


def _resolve_root(
    msg_id: str,
    parent_of: dict[str, str | None],
    memo: dict[str, str],
) -> str:
    """Walk in_reply_to to the root id, memoizing and guarding against cycles."""
    if msg_id in memo:
        return memo[msg_id]
    path: list[str] = []
    cur: str | None = msg_id
    seen: set[str] = set()
    while cur is not None and cur in parent_of and cur not in seen:
        seen.add(cur)
        path.append(cur)
        parent = parent_of[cur]
        if parent is None:
            # cur is its own root.
            break
        if parent not in parent_of:
            # Broken/missing link: fall back to cur as the root.
            break
        cur = parent
    root = cur if cur is not None and cur in parent_of else msg_id
    # cur is the deepest resolvable node; if its parent is None it's the root,
    # otherwise the chain is broken and cur is the best-effort root.
    for node in path:
        memo[node] = root
    memo[msg_id] = root
    return root


def _name_map(conn: Any, user_ids: set[str]) -> dict[str, str]:
    """Resolve user uuids to users.name. Missing ids simply absent from map."""
    names: dict[str, str] = {}
    ids = [u for u in user_ids if u]
    if not ids:
        return names
    with conn.cursor() as cur:
        cur.execute("SELECT id, name FROM users WHERE id = ANY(%s)", (ids,))
        for uid, name in cur.fetchall():
            names[str(uid)] = name
    return names


def _label(
    user_id: str | None,
    direction: str | None,
    names: dict[str, str],
) -> str:
    """Resolve a participant to a stable, deterministic display label.

    Prefer users.name; otherwise fall back to a stable label combining the
    direction role and a truncated uuid (e.g. 'inbound:1a2b3c4d').
    """
    if user_id and str(user_id) in names:
        return names[str(user_id)]
    role = (direction or "unknown").strip() or "unknown"
    if user_id:
        return f"{role}:{str(user_id)[:8]}"
    return f"{role}:unknown"


def extract(args: argparse.Namespace) -> int:
    db_url = _get_db_url()

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise SystemExit(
            "psycopg is required to extract a real corpus. "
            "Install with: pip install psycopg[binary]"
        ) from exc

    import yaml

    sql, params = _build_query(
        limit=args.limit,
        since=args.since,
        topic=args.topic,
        thread_root=args.thread_root,
    )

    rows: list[dict[str, Any]] = []
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d.name for d in cur.description]
            for raw in cur.fetchall():
                rows.append(dict(zip(cols, raw)))

        # Resolve participant names.
        user_ids: set[str] = set()
        for r in rows:
            if r.get("sender_id"):
                user_ids.add(str(r["sender_id"]))
            if r.get("recipient_id"):
                user_ids.add(str(r["recipient_id"]))
        names = _name_map(conn, user_ids)

    # Build a parent map for thread-root synthesis.
    parent_of: dict[str, str | None] = {}
    for r in rows:
        mid = str(r["id"])
        irt = r.get("in_reply_to")
        parent_of[mid] = str(irt) if irt is not None else None

    memo: dict[str, str] = {}

    messages: list[dict[str, Any]] = []
    threads: set[str] = set()
    topics: set[str] = set()
    dates: list[datetime] = []

    for r in rows:
        mid = str(r["id"])
        thread_id = _resolve_root(mid, parent_of, memo)
        topic_raw = r.get("topic_id")
        topic_id = str(topic_raw) if topic_raw is not None else NO_TOPIC_SENTINEL
        sent_at = r["sent_at"]
        if isinstance(sent_at, datetime):
            if sent_at.tzinfo is None:
                sent_at = sent_at.replace(tzinfo=timezone.utc)
            sent_at_iso = sent_at.isoformat()
            dates.append(sent_at)
        else:
            sent_at_iso = str(sent_at)

        entry: dict[str, Any] = {
            "id": mid,
            "thread_id": thread_id,
            "topic_id": topic_id,
            "sender": _label(r.get("sender_id"), r.get("direction"), names),
            "recipient": _label(r.get("recipient_id"), r.get("direction"), names),
            "sent_at": sent_at_iso,
            "content": r.get("content") or "",
        }
        ma = r.get("media_analysis")
        if ma is not None:
            entry["media_analysis"] = ma
        messages.append(entry)
        threads.add(thread_id)
        topics.add(topic_id)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        yaml.safe_dump(
            {"messages": messages},
            f,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )

    # Summary only — never print message content.
    if dates:
        lo = min(dates).isoformat()
        hi = max(dates).isoformat()
        date_range = f"{lo} .. {hi}"
    else:
        date_range = "(none)"
    print(f"Wrote {len(messages)} messages to {out_path}")
    print(f"  threads: {len(threads)}")
    print(f"  topics:  {len(topics)}")
    print(f"  date range: {date_range}")
    print("  NOTE: this file contains REAL user data and is gitignored.")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m eval.retrieval.extract_real_corpus",
        description=(
            "Extract a REAL-data corpus from the production messages table for "
            "the retrieval eval launch gate. ⚠️ WRITES REAL INTIMATE USER DATA "
            "TO DISK IN PLAINTEXT — the output is gitignored and should be "
            "deleted after labeling. Bounded by default (never unbounded)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max messages to extract (default {DEFAULT_LIMIT}; never unbounded).",
    )
    parser.add_argument(
        "--since",
        default=None,
        help="Only messages with sent_at >= this date (YYYY-MM-DD).",
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="Restrict to a single topic_id (uuid).",
    )
    parser.add_argument(
        "--thread-root",
        default=None,
        help="Restrict to the reply chain rooted at this message id (uuid).",
    )
    parser.add_argument(
        "--out",
        default=DEFAULT_OUT,
        help=f"Output YAML path (default {DEFAULT_OUT}; gitignored).",
    )
    args = parser.parse_args(argv)
    if args.limit <= 0:
        parser.error("--limit must be a positive integer (extraction is never unbounded).")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return extract(args)


if __name__ == "__main__":
    sys.exit(main())
