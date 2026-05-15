#!/usr/bin/env python3
"""Two-phase import of a ChatGPT-export markdown into the Veas database.

Phase 1 — ``categorise``: a Claude agent reads raw markdown and produces an
annotated document using pandoc-style fenced divs (``::: memory …``,
``::: distillation …``, ``::: theme …``, ``::: pregnancy …``,
``::: style-notes``, ``::: skip``). Output is plain markdown you can review
or hand-edit before importing.

Phase 2 — ``import``: parses the annotated document and writes rows to the
DB (memories, distillations, themes, users.style_notes, users.pregnancy_*).

Typical flow:

    PYENV_VERSION=3.11.11 python -m scripts.import_chatgpt categorise \\
        --bot tante_rosi --in ~/Downloads/chatgpt.md --out /tmp/rosi.md
    # review/edit /tmp/rosi.md
    PYENV_VERSION=3.11.11 python -m scripts.import_chatgpt import \\
        --bot tante_rosi --user-name "Peter" --file /tmp/rosi.md

You can also write the annotated doc by hand and skip ``categorise`` entirely.

Re-running ``import`` creates duplicates. Every inserted content row starts
with ``[chatgpt-import]`` so cleanup is one DELETE per table.

Annotation format:

    ::: memory confidence=high
    Peter lives in Berlin with his partner Hannah.
    :::

    ::: distillation confidence=medium sensitivity=low
    Has been weighing parental leave splits for months.
    :::

    ::: theme
    title: Parental leave planning
    description: Ongoing back-and-forth about how to split leave.
    :::

    ::: pregnancy
    edd: 2026-09-12
    dating_basis: scan
    scan_date: 2026-01-15
    :::

    ::: style-notes
    Terse replies preferred. No emojis.
    :::

    ::: skip
    (anything the agent decided is transient — preserved for transparency)
    :::
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import anthropic
import asyncpg

from app.config import get_settings
from app.services import crypto

logger = logging.getLogger("import_chatgpt")

IMPORT_TAG = "[chatgpt-import]"
DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_CHARS = 80_000

BOT_TO_TOPIC_SLUG = {
    "mediator": "relationship",
    "tante_rosi": "pregnancy",
}
BOT_DISPLAY = {
    "mediator": "Véas (a relationship mediator helping two partners communicate)",
    "tante_rosi": "Tante Rosi (a calm, plain-spoken German pregnancy companion)",
}

ALLOWED_KINDS = {"memory", "distillation", "theme", "pregnancy", "style-notes", "skip"}


# --- categorise prompt ------------------------------------------------------

CATEGORISE_SYSTEM = """\
You are an annotator preparing ChatGPT-export markdown for import into a database
that seeds a bot. The bot you are seeding is: {bot_display}

Read the markdown excerpt and rewrite it as a sequence of pandoc-style fenced
div blocks. Use ONLY these block kinds:

    ::: memory confidence=high|medium|low
    <one short third-person sentence about the user>
    :::

    ::: distillation confidence=high|medium|low sensitivity=low|medium|high
    <1–3 sentence summary of a recurring theme or pattern>
    :::

    ::: theme
    title: <short title>
    description: <one or two sentences>
    :::

    ::: style-notes
    <preferences about communication style: terseness, tone, language, taboos>
    :::
{pregnancy_kind}
    ::: skip
    <text you decided is transient or out of scope — keep verbatim for transparency>
    :::

Rules:
- Output ONLY blocks, no commentary, no headings outside blocks.
- One fact per memory. One theme per theme block. One pattern per distillation.
- Do not invent facts. If unsure, mark confidence=low or use ::: skip.
- Memories phrase the user in third person from the bot's perspective.
- Distillations are about recurring patterns, not single events.
- Use ::: skip generously for greetings, meta-chat, transient details, anything
  the bot does not need to know going forward. Skipped content is preserved in
  the annotated doc but not written to the DB — it makes your categorisation
  reviewable.
