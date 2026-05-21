"""Sprint 1 — Opus-driven prep step.

Produces a structured agenda (see :class:`app.services.live.schemas.Agenda`)
for a chosen bot + user, validates it against the schema, then persists the
session envelope to ``mediator.conversations`` and the items to
``mediator.conversation_items``.

The LLM call is abstracted behind :class:`AgendaProducer`. Production wires
this to Anthropic Opus via function calling; tests inject a stub. Both keep
the call site identical — schema validation is the gate, not the source.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.services.live.schemas import (
    Agenda,
    AgendaItem,
    PrepRequest,
    PrepResult,
)
from app.services.live.bot_profile import (
    format_live_bot_profile,
    live_bot_profile_context,
    user_from_live_row,
)

logger = logging.getLogger(__name__)


def select_agenda_producer() -> "AgendaProducer":
    """Pick the agenda producer based on env.

    * ``LIVE_VOICE_PREP_PROVIDER=stub`` → :class:`StubAgendaProducer`.
    * ``LIVE_VOICE_PREP_PROVIDER=anthropic`` → :class:`AnthropicOpusAgendaProducer`.
    * ``LIVE_VOICE_PREP_PROVIDER=deepseek`` → :class:`DeepseekAgendaProducer`.
    * Auto-select when unset: real Anthropic key wins, else Deepseek if its
      key is real, else stub.
    """
    provider = (os.environ.get("LIVE_VOICE_PREP_PROVIDER") or "").strip().lower()
    anthropic_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    deepseek_key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    has_anthropic = anthropic_key.startswith("sk-ant-") and "stub" not in anthropic_key
    has_deepseek = bool(deepseek_key) and "stub" not in deepseek_key.lower()

    if provider == "stub":
        return StubAgendaProducer()
    if provider == "anthropic":
        return AnthropicOpusAgendaProducer()
    if provider == "deepseek":
        return DeepseekAgendaProducer()
    if has_anthropic:
        return AnthropicOpusAgendaProducer()
    if has_deepseek:
        return DeepseekAgendaProducer()
    return StubAgendaProducer()


class AgendaProducer(Protocol):
    """Anything that turns a :class:`PrepRequest` into a validated :class:`Agenda`.

    Real impls call Anthropic with prompt-cached system + tool definition;
    test impls return canned fixtures.  Both must return a model that has
    already passed :class:`Agenda` validation — this protocol is purely a
    boundary marker, not a behavior contract.
    """

    async def __call__(self, request: PrepRequest, context: dict[str, Any]) -> Agenda: ...


async def gather_prep_context(pool: Any, user_id: UUID, bot_id: str) -> dict[str, Any]:
    """Collect the inputs Opus needs to build a useful agenda.

    Pulls:
    * user record (timezone, style_notes)
    * bot binding (confirm the caller actually owns this bot)
    * recent distillations for the user+bot scope (last 20)
    * existing themes (so the agenda can cluster under them)

    Returns a dict suitable for passing to the AgendaProducer. Failures in
    any individual section are non-fatal — Opus can plan without the full
    context, just less well.
    """
    context: dict[str, Any] = {"user_id": str(user_id), "bot_id": bot_id}

    try:
        user_row = await pool.fetchrow(
            """
            SELECT id, name, phone, timezone, style_notes, onboarding_state,
                   pacing_preferences, pregnancy_edd, pregnancy_dating_basis,
                   pregnancy_lmp_date, pregnancy_scan_date, pregnancy_scan_corrected_at,
                   pregnancy_started_at, pregnancy_ended_at, pregnancy_outcome
            FROM users
            WHERE id = $1
            """,
            user_id,
        )
        if user_row is not None:
            context["user"] = {
                "name": user_row["name"],
                "timezone": user_row["timezone"],
                "style_notes": user_row["style_notes"],
            }
            context["bot_profile"] = live_bot_profile_context(
                bot_id,
                user=user_from_live_row(user_id, user_row),
            )
    except Exception:
        logger.warning("prep: failed to load user record", exc_info=True)
    if "bot_profile" not in context:
        context["bot_profile"] = live_bot_profile_context(bot_id)

    try:
        themes = await pool.fetch(
            """
            SELECT id, slug, label
            FROM themes
            WHERE user_id = $1 AND bot_id = $2
            ORDER BY updated_at DESC NULLS LAST, created_at DESC
            LIMIT 20
            """,
            user_id,
            bot_id,
        )
        context["themes"] = [
            {"id": str(t["id"]), "slug": t["slug"], "label": t["label"]} for t in themes
        ]
    except Exception:
        logger.info("prep: themes table not queryable for this user/bot", exc_info=True)
        context["themes"] = []

    try:
        distillations = await pool.fetch(
            """
            SELECT id, content, kind, theme_id, created_at
            FROM distillations
            WHERE user_id = $1 AND bot_id = $2
            ORDER BY created_at DESC
            LIMIT 20
            """,
            user_id,
            bot_id,
        )
        context["distillations"] = [
            {
                "id": str(d["id"]),
                "content": d["content"],
                "kind": d.get("kind"),
                "theme_id": str(d["theme_id"]) if d["theme_id"] else None,
            }
            for d in distillations
        ]
    except Exception:
        logger.info("prep: distillations not queryable for this user/bot", exc_info=True)
        context["distillations"] = []

    return context


async def produce_agenda(
    pool: Any,
    request: PrepRequest,
    *,
    producer: AgendaProducer,
) -> PrepResult:
    """End-to-end prep: gather context, call producer, persist atomically.

    Persists to ``mediator.conversations`` + ``mediator.conversation_items``
    in a single transaction so a partial agenda never lands. Sets
    ``current_item_id`` on the conversation row to the UUID of the row
    matching ``agenda.first_item_id``.
    """
    user_uuid = UUID(request.user_id)
    context = await gather_prep_context(pool, user_uuid, request.bot_id)

    agenda = await producer(request, context)  # already schema-validated by the caller

    # Resolve theme_slug -> theme_id lookups up-front so the transaction is
    # short. Themes not present are silently dropped (Opus may invent slugs).
    theme_slugs = {item.theme_slug for item in agenda.items if item.theme_slug}
    theme_id_by_slug: dict[str, UUID] = {}
    if theme_slugs:
        try:
            rows = await pool.fetch(
                "SELECT id, slug FROM themes WHERE user_id = $1 AND slug = ANY($2::text[])",
                user_uuid,
                list(theme_slugs),
            )
            theme_id_by_slug = {r["slug"]: r["id"] for r in rows}
        except Exception:
            logger.warning("prep: theme lookup failed; theme_id=NULL on every item", exc_info=True)

    session_id = uuid4()
    item_uuid_by_id: dict[str, UUID] = {item.id: uuid4() for item in agenda.items}
    current_item_uuid = item_uuid_by_id[agenda.first_item_id]
    mode = "steered" if (request.steering_text or "").strip() else "open"

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                """
                INSERT INTO mediator.conversations
                    (id, user_id, bot_id, mode, steering_text, status, prep_summary, current_item_id)
                VALUES ($1, $2, $3, $4, $5, 'ready', $6, NULL)
                """,
                session_id,
                user_uuid,
                request.bot_id,
                mode,
                request.steering_text,
                agenda.prep_summary,
            )
            for order_hint, item in enumerate(agenda.items):
                await conn.execute(
                    """
                    INSERT INTO mediator.conversation_items (
                        id, conversation_id, theme_id, kind, title, intent, ask,
                        done_when, next_item_ids, priority, speaker_scope,
                        coverage_evidence_required, order_hint
                    )
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::uuid[], $10, $11, $12, $13)
                    """,
                    item_uuid_by_id[item.id],
                    session_id,
                    theme_id_by_slug.get(item.theme_slug) if item.theme_slug else None,
                    item.kind,
                    item.title,
                    item.intent,
                    item.ask,
                    item.done_when,
                    [item_uuid_by_id[ref] for ref in item.next_item_ids],
                    item.priority,
                    item.speaker_scope,
                    item.coverage_evidence_required,
                    item.order_hint or order_hint,
                )
            # Now that all items exist, set current_item_id on the conversation row.
            await conn.execute(
                "UPDATE mediator.conversations SET current_item_id = $1 WHERE id = $2",
                current_item_uuid,
                session_id,
            )

    return PrepResult(
        session_id=str(session_id),
        agenda=agenda,
        items_persisted=len(agenda.items),
        current_item_id=str(current_item_uuid),
    )


# --------------------------------------------------------------------------- #
# Reference impl: a stub producer that returns a deterministic agenda based
# on the steering_text.  Used for tests AND as the v0 "no Anthropic key"
# fallback so Sprint 1 can be exercised end-to-end before live calls land.
# --------------------------------------------------------------------------- #


class StubAgendaProducer:
    """Deterministic agenda producer for tests + dev runs without an LLM key.

    Returns a 3-item agenda (one 'must', two 'should') that exercises the
    full schema: themes if any are in context, internal refs, partner scope.
    """

    async def __call__(self, request: PrepRequest, context: dict[str, Any]) -> Agenda:
        steering = (request.steering_text or "").strip() or "general check-in"
        bot_profile = context.get("bot_profile") or {"bot_id": request.bot_id}
        bot_name = bot_profile.get("display_name") or request.bot_id
        topic_slug = bot_profile.get("primary_topic_slug") or request.topic_slug or "general"
        participants_shape = bot_profile.get("participants_shape") or "solo"
        topic_label = str(topic_slug).replace("_", " ")
        themes = context.get("themes") or []
        first_theme_slug = themes[0]["slug"] if themes else None
        if participants_shape == "dyad":
            anchor_ask = "What is the relationship moment you most want us to understand?"
            context_title = "What each side is carrying"
            context_ask = "What happened around this, and what do you think it touched in each of you?"
            outcome_ask = "If this lands well, what would feel repaired or clearer between you?"
        elif topic_slug == "fitness":
            anchor_ask = "What part of your fitness or recovery do you want to sort out first?"
            context_title = "Current training reality"
            context_ask = "What has actually happened with your body, schedule, and energy lately?"
            outcome_ask = "What would a realistic next training step look like after this?"
        elif topic_slug == "pregnancy":
            anchor_ask = "What pregnancy question or feeling do you most want support with right now?"
            context_title = "What matters medically and emotionally"
            context_ask = "What has changed recently in your body, appointments, or worries?"
            outcome_ask = "What would help you feel steadier or clearer by the end?"
        elif topic_slug == "career":
            anchor_ask = "What work decision, tension, or ambition do you want to examine first?"
            context_title = "Work context and stakes"
            context_ask = "What has been happening at work, and what feels at stake for you?"
            outcome_ask = "What would a grounded next move look like tomorrow?"
        else:
            anchor_ask = "What habit or pattern do you want to work with first?"
            context_title = "Current pattern"
            context_ask = "What has been happening recently, and where does the pattern break down?"
            outcome_ask = "What small repeatable step would count as progress after this?"

        items = [
            AgendaItem(
                id="must_anchor",
                title=f"{bot_name}: focus the {topic_label} conversation",
                intent=f"Set a {topic_label} focus from the user's steering: {steering[:120]}",
                ask=anchor_ask,
                done_when="The user names a concrete topic or moment.",
                kind="planned",
                priority="must",
                speaker_scope="primary",
                coverage_evidence_required="explicit_answer",
                next_item_ids=["should_context", "should_outcome"],
                theme_slug=first_theme_slug,
                order_hint=0,
            ),
            AgendaItem(
                id="should_context",
                title=context_title,
                intent=f"Gather enough {topic_label} context for {bot_name} to respond in scope.",
                ask=context_ask,
                done_when="A short scene or trigger has been described.",
                kind="planned",
                priority="should",
                speaker_scope="primary",
                coverage_evidence_required="explicit_answer",
                order_hint=1,
            ),
            AgendaItem(
                id="should_outcome",
                title="What would count as useful",
                intent="Make the success criterion concrete so the session can close cleanly.",
                ask=outcome_ask,
                done_when="A concrete, observable next step or feeling has been named.",
                kind="planned",
                priority="should",
                speaker_scope="primary",
                coverage_evidence_required="concrete_decision",
                order_hint=2,
            ),
        ]
        agenda = Agenda(
            prep_summary=(
                f"{bot_name} prepared a {topic_label} brief from: {steering!r}. "
                "The agenda stays inside that persona and topic."
            ),
            items=items,
            first_item_id="must_anchor",
        )
        # Round-trip validates the schema (raises on internal-ref / uniqueness failures).
        return Agenda.model_validate(json.loads(agenda.model_dump_json()))


# --------------------------------------------------------------------------- #
# Real impl: Anthropic Opus via function-calling.
# --------------------------------------------------------------------------- #


class AnthropicOpusAgendaProducer:
    """Real Opus-driven agenda producer (gated on a real ANTHROPIC_API_KEY).

    Schema-validated: Opus is forced to emit the ``compose_agenda`` tool;
    we validate the tool input through :class:`Agenda` before returning.
    Tests skip this impl by selecting the stub.
    """

    def __init__(self, *, model: str = "claude-opus-4-7") -> None:
        self._model = model

    async def __call__(self, request: PrepRequest, context: dict[str, Any]) -> Agenda:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"anthropic SDK unavailable: {exc}") from exc

        client = anthropic.AsyncAnthropic()
        user_info = context.get("user") or {}
        bot_profile = context.get("bot_profile") or {"bot_id": request.bot_id}
        themes = context.get("themes") or []
        distillations = context.get("distillations") or []

        system = [
            {
                "type": "text",
                "text": (
                    "You are preparing a live voice conversation between a user and a "
                    "selected Veas bot. Produce a checklist agenda that matches the "
                    "selected bot's scope, persona, and conversational style. "
                    "Stay grounded in the user's recent state. Output ONLY via the "
                    "compose_agenda tool — never plain text. Items must include at "
                    "least one 'must' priority anchored to the user's stated intent. "
                    "The prep_summary, titles, intents, and likely asks are shown to "
                    "the user before the mic opens, so they must read as this selected "
                    "bot preparing this selected topic. Do not use relationship or "
                    "partner framing unless the selected profile is dyadic."
                ),
            },
            {
                "type": "text",
                "text": "SELECTED BOT PROFILE:\n" + format_live_bot_profile(bot_profile),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    "USER CONTEXT:\n"
                    f"- name: {user_info.get('name') or '(unknown)'}\n"
                    f"- timezone: {user_info.get('timezone') or '(unknown)'}\n"
                    f"- style_notes: {user_info.get('style_notes') or '(none)'}\n"
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "EXISTING THEMES:\n" + json.dumps(themes, indent=2),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "RECENT DISTILLATIONS:\n" + json.dumps(distillations, indent=2),
                "cache_control": {"type": "ephemeral"},
            },
        ]

        user_message = (
            f"Build an agenda for the conversation. "
            f"Steering text from user (may be empty): {request.steering_text or '(none)'}\n"
            f"Topic slug: {request.topic_slug or '(none)'}\n"
            f"Aim for 3-6 items. Include 1-2 must items anchored to the steering."
        )
        tool = {
            "name": "compose_agenda",
            "description": "Return the structured Agenda for this prep step.",
            "input_schema": Agenda.model_json_schema(),
        }
        resp = await client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": "compose_agenda"},
            messages=[{"role": "user", "content": user_message}],
        )
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "compose_agenda":
                return Agenda.model_validate(block.input)
        raise RuntimeError("Opus did not emit compose_agenda tool_use")


# --------------------------------------------------------------------------- #
# Deepseek impl: JSON-mode chat completion (no tool_choice forcing needed).
# --------------------------------------------------------------------------- #


class DeepseekAgendaProducer:
    """Agenda producer backed by Deepseek's OpenAI-compatible API.

    Uses ``response_format={"type":"json_object"}`` and schema-injection in
    the system prompt to avoid needing a forced tool_choice. The returned
    JSON object is validated through :class:`Agenda`.
    """

    def __init__(self, *, model: str | None = None) -> None:
        self._model = model

    async def __call__(self, request: PrepRequest, context: dict[str, Any]) -> Agenda:
        import httpx

        from app.config import get_settings

        settings = get_settings()
        if settings.deepseek_api_key is None:
            raise RuntimeError("DEEPSEEK_API_KEY not configured")
        # Allow an explicit prep override; default to the conversational model.
        model = (
            self._model
            or os.environ.get("LIVE_VOICE_PREP_MODEL")
            or settings.deepseek_conversational_model
        )

        user_info = context.get("user") or {}
        bot_profile = context.get("bot_profile") or {"bot_id": request.bot_id}
        themes = context.get("themes") or []
        distillations = context.get("distillations") or []
        schema = Agenda.model_json_schema()

        system_text = (
            "You are preparing a live voice conversation between a user and a "
            "selected Veas bot. Produce a checklist agenda that matches the "
            "selected bot's scope, persona, and conversational style. "
            "Stay grounded in the user's recent state.\n\n"
            "The `prep_summary`, item `title`, item `intent`, and item `ask` fields "
            "are shown to the user before the mic opens. Write them as this selected "
            "bot preparing this selected topic. Do not use relationship or partner "
            "framing unless the selected profile is dyadic.\n\n"
            "Respond with ONE JSON object that validates against "
            "OUTPUT_SCHEMA. No prose, no markdown, no code fences.\n"
            "Hard constraints:\n"
            "  * 1 to 24 items in `items`\n"
            "  * Each `id` is a unique slug matching ^[a-zA-Z0-9_-]+$\n"
            "  * `first_item_id` MUST match the id of one of the items\n"
            "  * At least one item MUST have priority='must'\n"
            "  * `next_item_ids` may only reference ids that exist in the list\n\n"
            f"OUTPUT_SCHEMA:\n{json.dumps(schema)}\n\n"
            "SELECTED BOT PROFILE:\n"
            f"{format_live_bot_profile(bot_profile)}\n\n"
            "USER CONTEXT:\n"
            f"- name: {user_info.get('name') or '(unknown)'}\n"
            f"- timezone: {user_info.get('timezone') or '(unknown)'}\n"
            f"- style_notes: {user_info.get('style_notes') or '(none)'}\n"
            "\n"
            f"EXISTING THEMES:\n{json.dumps(themes, indent=2)}\n\n"
            f"RECENT DISTILLATIONS:\n{json.dumps(distillations, indent=2)}"
        )
        user_message = (
            "Build an agenda for the conversation.\n"
            f"Steering text from user (may be empty): {request.steering_text or '(none)'}\n"
            f"Topic slug: {request.topic_slug or '(none)'}\n"
            "Aim for 3-6 items. Include 1-2 'must' items anchored to the steering."
        )

        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_text},
                {"role": "user", "content": user_message},
            ],
            "max_tokens": 2048,
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
        return Agenda.model_validate_json(content)
