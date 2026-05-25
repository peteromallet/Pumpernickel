"""Sprint 3 — live per-turn caller.

Contract (:class:`TurnCaller`): given a fresh user transcript, return one
:class:`~app.services.live.schemas.TurnEmission`.  The orchestrator then
applies it atomically to the DB.

Ships two impls:

* :class:`StubTurnCaller` — deterministic stub for dev/no-key runs.
  Generates a plausible selected-bot utterance, advances coverage on the
  current item, and notes a single "fact".  Wire protocol is identical to
  the real Haiku caller.
* :class:`AnthropicHaikuTurnCaller` — calls Claude Haiku 4.5 with the
  agenda prompt-cached.  Selected when ``LIVE_VOICE_TURN_PROVIDER=anthropic``.
* :class:`DeepseekTurnCaller` — calls DeepSeek JSON mode.  Used as the
  production fallback when Anthropic is present but unavailable.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Protocol
from uuid import UUID

from app.services.live.schemas import (
    CoverageDelta,
    TurnEmission,
    TurnNote,
    TurnRequest,
)
from app.services.live.bot_profile import (
    format_live_bot_profile,
    live_bot_profile_context,
    user_from_live_row,
)

logger = logging.getLogger(__name__)


class TurnCaller(Protocol):
    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission: ...


class FallbackTurnCaller:
    """Try a primary turn caller, then a secondary caller before surfacing failure."""

    def __init__(
        self,
        primary: TurnCaller,
        fallback: TurnCaller,
        *,
        primary_name: str,
        fallback_name: str,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.primary_name = primary_name
        self.fallback_name = fallback_name

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        try:
            return await self.primary.call(request, context)
        except Exception as exc:
            logger.warning(
                "turn_loop: %s turn caller failed; falling back to %s: %s",
                self.primary_name,
                self.fallback_name,
                exc,
            )
            return await self.fallback.call(request, context)


def select_turn_caller() -> "TurnCaller":
    provider = (os.environ.get("LIVE_VOICE_TURN_PROVIDER") or "").strip().lower()
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    deepseek_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    has_anthropic = anthropic_key.startswith("sk-ant-") and "stub" not in anthropic_key
    has_deepseek = bool(deepseek_key) and "stub" not in deepseek_key.lower()

    if provider == "stub":
        return StubTurnCaller()
    if provider == "deepseek":
        return DeepseekTurnCaller()
    if provider == "anthropic":
        if has_deepseek:
            return FallbackTurnCaller(
                AnthropicHaikuTurnCaller(),
                DeepseekTurnCaller(),
                primary_name="anthropic",
                fallback_name="deepseek",
            )
        return AnthropicHaikuTurnCaller()
    # Auto-select: keep the designed Anthropic path, but never let an
    # unavailable Anthropic account block live replies when DeepSeek is configured.
    if has_anthropic and has_deepseek:
        return FallbackTurnCaller(
            AnthropicHaikuTurnCaller(),
            DeepseekTurnCaller(),
            primary_name="anthropic",
            fallback_name="deepseek",
        )
    if has_anthropic:
        return AnthropicHaikuTurnCaller()
    if has_deepseek:
        return DeepseekTurnCaller()
    return StubTurnCaller()


async def load_turn_context(pool: Any, session_id: UUID) -> dict[str, Any]:
    """Pull what Haiku needs to plan a turn: conversation row, current item,
    last few transcript_turns, items still pending.
    """
    context: dict[str, Any] = {"session_id": str(session_id)}
    try:
        conv = await pool.fetchrow(
            """
            SELECT id, user_id, bot_id, prep_summary, current_item_id,
                   session_fields, status
            FROM mediator.conversations
            WHERE id = $1
            """,
            session_id,
        )
    except Exception:
        logger.warning("turn_loop: failed to load conversation row", exc_info=True)
        return context
    if conv is None:
        return context
    context["conversation"] = {k: v for k, v in dict(conv).items()}
    bot_id = context["conversation"].get("bot_id")
    user_id = context["conversation"].get("user_id")

    if bot_id is not None:
        user = None
        if user_id is not None:
            try:
                user_row = await pool.fetchrow(
                    """
                    SELECT id, name, phone, timezone, onboarding_state,
                           pacing_preferences, pregnancy_edd, pregnancy_dating_basis,
                           pregnancy_lmp_date, pregnancy_scan_date,
                           pregnancy_scan_corrected_at, pregnancy_started_at,
                           pregnancy_ended_at, pregnancy_outcome
                    FROM users
                    WHERE id = $1
                    """,
                    user_id,
                )
                user = user_from_live_row(user_id, user_row)
            except Exception:
                logger.warning("turn_loop: failed to load user row", exc_info=True)
        context["bot_profile"] = live_bot_profile_context(bot_id, user=user)

    try:
        items = await pool.fetch(
            """
            SELECT id, title, intent, ask, done_when, status, priority, order_hint
            FROM mediator.conversation_items
            WHERE conversation_id = $1
            ORDER BY order_hint, created_at
            """,
            session_id,
        )
        context["items"] = [dict(r) for r in items]
    except Exception:
        context["items"] = []

    try:
        last_turns = await pool.fetch(
            """
            SELECT speaker_role, speaker_label, text, ts
            FROM mediator.transcript_turns
            WHERE conversation_id = $1
            ORDER BY ts DESC
            LIMIT 8
            """,
            session_id,
        )
        context["last_turns"] = list(reversed([dict(r) for r in last_turns]))
    except Exception:
        context["last_turns"] = []

    return context


async def apply_emission(pool: Any, session_id: UUID, emission: TurnEmission) -> None:
    """Atomic apply of a validated TurnEmission to the DB.

    * Coverage: bump conversation_items.status (+ coverage fields) for each delta.
    * new_items: insert as conversation_items with kind in {dynamic, thread}.
    * notes: insert as conversation_notes rows.
    * session_fields_patch: shallow-merge into conversations.session_fields.
    * route_to_item_id: update conversations.current_item_id.

    Maps Haiku's stable string item ids to DB UUIDs via title lookup for
    coverage on planned items (the stub uses titles); the real Haiku
    caller will be given UUIDs in its prompt so it returns them directly.
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            for delta in emission.coverage:
                # Coverage targets may arrive as either a UUID string or a
                # planning-time id (e.g. "must_anchor").  Resolve via UUID
                # first, fall back to title prefix match for the stub.
                target_uuid = _maybe_uuid(delta.item_id)
                if target_uuid is None:
                    # Stub-id path: match against the title prefix that
                    # StubAgendaProducer baked in.
                    row = await conn.fetchrow(
                        """
                        SELECT id FROM mediator.conversation_items
                        WHERE conversation_id = $1 AND title ILIKE $2 || '%'
                        ORDER BY order_hint
                        LIMIT 1
                        """,
                        session_id,
                        delta.item_id[:8],  # best-effort
                    )
                    if row is None:
                        continue
                    target_uuid = row["id"]
                await conn.execute(
                    """
                    UPDATE mediator.conversation_items
                    SET status = $2,
                        coverage_evidence_quote = COALESCE($3, coverage_evidence_quote),
                        coverage_summary = COALESCE($4, coverage_summary),
                        covered_at = CASE WHEN $2 = 'covered' THEN now() ELSE covered_at END
                    WHERE id = $1
                    """,
                    target_uuid,
                    delta.status,
                    delta.evidence_quote,
                    delta.summary,
                )

            for new in emission.new_items:
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_items
                        (conversation_id, kind, title, intent, ask, done_when,
                         priority, speaker_scope, coverage_evidence_required)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                    """,
                    session_id,
                    new.kind,
                    new.title,
                    new.intent,
                    new.ask,
                    new.done_when,
                    new.priority,
                    new.speaker_scope,
                    new.coverage_evidence_required,
                )

            for note in emission.notes:
                # conversation_notes has no `kind` column; encode kind as a
                # short text prefix so we don't lose it.
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_notes
                        (conversation_id, text)
                    VALUES ($1, $2)
                    """,
                    session_id,
                    f"[{note.kind}] {note.text}",
                )

            if emission.session_fields_patch:
                # Shallow-merge: read current, patch, write back.
                row = await conn.fetchrow(
                    "SELECT session_fields FROM mediator.conversations WHERE id = $1",
                    session_id,
                )
                current = dict(row["session_fields"] or {}) if row else {}
                current.update(emission.session_fields_patch)
                import json as _json
                await conn.execute(
                    "UPDATE mediator.conversations SET session_fields = $2 WHERE id = $1",
                    session_id,
                    _json.dumps(current),
                )

            if emission.route_to_item_id:
                target = _maybe_uuid(emission.route_to_item_id)
                if target is not None:
                    await conn.execute(
                        "UPDATE mediator.conversations SET current_item_id = $2 WHERE id = $1",
                        session_id,
                        target,
                    )


