"""Dedicated fixture-backed fake pool for M4 Sisypy nav/search validation.

This pool is intentionally narrow: it only implements the SQL shapes used by
the selected nav/search tools plus the registry's turn-audit/tool-call writes.
Anything outside that surface is recorded as infrastructure failure evidence so
scenario grading can distinguish harness breakage from agent behavior.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.split())


def _jsonb(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _as_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    raise TypeError(f"unsupported datetime value: {value!r}")


def _as_date(value: Any) -> date | None:
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    raise TypeError(f"unsupported date value: {value!r}")


@dataclass(slots=True)
class _InfrastructureIssue:
    kind: str
    sql: str
    args: list[str]
    note: str
    recorded_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )


class _FakeTransaction:
    def __init__(self, pool: "AgenticFakePool") -> None:
        self.pool = pool

    async def __aenter__(self) -> None:
        self.pool.transaction_depth += 1
        return None

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.pool.transaction_depth -= 1
        return False


class _FakeConnection:
    def __init__(self, pool: "AgenticFakePool") -> None:
        self.pool = pool

    async def execute(self, sql: str, *args: Any) -> Any:
        return await self.pool.execute(sql, *args)

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return await self.pool.fetch(sql, *args)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        return await self.pool.fetchrow(sql, *args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return await self.pool.fetchval(sql, *args)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self.pool)


class _AcquireContext:
    def __init__(self, pool: "AgenticFakePool") -> None:
        self.pool = pool

    async def __aenter__(self) -> _FakeConnection:
        return _FakeConnection(self.pool)

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class AgenticFakePool:
    """Small SQL-shape fake for M4 fixture runs.

    The pool models only the read-path queries used by:
    - `messages_before`
    - `messages_after`
    - `open_thread`
    - `scroll`
    - `topic_recent`
    - `search_messages`
    - `search` / `hybrid_search` hydration and retrieval SQL
    - `registry.call_tool()` audit writes
    """

    def __init__(
        self,
        *,
        messages: list[dict[str, Any]],
        viewer_user_id: UUID,
        partner_user_id: UUID | None,
        bot_id: str,
        topic_id: UUID,
        dyad_id: UUID | None = None,
        turn_id: UUID | None = None,
        thread_partner_share_default: str = "opt_in",
    ) -> None:
        self.viewer_user_id = viewer_user_id
        self.partner_user_id = partner_user_id
        self.bot_id = bot_id
        self.topic_id = topic_id
        self.dyad_id = dyad_id
        self.turn_id = turn_id
        self.thread_partner_share_default = thread_partner_share_default
        self.transaction_depth = 0
        self.turn_audit_events: list[dict[str, Any]] = []
        self.tool_calls: list[dict[str, Any]] = []
        self.bot_turns: dict[UUID, dict[str, Any]] = {}
        self.infrastructure_issues: list[_InfrastructureIssue] = []
        self.executed_sql: list[tuple[str, tuple[Any, ...]]] = []
        self.message_embeddings: dict[UUID, dict[str, Any]] = {}
        self.out_of_bounds: list[dict[str, Any]] = []
        self._messages = [self._normalize_message(row) for row in messages]
        self._messages_by_id = {row["message_id"]: row for row in self._messages}

    def acquire(self) -> _AcquireContext:
        return _AcquireContext(self)

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(self)

    def seed_embedding(
        self,
        message_id: UUID,
        *,
        embedding: list[float],
        model: str,
        dimension: int,
    ) -> None:
        self.message_embeddings[message_id] = {
            "message_id": message_id,
            "embedding": embedding,
            "model": model,
            "dimension": dimension,
        }

    def add_oob(
        self,
        *,
        owner_id: UUID,
        severity: str = "firm",
        status: str = "active",
    ) -> None:
        self.out_of_bounds.append(
            {"owner_id": owner_id, "severity": severity, "status": status}
        )

    def infrastructure_status(self) -> dict[str, Any]:
        issues = [asdict(issue) for issue in self.infrastructure_issues]
        return {
            "status": "infrastructure" if issues else "ok",
            "infrastructure_failed": bool(issues),
            "reason": (
                f"{len(issues)} unsupported SQL statement(s) hit the M4 fake pool."
                if issues
                else "M4 fake pool handled the exercised SQL surface."
            ),
            "issues": issues,
        }

    def write_infrastructure_json(self, path: Path) -> None:
        path.write_text(
            json.dumps(self.infrastructure_status(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        compact = _normalize_sql(sql)
        self.executed_sql.append((compact, args))
        if "FROM ranked_ids JOIN mediator.v_searchable_messages m" in compact:
            return self._handle_hydrate_fetch(args)
        if "WITH query AS (" in compact and "keyword_matches" in compact:
            return self._handle_keyword_fetch(compact, args)
        if "WITH semantic_matches AS (" in compact:
            return self._handle_semantic_fetch(compact, args)
        if "FROM mediator.v_searchable_messages m" in compact:
            return self._handle_searchable_fetch(compact, args)
        return self._unsupported("fetch", compact, args, fallback=[])

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        compact = _normalize_sql(sql)
        self.executed_sql.append((compact, args))
        if compact.startswith("INSERT INTO turn_audit_events ("):
            return self._insert_turn_audit_event(args)
        if "FROM mediator.v_searchable_messages m" in compact:
            rows = self._handle_searchable_fetch(compact, args)
            return rows[0] if rows else None
        return self._unsupported("fetchrow", compact, args, fallback=None)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        compact = _normalize_sql(sql)
        self.executed_sql.append((compact, args))
        return self._unsupported("fetchval", compact, args, fallback=None)

    async def execute(self, sql: str, *args: Any) -> str:
        compact = _normalize_sql(sql)
        self.executed_sql.append((compact, args))
        if compact.startswith("INSERT INTO tool_calls "):
            self._insert_tool_call(args)
            return "INSERT 0 1"
        if compact.startswith("UPDATE bot_turns SET final_output_message_id=$1 WHERE id=$2"):
            message_id, turn_id = args
            row = self.bot_turns.setdefault(turn_id, {"id": turn_id})
            row["final_output_message_id"] = message_id
            return "UPDATE 1"
        if compact.startswith("SET LOCAL hnsw.ef_search = "):
            return "SET"
        return self._unsupported("execute", compact, args, fallback="UNSUPPORTED")

    def _normalize_message(self, raw: dict[str, Any]) -> dict[str, Any]:
        message_id = UUID(str(raw["id"]))
        sender_id = UUID(str(raw["sender_id"])) if raw.get("sender_id") else None
        recipient_id = (
            UUID(str(raw["recipient_id"])) if raw.get("recipient_id") else None
        )
        sent_at = _as_datetime(raw["sent_at"])
        if sent_at is None:
            raise ValueError(f"message {message_id} is missing sent_at")
        row = {
            "message_id": message_id,
            "id": message_id,
            "sender_id": sender_id,
            "recipient_id": recipient_id,
            "thread_owner_user_id": (
                UUID(str(raw["thread_owner_user_id"]))
                if raw.get("thread_owner_user_id")
                else self._derive_thread_owner(raw, sender_id, recipient_id)
            ),
            "thread_owner_partner_share": raw.get(
                "thread_owner_partner_share", self.thread_partner_share_default
            ),
            "bot_id": raw.get("bot_id", self.bot_id),
            "topic_id": UUID(str(raw.get("topic_id", self.topic_id))),
            "dyad_id": UUID(str(raw["dyad_id"])) if raw.get("dyad_id") else self.dyad_id,
            "direction": raw.get("direction", "inbound"),
            "sent_at": sent_at,
            "content": raw.get("content", ""),
            "canonical_text": raw.get("canonical_text") or raw.get("content", ""),
            "media_analysis": raw.get("media_analysis"),
            "media_type": raw.get("media_type"),
            "charge": raw.get("charge", "routine"),
            "edited_at": _as_datetime(raw.get("edited_at")),
            "edit_history": raw.get("edit_history"),
            "deleted_at": _as_datetime(raw.get("deleted_at")),
            "search_suppressed_at": _as_datetime(raw.get("search_suppressed_at")),
            "local_day": _as_date(raw.get("local_day")),
            "semantic_terms": {term.casefold() for term in raw.get("semantic_terms", [])},
        }
        row["search_tsv_terms"] = self._keyword_terms(row["canonical_text"])
        return row

    def _derive_thread_owner(
        self,
        raw: dict[str, Any],
        sender_id: UUID | None,
        recipient_id: UUID | None,
    ) -> UUID | None:
        if raw.get("direction") == "outbound":
            return recipient_id or sender_id
        return sender_id or recipient_id

    def _participant_ids_from_args(self, args: tuple[Any, ...], idx: int = 2) -> list[UUID]:
        return [UUID(str(item)) for item in list(args[idx] or [])]

    def _base_visible_rows(
        self,
        *,
        participant_ids: list[UUID],
        viewer_user_id: UUID,
        bot_id: str,
        topic_id: UUID | None = None,
        thread_owner_user_id: UUID | None = None,
        dyad_id: UUID | None = None,
    ) -> list[dict[str, Any]]:
        visible = []
        for row in self._messages:
            if row["deleted_at"] is not None or row["search_suppressed_at"] is not None:
                continue
            if row["bot_id"] != bot_id:
                continue
            if row["thread_owner_user_id"] not in participant_ids:
                continue
            if row["sender_id"] not in participant_ids and row["recipient_id"] not in participant_ids:
                continue
            if (
                row["thread_owner_user_id"] != viewer_user_id
                and row["thread_owner_partner_share"] != "opt_in"
            ):
                continue
            if self._owner_has_active_oob(row["thread_owner_user_id"]):
                continue
            if topic_id is not None and row["topic_id"] != topic_id:
                continue
            if thread_owner_user_id is not None and row["thread_owner_user_id"] != thread_owner_user_id:
                continue
            if dyad_id is not None and row["dyad_id"] != dyad_id:
                continue
            visible.append(dict(row))
        return visible

    def _owner_has_active_oob(self, owner_id: UUID | None) -> bool:
        for row in self.out_of_bounds:
            if (
                row.get("owner_id") == owner_id
                and row.get("status") == "active"
                and row.get("severity") in {"firm", "hard"}
            ):
                return True
        return False

    def _handle_searchable_fetch(
        self, compact: str, args: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        bot_id = str(args[0])
        viewer_user_id = UUID(str(args[1]))
        participant_ids = self._participant_ids_from_args(args)
        topic_id, thread_owner_user_id, dyad_id, idx = self._parse_scope_params(
            compact, args, start_idx=3
        )

        rows = self._base_visible_rows(
            participant_ids=participant_ids,
            viewer_user_id=viewer_user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            thread_owner_user_id=thread_owner_user_id,
            dyad_id=dyad_id,
        )

        if "m.canonical_text ILIKE" in compact:
            pattern = str(args[idx]).strip("%").casefold()
            rows = [row for row in rows if pattern in row["canonical_text"].casefold()]
            idx += 1

        if "m.sent_at >=" in compact and "m.sent_at <" in compact:
            start = _as_datetime(args[idx])
            end = _as_datetime(args[idx + 1])
            rows = [
                row
                for row in rows
                if start is not None and end is not None and start <= row["sent_at"] < end
            ]
            idx += 2

        if "(m.sent_at, m.message_id) < (" in compact:
            anchor_sent_at = _as_datetime(args[idx])
            anchor_id = UUID(str(args[idx + 1]))
            rows = [
                row
                for row in rows
                if (row["sent_at"], row["message_id"]) < (anchor_sent_at, anchor_id)
            ]
            idx += 2
        elif "(m.sent_at, m.message_id) <= (" in compact:
            anchor_sent_at = _as_datetime(args[idx])
            anchor_id = UUID(str(args[idx + 1]))
            rows = [
                row
                for row in rows
                if (row["sent_at"], row["message_id"]) <= (anchor_sent_at, anchor_id)
            ]
            idx += 2
        elif "(m.sent_at, m.message_id) > (" in compact:
            anchor_sent_at = _as_datetime(args[idx])
            anchor_id = UUID(str(args[idx + 1]))
            rows = [
                row
                for row in rows
                if (row["sent_at"], row["message_id"]) > (anchor_sent_at, anchor_id)
            ]
            idx += 2
        elif "m.message_id =" in compact:
            anchor_id = UUID(str(args[idx]))
            rows = [row for row in rows if row["message_id"] == anchor_id]
            idx += 1

        descending = "ORDER BY m.sent_at DESC, m.message_id DESC" in compact
        rows.sort(
            key=lambda row: (row["sent_at"], row["message_id"].int),
            reverse=descending,
        )

        limit = int(args[idx]) if idx < len(args) else len(rows)
        selected = rows[:limit]
        if " AS id," in compact:
            return [self._project_search_messages_row(row) for row in selected]
        return selected

    def _parse_scope_params(
        self,
        compact: str,
        args: tuple[Any, ...],
        *,
        start_idx: int,
    ) -> tuple[UUID | None, UUID | None, UUID | None, int]:
        idx = start_idx
        topic_id = None
        thread_owner_user_id = None
        dyad_id = None
        if " AND m.topic_id = $" in compact:
            topic_id = UUID(str(args[idx]))
            idx += 1
        if " AND m.thread_owner_user_id = $" in compact:
            thread_owner_user_id = UUID(str(args[idx]))
            idx += 1
        if " AND m.dyad_id = $" in compact:
            dyad_id = UUID(str(args[idx]))
            idx += 1
        return topic_id, thread_owner_user_id, dyad_id, idx

    def _project_search_messages_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["message_id"],
            "sender_id": row["sender_id"],
            "recipient_id": row["recipient_id"],
            "sent_at": row["sent_at"],
            "content": row["content"],
            "media_type": row["media_type"],
            "media_analysis": row["media_analysis"],
            "bot_id": row["bot_id"],
            "topic_id": row["topic_id"],
            "charge": row["charge"],
            "direction": row["direction"],
        }

    def _handle_hydrate_fetch(self, args: tuple[Any, ...]) -> list[dict[str, Any]]:
        message_ids = [UUID(str(item)) for item in list(args[-1] or [])]
        return [dict(self._messages_by_id[mid]) for mid in message_ids if mid in self._messages_by_id]

    def _handle_keyword_fetch(
        self, compact: str, args: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        query = str(args[0])
        bot_id = str(args[1])
        viewer_user_id = UUID(str(args[2]))
        participant_ids = self._participant_ids_from_args(args, idx=3)
        topic_id, thread_owner_user_id, dyad_id, idx = self._parse_scope_params(
            compact, args, start_idx=4
        )
        limit = int(args[idx])

        rows = self._base_visible_rows(
            participant_ids=participant_ids,
            viewer_user_id=viewer_user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            thread_owner_user_id=thread_owner_user_id,
            dyad_id=dyad_id,
        )
        query_terms = self._keyword_terms(query)
        scored = []
        for row in rows:
            score = sum(row["search_tsv_terms"].count(term) for term in query_terms)
            if score > 0:
                scored.append((score, row))
        scored.sort(
            key=lambda item: (
                -item[0],
                -item[1]["sent_at"].timestamp(),
                -item[1]["message_id"].int,
            )
        )
        result = []
        for rank, (score, row) in enumerate(scored[:limit], start=1):
            result.append(
                {
                    "message_id": row["message_id"],
                    "sent_at": row["sent_at"],
                    "keyword_score": float(score),
                    "keyword_rank": rank,
                }
            )
        return result

    def _handle_semantic_fetch(
        self, compact: str, args: tuple[Any, ...]
    ) -> list[dict[str, Any]]:
        query_vector = [float(item) for item in list(args[0] or [])]
        model = str(args[1])
        dimension = int(args[2])
        bot_id = str(args[3])
        viewer_user_id = UUID(str(args[4]))
        participant_ids = self._participant_ids_from_args(args, idx=5)
        topic_id, thread_owner_user_id, dyad_id, idx = self._parse_scope_params(
            compact, args, start_idx=6
        )
        limit = int(args[idx])

        rows = self._base_visible_rows(
            participant_ids=participant_ids,
            viewer_user_id=viewer_user_id,
            bot_id=bot_id,
            topic_id=topic_id,
            thread_owner_user_id=thread_owner_user_id,
            dyad_id=dyad_id,
        )
        scored = []
        for row in rows:
            embedding = self.message_embeddings.get(row["message_id"])
            if embedding is None:
                continue
            if embedding["model"] != model or embedding["dimension"] != dimension:
                continue
            distance = self._cosine_distance(query_vector, embedding["embedding"])
            scored.append((distance, row))
        scored.sort(
            key=lambda item: (
                item[0],
                -item[1]["sent_at"].timestamp(),
                -item[1]["message_id"].int,
            )
        )
        result = []
        for rank, (distance, row) in enumerate(scored[:limit], start=1):
            result.append(
                {
                    "message_id": row["message_id"],
                    "sent_at": row["sent_at"],
                    "cosine_distance": distance,
                    "semantic_rank": rank,
                }
            )
        return result

    def _insert_turn_audit_event(self, args: tuple[Any, ...]) -> dict[str, Any]:
        (
            turn_id,
            event_type,
            step,
            severity,
            occurred_at,
            duration_ms,
            actor,
            message,
            metadata,
            sensitive_metadata_encrypted,
        ) = args
        event_seq = 1 + sum(1 for row in self.turn_audit_events if row["turn_id"] == turn_id)
        row = {
            "id": uuid4(),
            "turn_id": turn_id,
            "event_seq": event_seq,
            "event_type": event_type,
            "step": step,
            "severity": severity,
            "occurred_at": occurred_at,
            "duration_ms": duration_ms,
            "actor": actor,
            "message": message,
            "metadata": _jsonb(metadata),
            "sensitive_metadata_encrypted": sensitive_metadata_encrypted,
        }
        self.turn_audit_events.append(row)
        return {"id": row["id"], "event_seq": event_seq}

    def _insert_tool_call(self, args: tuple[Any, ...]) -> None:
        (
            turn_id,
            tool_name,
            arguments,
            result,
            called_at,
            duration_ms,
            kind,
            summary,
        ) = args
        self.tool_calls.append(
            {
                "id": uuid4(),
                "turn_id": turn_id,
                "tool_name": tool_name,
                "arguments": _jsonb(arguments),
                "result": _jsonb(result),
                "called_at": called_at,
                "duration_ms": duration_ms,
                "kind": kind,
                "summary": summary,
            }
        )

    def _unsupported(
        self,
        kind: str,
        compact: str,
        args: tuple[Any, ...],
        *,
        fallback: Any,
    ) -> Any:
        self.infrastructure_issues.append(
            _InfrastructureIssue(
                kind=kind,
                sql=compact,
                args=[repr(arg) for arg in args],
                note="Unsupported SQL shape hit the dedicated M4 fake pool.",
            )
        )
        return fallback

    def _keyword_terms(self, text: str) -> list[str]:
        return [term for term in re.split(r"[^a-z0-9]+", text.casefold()) if term]

    def _cosine_distance(self, left: list[float], right: list[float]) -> float:
        if not left or not right or len(left) != len(right):
            return 1.0
        dot = sum(a * b for a, b in zip(left, right, strict=False))
        left_norm = sum(a * a for a in left) ** 0.5
        right_norm = sum(b * b for b in right) ** 0.5
        if left_norm == 0.0 or right_norm == 0.0:
            return 1.0
        cosine_similarity = dot / (left_norm * right_norm)
        return 1.0 - cosine_similarity
