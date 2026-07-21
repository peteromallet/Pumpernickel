"""Bridge: normalizer → M1 reflection entry creation/correction services.

Integrates the bounded ``ReflectionNormalizer`` output with the existing
``ReflectionStore.create_entry()`` / ``correct_entry()`` APIs so that
finalized sessions produce immutable, normalized reflection entries.

Placement rationale (T10)
--------------------------
Normalization is cheap (pure computation + a single message-text fetch).
We place it **synchronously at record time** — immediately after
``finalize_session`` — rather than in a separate claim+process worker
stage.  This keeps the worker seam simple (finalize + record in one
call) and avoids introducing a claim-then-normalize race where the
normalizer runs on stale session state.

The bridge is idempotent: if a current entry already exists for the
session, the call returns the existing entry rather than creating a
duplicate revision.  This makes retry-after-failure safe and keeps the
``create_entry`` path free of spurious revision bumps.

Design contract
---------------
* **Single responsibility**: fetch message texts, normalize, map to the
  envelope shape expected by ``validate_entry_payload``, call
  ``ReflectionStore.create_entry()``.
* **Idempotent**: ``check_entry_exists`` before creating.  Sessions that
  already have a current entry are skipped (existing entry returned).
* **Immutable**: entries are created once; corrections go through
  ``correct_entry()`` which appends a new revision.
* **Evidence-bound**: the normalizer's missing-field restraint is
  preserved — the envelope only contains what the source messages support.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from app.reflections.normalizer import (
    NormalizedReflection,
    ReflectionNormalizer,
)
from app.services.reflections import (
    ReflectionEntry,
    ReflectionStore,
    SessionNotFoundError,
)

logger = logging.getLogger(__name__)

# Singleton normalizer — stateless, safe to reuse.
_normalizer = ReflectionNormalizer()


# ── Public API ──────────────────────────────────────────────────────────────


async def normalize_and_create_entry(
    store: ReflectionStore,
    *,
    user_id: UUID,
    session_id: UUID,
    bot_id: str,
    pool: Any,
    processor_version: str | None = None,
    created_by_turn_id: UUID | None = None,
) -> ReflectionEntry | None:
    """Normalize a finalized session's source messages and create its entry.

    This is the primary integration seam: it fetches message texts for the
    session's ``source_message_ids``, runs the bounded normalizer, maps the
    result to the envelope format, and calls ``ReflectionStore.create_entry()``.

    **Idempotency**: if the session already has a current entry (revision ≥ 1,
    meaning the leaf row that no successor references), this function returns the existing
    entry without creating a new revision.  This makes retry-after-failure
    safe.

    **Missing-field restraint**: the normalizer's contract is preserved —
    only fields with source-message evidence appear in the envelope.
    Unsupported template fields are recorded in ``fields_unsupported``
    within ``template_data._normalizer_meta``.

    Args:
        store: ``ReflectionStore`` instance backed by the same pool.
        user_id: Owning user.
        session_id: The session (must be finalized or processed).
        bot_id: Bot identifier.
        pool: Database pool for fetching message texts.
        processor_version: Optional version tag for the entry.
        created_by_turn_id: Optional turn that triggered creation.

    Returns:
        The created (or existing) ``ReflectionEntry``, or ``None`` if the
        session has no source messages to normalize.

    Raises:
        SessionNotFoundError: If the session does not exist or is not owned
            by *user_id*.
        ValueError: If the session is not in a status that allows entry
            creation (must be ``finalizing`` or ``processed``).
    """
    # ── Idempotency check ──────────────────────────────────────────────
    existing = await store.get_current_entry(
        user_id=user_id,
        session_id=session_id,
    )
    if existing is not None:
        logger.debug(
            "normalize_and_create_entry: entry already exists for session=%s "
            "entry=%s rev=%s — returning existing (idempotent)",
            session_id,
            existing.id,
            existing.revision_number,
        )
        return existing

    # ── Load session state ──────────────────────────────────────────────
    session = await store.get_session(user_id=user_id, session_id=session_id)
    if session is None:
        raise SessionNotFoundError(
            f"Session {session_id} not found for user {user_id}"
        )

    if not session.source_message_ids:
        logger.info(
            "normalize_and_create_entry: session=%s has no source messages — "
            "nothing to normalize",
            session_id,
        )
        return None

    # ── Fetch message texts ─────────────────────────────────────────────
    message_texts = await _fetch_message_texts(pool, session.source_message_ids)
    if not message_texts:
        logger.warning(
            "normalize_and_create_entry: no message texts found for session=%s "
            "source_message_ids=%s",
            session_id,
            [str(m) for m in session.source_message_ids],
        )
        return None

    # ── Normalize ───────────────────────────────────────────────────────
    normalized: NormalizedReflection = _normalizer.normalize(
        source_message_ids=session.source_message_ids,
        source_message_texts=message_texts,
        template_key=session.template_key or "freeform_reflection",
    )

    # ── Map to envelope ─────────────────────────────────────────────────
    envelope = _normalized_to_envelope(normalized)

    # ── Create the entry ────────────────────────────────────────────────
    entry = await store.create_entry(
        user_id=user_id,
        session_id=session_id,
        bot_id=bot_id,
        topic_id=session.topic_id,
        template_key=session.template_key or "freeform_reflection",
        temporal_scope=session.temporal_scope,
        phase=session.phase,
        period_start=session.period_start,
        period_end=session.period_end,
        timezone_=session.timezone,
        source_message_ids=session.source_message_ids,
        payload=envelope,
        plaintext_searchable=normalized.shared.plaintext_summary,
        summary=normalized.shared.plaintext_summary,
        schema_version=normalized.schema_version,
        processor_version=processor_version,
        created_by_turn_id=created_by_turn_id,
    )

    logger.info(
        "normalize_and_create_entry: created entry=%s session=%s template=%s "
        "confidence=%.2f",
        entry.id,
        session_id,
        normalized.template_key,
        normalized.extraction_confidence,
    )
    return entry


# ── Envelope mapping ────────────────────────────────────────────────────────


def _normalized_to_envelope(normalized: NormalizedReflection) -> dict[str, Any]:
    """Map a ``NormalizedReflection`` to the envelope dict for ``create_entry``.

    The envelope shape is defined by ``reflection_templates._SHARED_ENVELOPE_KEYS``.
    We populate only the fields that the normalizer has evidence for; all
    others are set to their zero values (None / [] / {}).

    Mapping:
        * ``summary`` ← ``shared.plaintext_summary``
        * ``signals`` ← ``{"sentiment": shared.detected_sentiment}`` (if present)
        * ``template_data`` ← normalized ``template_data``, augmented with
          ``_normalizer_meta`` containing ``fields_unsupported``,
          ``extraction_confidence``, ``extracted_topics``, and
          ``explicit_user_statements``.
    """
    shared = normalized.shared

    # Build template_data with normalizer metadata so downstream consumers
    # can see which fields had evidence and which didn't.
    template_data: dict[str, Any] = dict(normalized.template_data)
    template_data["_normalizer_meta"] = {
        "fields_unsupported": list(normalized.fields_unsupported),
        "extraction_confidence": normalized.extraction_confidence,
        "extracted_topics": list(shared.extracted_topics),
        "explicit_user_statements": list(shared.explicit_user_statements),
    }

    signals: dict[str, Any] = {}
    if shared.detected_sentiment is not None:
        signals["sentiment"] = shared.detected_sentiment

    envelope: dict[str, Any] = {
        "summary": shared.plaintext_summary,
        "facts": shared.explicit_user_statements if shared.explicit_user_statements else [],
        "events": [],
        "decisions": [],
        "priorities": [],
        "wins": [],
        "blockers": [],
        "open_loops": [],
        "questions": [],
        "signals": signals,
        "template_data": template_data,
    }

    return envelope


# ── Internal helpers ────────────────────────────────────────────────────────


async def _fetch_message_texts(
    pool: Any,
    message_ids: list[UUID],
) -> list[str]:
    """Fetch message content for a list of message IDs, preserving order.

    Returns a list of content strings in the same order as *message_ids*.
    Messages that cannot be found are silently omitted.
    """
    if not message_ids:
        return []

    rows = await pool.fetch(
        """
        SELECT id, content
        FROM messages
        WHERE id = ANY($1::uuid[])
          AND deleted_at IS NULL
        """,
        message_ids,
    )

    # Build lookup, then preserve input order.
    content_map: dict[UUID, str] = {}
    for row in rows:
        rid = row["id"]
        if isinstance(rid, UUID):
            content = row.get("content")
            if content:
                content_map[rid] = content

    result: list[str] = []
    for mid in message_ids:
        if mid in content_map:
            result.append(content_map[mid])

    return result