- Block bodies must not themselves contain ``:::`` lines.
"""

PREGNANCY_KIND = """
    ::: pregnancy
    edd: YYYY-MM-DD          # if the user gave a due date
    dating_basis: lmp|scan
    lmp_date: YYYY-MM-DD
    scan_date: YYYY-MM-DD
    started_at: YYYY-MM-DDTHH:MM:SSZ
    outcome: birth|loss|termination
    ended_at: YYYY-MM-DDTHH:MM:SSZ
    :::
    Only emit this when the user explicitly mentioned pregnancy state. Include
    only the fields you have. Omit the block entirely if nothing applies.
"""


# --- markdown splitting -----------------------------------------------------

def split_threads(text: str, header_regex: str, max_chars: int) -> list[str]:
    pat = re.compile(header_regex, flags=re.MULTILINE)
    parts: list[str] = []
    last = 0
    for m in pat.finditer(text):
        if m.start() > last:
            parts.append(text[last:m.start()].strip())
        last = m.start()
    parts.append(text[last:].strip())
    parts = [p for p in parts if p]

    if len(parts) <= 1:
        parts = [text.strip()]

    chunks: list[str] = []
    for part in parts:
        if len(part) <= max_chars:
            chunks.append(part)
            continue
        chunks.extend(_window(part, max_chars))
    return [c for c in chunks if c.strip()]


def _window(text: str, max_chars: int) -> list[str]:
    paragraphs = text.split("\n\n")
    out: list[str] = []
    buf: list[str] = []
    size = 0
    for p in paragraphs:
        plen = len(p) + 2
        if size + plen > max_chars and buf:
            out.append("\n\n".join(buf))
            buf = [p]
            size = plen
        else:
            buf.append(p)
            size += plen
    if buf:
        out.append("\n\n".join(buf))
    return out


# --- categorise -------------------------------------------------------------

async def categorise(args: argparse.Namespace) -> int:
    text = Path(args.in_path).expanduser().read_text(encoding="utf-8")
    chunks = split_threads(text, args.split_regex, args.max_chars)
    if args.max_chunks:
        chunks = chunks[: args.max_chunks]
    logger.info("categorising %d chunks (%d total chars)", len(chunks), len(text))

    settings = get_settings()
    client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key.get_secret_value())

    is_rosi = args.bot == "tante_rosi"
    system = CATEGORISE_SYSTEM.format(
        bot_display=BOT_DISPLAY[args.bot],
        pregnancy_kind=PREGNANCY_KIND if is_rosi else "",
    )

    pieces: list[str] = []
    for i, chunk in enumerate(chunks, 1):
        logger.info("  chunk %d/%d (%d chars)", i, len(chunks), len(chunk))
        try:
            msg = await client.messages.create(
                model=args.model,
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": chunk}],
            )
        except Exception:
            logger.exception("  failed on chunk %d, skipping", i)
            continue
        out = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        pieces.append(f"<!-- chunk {i}/{len(chunks)} -->\n\n{out}")

    annotated = "\n\n".join(pieces) + "\n"

    if args.out_path:
        Path(args.out_path).expanduser().write_text(annotated, encoding="utf-8")
        logger.info("wrote annotated doc to %s", args.out_path)
    else:
        sys.stdout.write(annotated)
    return 0


# --- annotated parser ------------------------------------------------------

BLOCK_RE = re.compile(
    r"^:::[ \t]+(?P<kind>[\w-]+)(?P<attrs>[^\n]*)\n(?P<body>.*?)\n?^:::[ \t]*$",
    re.MULTILINE | re.DOTALL,
)
ATTR_RE = re.compile(r"(\w[\w-]*)=(\"[^\"]*\"|\S+)")


def parse_blocks(text: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in BLOCK_RE.finditer(text):
        kind = m.group("kind")
        if kind not in ALLOWED_KINDS:
            logger.warning("unknown block kind %r — skipped", kind)
            continue
        attrs = {
            k: v.strip('"') for k, v in ATTR_RE.findall(m.group("attrs"))
        }
        body = m.group("body").strip()
        out.append({"kind": kind, "attrs": attrs, "body": body})
    return out


def _parse_kv_body(body: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip()
        if "#" in v:  # strip inline comments
            v = v.split("#", 1)[0].strip()
        if v:
            result[k.strip().replace("-", "_")] = v
    return result


# --- DB writers ------------------------------------------------------------

async def resolve_user_id(
    conn: asyncpg.Connection,
    *,
    user_id: str | None,
    user_name: str | None,
) -> str:
    if user_id:
        row = await conn.fetchrow("SELECT id FROM users WHERE id = $1", user_id)
        if not row:
            raise SystemExit(f"No user with id={user_id}")
        return str(row["id"])
    if not user_name:
        raise SystemExit("Either --user-id or --user-name is required")
    rows = await conn.fetch(
        "SELECT id, name FROM users WHERE name ILIKE $1",
        f"%{user_name}%",
    )
    if not rows:
        raise SystemExit(f"No user matching name={user_name!r}")
    if len(rows) > 1:
        names = ", ".join(f"{r['name']} ({r['id']})" for r in rows)
        raise SystemExit(f"Ambiguous --user-name {user_name!r}: {names}")
    return str(rows[0]["id"])


async def resolve_topic_id(conn: asyncpg.Connection, *, bot: str) -> str:
    slug = BOT_TO_TOPIC_SLUG[bot]
    row = await conn.fetchrow("SELECT id FROM topics WHERE slug = $1", slug)
    if not row:
        raise SystemExit(f"No topic with slug={slug!r}; has the bot/topic migration run?")
    return str(row["id"])


async def tag_artifact(
    conn: asyncpg.Connection,
    *,
    table: str,
    artifact_id: str,
    topic_id: str,
    bot_id: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO artifact_topics (
            artifact_table, artifact_id, topic_id, tagged_by_bot_id, status, reason
        )
        VALUES ($1, $2, $3, $4, 'active', 'ChatGPT import')
        ON CONFLICT (artifact_table, artifact_id, topic_id) DO UPDATE
        SET status = 'active',
            tagged_by_bot_id = EXCLUDED.tagged_by_bot_id,
            reason = EXCLUDED.reason,
            retired_at = NULL
        """,
        table, artifact_id, topic_id, bot_id,
    )


