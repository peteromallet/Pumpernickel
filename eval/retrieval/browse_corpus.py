"""Browse a local corpus YAML to find message ids for golden-set labeling.

Reads a local corpus YAML ONLY (no database). This is the tool a human uses to
hunt for the expected_message_ids that go into the real golden set: grep for a
phrase, list a thread, or look up a specific id, and copy the printed ids into
real_golden_set.yaml (see eval/retrieval/REAL_GOLDEN_SET.md).

Prints one line per match:
    [<id>] (thread=<thread_id>, topic=<topic_id>, <sender>→<recipient>, <sent_at>) <content>

Usage:
    python -m eval.retrieval.browse_corpus --corpus eval/retrieval/real_corpus.yaml \
        [--grep <substring>] [--id <message_id>] [--thread <thread_id>] \
        [--limit N]

Note: --grep matches case-insensitively against message content. Filters
combine with AND (a message must satisfy every provided filter).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

DEFAULT_LIMIT = 50


def _load(path: Path) -> list[dict[str, Any]]:
    import yaml

    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or "messages" not in data:
        raise SystemExit(f"{path} does not look like a corpus (no top-level 'messages').")
    return list(data["messages"] or [])


def _matches(
    msg: dict[str, Any],
    *,
    grep: str | None,
    msg_id: str | None,
    thread: str | None,
) -> bool:
    if msg_id is not None and str(msg.get("id")) != msg_id:
        return False
    if thread is not None and str(msg.get("thread_id")) != thread:
        return False
    if grep is not None:
        if grep.lower() not in str(msg.get("content") or "").lower():
            return False
    return True


def _format(msg: dict[str, Any]) -> str:
    content = str(msg.get("content") or "").replace("\n", " ⏎ ")
    return (
        f"[{msg.get('id')}] "
        f"(thread={msg.get('thread_id')}, topic={msg.get('topic_id')}, "
        f"{msg.get('sender')}→{msg.get('recipient')}, {msg.get('sent_at')}) "
        f"{content}"
    )


def browse(args: argparse.Namespace) -> int:
    messages = _load(Path(args.corpus))
    shown = 0
    for msg in messages:
        if not _matches(msg, grep=args.grep, msg_id=args.id, thread=args.thread):
            continue
        print(_format(msg))
        shown += 1
        if shown >= args.limit:
            break
    print(f"\n-- {shown} match(es) shown (limit {args.limit}) --", file=sys.stderr)
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m eval.retrieval.browse_corpus",
        description=(
            "Browse a local corpus YAML to find message ids for the golden set. "
            "Reads the local file only (no DB). Filters combine with AND."
        ),
    )
    parser.add_argument(
        "--corpus",
        required=True,
        help="Path to a corpus YAML (e.g. eval/retrieval/real_corpus.yaml).",
    )
    parser.add_argument(
        "--grep",
        default=None,
        help="Case-insensitive substring to match against message content.",
    )
    parser.add_argument(
        "--id",
        default=None,
        help="Show only the message with this exact id.",
    )
    parser.add_argument(
        "--thread",
        default=None,
        help="Show only messages in this thread_id.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max matches to print (default {DEFAULT_LIMIT}).",
    )
    args = parser.parse_args(argv)
    if args.grep is None and args.id is None and args.thread is None:
        parser.error("provide at least one of --grep, --id, or --thread.")
    return args


def main(argv: list[str] | None = None) -> int:
    return browse(_parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