def _maybe_uuid(value: str) -> UUID | None:
    try:
        return UUID(value)
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Stub impl.
# --------------------------------------------------------------------------- #


class StubTurnCaller:
    """Deterministic stub used in dev / tests / no-Anthropic-key local runs."""

    def __init__(self) -> None:
        self._turn_count = 0

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        self._turn_count += 1
        items = context.get("items") or []
        current_id = (context.get("conversation") or {}).get("current_item_id")
        current_title = "this"
        next_item_id = None
        for item in items:
            if item["id"] == current_id:
                current_title = item["title"]
            if item["status"] in ("pending", "active") and str(item["id"]) != str(current_id):
                next_item_id = str(item["id"])
                break

        user_text = (request.user_transcript_final or "").strip()
        utterance = (
            f"Thanks for sharing that. I hear you saying: \"{user_text[:120]}\". "
            f"Let's stay with this for a moment before we move on."
        )
        coverage: list[CoverageDelta] = []
        if current_id:
            coverage.append(
                CoverageDelta(
                    item_id=str(current_id),
                    status="covered" if self._turn_count >= 1 else "active",
                    evidence_quote=user_text[:200] or "(no transcript)",
                    summary=f"Stub coverage update for {current_title!r}.",
                )
            )
        notes: list[TurnNote] = [
            TurnNote(kind="fact", text=f"Turn {self._turn_count}: user said {user_text[:80]!r}.")
        ]
        return TurnEmission(
            utterance=utterance,
            route_to_item_id=next_item_id,
            coverage=coverage,
            notes=notes,
        )