async def write_memory(conn, *, user_id, bot_id, topic_id, content, confidence="medium") -> str:  # noqa: ARG001
    tagged = f"{IMPORT_TAG} {content}"
    row = await conn.fetchrow(
        """
        INSERT INTO memories (about_user_id, content, content_encrypted, recorded_by_bot_id)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        user_id, tagged, crypto.encrypt_value(tagged), bot_id,
    )
    artifact_id = str(row["id"])
    await tag_artifact(
        conn, table="memories", artifact_id=artifact_id, topic_id=topic_id, bot_id=bot_id,
    )
    return artifact_id


async def write_theme(conn, *, bot_id: str, topic_id: str, title: str, description: str) -> str:
    row = await conn.fetchrow(
        """
        INSERT INTO themes (title, description, recorded_by_bot_id)
        VALUES ($1, $2, $3)
        RETURNING id
        """,
        f"{IMPORT_TAG} {title}", description, bot_id,
    )
    artifact_id = str(row["id"])
    await tag_artifact(
        conn, table="themes", artifact_id=artifact_id, topic_id=topic_id, bot_id=bot_id,
    )
    return artifact_id


async def write_distillation(
    conn,
    *,
    user_id: str,
    bot_id: str,
    topic_id: str,
    content: str,
    confidence: str,
    sensitivity: str,
    related_memory_ids: list[str],
    related_theme_ids: list[str],
) -> str:
    tagged = f"{IMPORT_TAG} {content}"
    confidence = confidence if confidence in ("high", "medium", "low") else "medium"
    sensitivity = sensitivity if sensitivity in ("low", "medium", "high") else "medium"
    row = await conn.fetchrow(
        """
        INSERT INTO distillations (
            content, content_encrypted,
            confidence, sensitivity, visibility,
            source_user_ids,
            related_memory_ids, related_observation_ids, related_theme_ids, supporting_message_ids,
            recorded_by_bot_id
        )
        VALUES ($1, $2, $3, $4, 'private', $5, $6, '{}'::uuid[], $7, '{}'::uuid[], $8)
        RETURNING id
        """,
        tagged, crypto.encrypt_value(tagged), confidence, sensitivity,
        [user_id], related_memory_ids, related_theme_ids, bot_id,
    )
    artifact_id = str(row["id"])
    await tag_artifact(
        conn, table="distillations", artifact_id=artifact_id, topic_id=topic_id, bot_id=bot_id,
    )
    return artifact_id


async def append_style_notes(conn, *, user_id: str, note: str) -> None:
    tagged = f"{IMPORT_TAG} {note}"
    await conn.execute(
        """
        UPDATE users
        SET style_notes = CASE
            WHEN style_notes IS NULL OR style_notes = '' THEN $2
            ELSE style_notes || E'\n\n' || $2
        END
        WHERE id = $1
        """,
        user_id, tagged,
    )


async def patch_pregnancy(conn, *, user_id: str, state: dict[str, str]) -> list[str]:
    field_map = {
        "edd": "pregnancy_edd",
        "dating_basis": "pregnancy_dating_basis",
        "lmp_date": "pregnancy_lmp_date",
        "scan_date": "pregnancy_scan_date",
        "started_at": "pregnancy_started_at",
        "outcome": "pregnancy_outcome",
        "ended_at": "pregnancy_ended_at",
    }
    current = await conn.fetchrow(
        "SELECT " + ", ".join(field_map.values()) + " FROM users WHERE id = $1",
        user_id,
    )
    if not current:
        return []

    date_fields = {"pregnancy_edd", "pregnancy_lmp_date", "pregnancy_scan_date"}
    datetime_fields = {"pregnancy_started_at", "pregnancy_ended_at"}

    pending: dict[str, Any] = {}
    for key, col in field_map.items():
        if key in state and current[col] is None:
            raw_value = state[key]
            if col in date_fields:
                pending[col] = date.fromisoformat(raw_value)
            elif col in datetime_fields:
                pending[col] = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
            else:
                pending[col] = raw_value

    # EDD + dating_basis must move together (DB constraint).
    if ("pregnancy_edd" in pending) ^ ("pregnancy_dating_basis" in pending):
        pending.pop("pregnancy_edd", None)
        pending.pop("pregnancy_dating_basis", None)
    # outcome + ended_at must move together.
    if ("pregnancy_outcome" in pending) ^ ("pregnancy_ended_at" in pending):
        pending.pop("pregnancy_outcome", None)
        pending.pop("pregnancy_ended_at", None)

    if not pending:
        return []
    setters = []
    values: list[Any] = [user_id]
    for col, val in pending.items():
        values.append(val)
        setters.append(f"{col} = ${len(values)}")
    await conn.execute(
        f"UPDATE users SET {', '.join(setters)} WHERE id = $1",
        *values,
    )
    return [f"{c}={v!r}" for c, v in pending.items()]


# --- import driver ---------------------------------------------------------

async def import_(args: argparse.Namespace) -> int:
    text = Path(args.file).expanduser().read_text(encoding="utf-8")
    blocks = parse_blocks(text)

    counts: dict[str, int] = {}
    for b in blocks:
        counts[b["kind"]] = counts.get(b["kind"], 0) + 1
    logger.info("parsed %d blocks: %s", len(blocks), counts)

    if args.dry_run:
        print(json.dumps(blocks, indent=2, ensure_ascii=False))
        return 0

    settings = get_settings()
    conn = await asyncpg.connect(settings.database_url, statement_cache_size=0)
    if settings.database_schema != "public":
        await conn.execute(f"SET search_path TO {settings.database_schema}, public")
    try:
        user_id = await resolve_user_id(
            conn, user_id=args.user_id, user_name=args.user_name,
        )
        topic_id = await resolve_topic_id(conn, bot=args.bot)
        logger.info("writing for user_id=%s bot=%s topic_id=%s", user_id, args.bot, topic_id)

        async with conn.transaction():
            theme_ids: list[str] = []
            for b in blocks:
                if b["kind"] != "theme":
                    continue
                kv = _parse_kv_body(b["body"])
                title = kv.get("title") or b["body"].splitlines()[0][:120]
                description = kv.get("description") or b["body"]
                theme_ids.append(await write_theme(
                    conn, bot_id=args.bot, topic_id=topic_id, title=title, description=description,
                ))

            memory_ids: list[str] = []
            for b in blocks:
                if b["kind"] != "memory":
                    continue
                memory_ids.append(await write_memory(
                    conn,
                    user_id=user_id,
                    bot_id=args.bot,
                    topic_id=topic_id,
                    content=b["body"],
                    confidence=b["attrs"].get("confidence", "medium"),
                ))

            for b in blocks:
                if b["kind"] != "distillation":
                    continue
                if not memory_ids and not theme_ids:
                    logger.warning(
                        "no memory/theme to anchor distillation, skipping: %s",
                        b["body"][:80],
                    )
                    continue
                await write_distillation(
                    conn,
                    user_id=user_id,
                    bot_id=args.bot,
                    topic_id=topic_id,
                    content=b["body"],
                    confidence=b["attrs"].get("confidence", "medium"),
                    sensitivity=b["attrs"].get("sensitivity", "medium"),
                    related_memory_ids=memory_ids,
                    related_theme_ids=theme_ids,
                )

            for b in blocks:
                if b["kind"] != "style-notes":
                    continue
                await append_style_notes(conn, user_id=user_id, note=b["body"])

            if args.bot == "tante_rosi":
                preg_state: dict[str, str] = {}
                for b in blocks:
                    if b["kind"] == "pregnancy":
                        preg_state.update(_parse_kv_body(b["body"]))
                if preg_state:
                    applied = await patch_pregnancy(
                        conn, user_id=user_id, state=preg_state,
                    )
                    if applied:
                        logger.info("patched pregnancy: %s", ", ".join(applied))
                    else:
                        logger.info("pregnancy state present but no fields applied")

        skipped = counts.get("skip", 0)
        logger.info(
            "done. memories=%d distillations=%d themes=%d style-notes=%d skipped=%d",
            len(memory_ids),
            counts.get("distillation", 0),
            len(theme_ids),
            counts.get("style-notes", 0),
            skipped,
        )
    finally:
        await conn.close()
    return 0


# --- CLI -------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("categorise", help="Annotate raw markdown with category blocks")
    c.add_argument("--bot", required=True, choices=list(BOT_TO_TOPIC_SLUG.keys()))
    c.add_argument("--in", dest="in_path", required=True, help="Path to raw markdown")
    c.add_argument("--out", dest="out_path", help="Path to write annotated doc (defaults to stdout)")
    c.add_argument("--model", default=DEFAULT_MODEL)
    c.add_argument("--max-chars", type=int, default=DEFAULT_MAX_CHARS, dest="max_chars")
    c.add_argument("--max-chunks", type=int, default=0)
    c.add_argument("--split-regex", default=r"^#\s+\S")
    c.add_argument("--log-level", default="INFO")

    i = sub.add_parser("import", help="Parse annotated doc and write to DB")
    i.add_argument("--bot", required=True, choices=list(BOT_TO_TOPIC_SLUG.keys()))
    g = i.add_mutually_exclusive_group(required=True)
    g.add_argument("--user-id")
    g.add_argument("--user-name")
    i.add_argument("--file", required=True, help="Annotated markdown to import")
    i.add_argument("--dry-run", action="store_true", help="Parse and print blocks, no DB writes")
    i.add_argument("--log-level", default="INFO")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if args.cmd == "categorise":
        return asyncio.run(categorise(args))
    if args.cmd == "import":
        return asyncio.run(import_(args))
    return 2


if __name__ == "__main__":
    sys.exit(main())