# --------------------------------------------------------------------------- #
# Real impl: Anthropic Haiku 4.5 with prompt-cached agenda.
# --------------------------------------------------------------------------- #


class AnthropicHaikuTurnCaller:
    """Real Haiku caller; not exercised in the stub-key local run.

    Implementation outline (left intentionally tight so it can be
    iterated against a real key):

    * Build a system prompt that loads the agenda + last_turns + current
      item details (prompt-cached via `cache_control: {type:"ephemeral"}`).
    * Tool schema: a single tool named ``emit_live_turn`` whose JSON
      schema mirrors :class:`TurnEmission`.
    * Force tool use; parse the resulting tool_use block; validate via
      :class:`TurnEmission`; return.
    """

    def __init__(self, *, model: str = "claude-haiku-4-5-20251001") -> None:
        self._model = model

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        import json
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"anthropic SDK unavailable: {exc}") from exc

        client = anthropic.AsyncAnthropic()
        conv = context.get("conversation") or {}
        bot_profile = context.get("bot_profile") or {"bot_id": conv.get("bot_id")}
        items = context.get("items") or []
        last_turns = context.get("last_turns") or []

        system = [
            {
                "type": "text",
                "text": (
                    "You are the selected Veas live-voice bot. Always respond with the "
                    "emit_live_turn tool; never use plain text. The agenda below is the "
                    "checklist you must drive. Follow the selected bot profile, scope, "
                    "and style. Stay grounded, short utterances (<= 60 words), and only "
                    "mark an item 'covered' when you can quote the user."
                ),
            },
            {
                "type": "text",
                "text": "SELECTED BOT PROFILE:\n" + format_live_bot_profile(bot_profile),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": f"PREP SUMMARY:\n{conv.get('prep_summary') or '(no prep summary)'}",
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "AGENDA:\n" + json.dumps([{
                    "id": str(i["id"]),
                    "title": i["title"],
                    "status": i["status"],
                    "priority": i["priority"],
                    "intent": i.get("intent"),
                    "ask": i.get("ask"),
                    "done_when": i.get("done_when"),
                } for i in items], indent=2),
                "cache_control": {"type": "ephemeral"},
            },
        ]
        user_content = "RECENT TRANSCRIPT:\n" + "\n".join(
            f"- [{t['speaker_role']}] {t['text']}" for t in last_turns[-6:]
        ) + f"\n\nLATEST USER UTTERANCE:\n{request.user_transcript_final}"

        tool = {
            "name": "emit_live_turn",
            "description": "Emit exactly one structured turn output.",
            "input_schema": TurnEmission.model_json_schema(),
        }
        resp = await client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit_live_turn"},
            messages=[{"role": "user", "content": user_content}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "emit_live_turn":
                return TurnEmission.model_validate(block.input)
        raise RuntimeError("Haiku did not emit a tool_use; check tool_choice settings")


# --------------------------------------------------------------------------- #
# Deepseek impl: JSON-mode chat completion (no tool_choice forcing needed).
# --------------------------------------------------------------------------- #


class DeepseekTurnCaller:
    """Deepseek per-turn caller.

    Uses Deepseek's OpenAI-compatible /chat/completions with
    ``response_format={"type":"json_object"}`` and prompt-side schema injection.
    Selected by ``LIVE_VOICE_TURN_PROVIDER=deepseek`` or auto-selected when
    a Deepseek key is present and the Anthropic key is missing/placeholder.
    """

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model

    async def call(self, request: TurnRequest, context: dict[str, Any]) -> TurnEmission:
        import json

        import httpx

        from app.config import get_settings

        settings = get_settings()
        if settings.deepseek_api_key is None:
            raise RuntimeError("DEEPSEEK_API_KEY not configured")
        model = self._model or settings.deepseek_conversational_model

        conv = context.get("conversation") or {}
        bot_profile = context.get("bot_profile") or {"bot_id": conv.get("bot_id")}
        items = context.get("items") or []
        last_turns = context.get("last_turns") or []

        schema = TurnEmission.model_json_schema()
        agenda = [
            {
                "id": str(i["id"]),
                "title": i["title"],
                "status": i["status"],
                "priority": i["priority"],
                "intent": i.get("intent"),
                "ask": i.get("ask"),
                "done_when": i.get("done_when"),
            }
            for i in items
        ]
        system_text = (
            "You are the selected Veas live-voice bot. Respond with ONE JSON "
            "object that validates against OUTPUT_SCHEMA below; no prose, no "
            "markdown, no code fences. Follow the selected bot profile, scope, "
            "and style. Stay grounded, keep utterances <= 60 words, and only "
            "mark an item 'covered' when you can quote the user.\n\n"
            f"OUTPUT_SCHEMA:\n{json.dumps(schema)}\n\n"
            f"SELECTED BOT PROFILE:\n{format_live_bot_profile(bot_profile)}\n\n"
            f"PREP SUMMARY:\n{conv.get('prep_summary') or '(no prep summary)'}\n\n"
            f"AGENDA:\n{json.dumps(agenda, indent=2)}"
        )
        user_content = (
            "RECENT TRANSCRIPT:\n"
            + "\n".join(f"- [{t['speaker_role']}] {t['text']}" for t in last_turns[-6:])
            + f"\n\nLATEST USER UTTERANCE:\n{request.user_transcript_final}"
        )
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            "stream": False,
        }
        if settings.deepseek_reasoning_effort:
            payload["reasoning_effort"] = settings.deepseek_reasoning_effort

        async with httpx.AsyncClient(timeout=settings.provider_call_timeout_seconds) as client:
            resp = await client.post(
                f"{settings.deepseek_base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"Deepseek returned unexpected payload: {data!r}") from exc
        return TurnEmission.model_validate_json(content)
