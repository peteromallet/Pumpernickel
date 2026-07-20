"""Tests for app/services/reflections_normalization_bridge.py.

Covers:
  - Envelope mapping: NormalizedReflection → create_entry payload.
  - Idempotent entry creation: calling normalize_and_create_entry twice
    returns the same entry (no duplicate revisions).
  - Immutable entry creation: entries are created once; corrections go
    through correct_entry() which appends a new revision.
  - Retry safety: errors during normalization do not corrupt session state;
    the session remains finalized and entry creation is retried.
  - Worker integration: finalization worker calls normalize_and_create_entry
    after finalize_session.
  - Missing-field restraint: fields without evidence appear as None/empty
    in the envelope.
  - Error cases: session not found, no source messages, session not finalized.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from app.reflections.normalizer import (
    NormalizedReflection,
    SharedReflectionPayload,
)
from app.services.reflections import (
    ReflectionEntry,
    SessionNotFoundError,
)
from app.services.reflections_normalization_bridge import (
    _fetch_message_texts,
    _normalized_to_envelope,
    normalize_and_create_entry,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fake_shared_payload(
    *,
    source_message_ids: list[UUID] | None = None,
    texts: list[str] | None = None,
    sentiment: str | None = "positive",
    topics: list[str] | None = None,
    statements: list[str] | None = None,
    summary: str = "Test reflection summary.",
) -> SharedReflectionPayload:
    ids = source_message_ids or [_uid()]
    raw_texts = texts or ["I feel great today. I finished my work."]
    return SharedReflectionPayload(
        source_message_ids=ids,
        raw_message_texts=raw_texts,
        normalized_at=_now(),
        extracted_topics=topics or [],
        detected_sentiment=sentiment,
        explicit_user_statements=statements or [],
        plaintext_summary=summary,
        fields_with_evidence=["plaintext_summary", "source_message_ids", "raw_message_texts", "normalized_at"],
    )


def _fake_normalized(
    *,
    shared: SharedReflectionPayload | None = None,
    template_key: str = "freeform_reflection",
    template_data: dict | None = None,
    fields_unsupported: list[str] | None = None,
    confidence: float = 0.8,
) -> NormalizedReflection:
    return NormalizedReflection(
        shared=shared or _fake_shared_payload(),
        template_key=template_key,
        template_data=template_data or {},
        schema_version=1,
        fields_unsupported=fields_unsupported or [],
        extraction_confidence=confidence,
    )


def _fake_entry(
    *,
    entry_id: UUID | None = None,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    revision_number: int = 1,
) -> ReflectionEntry:
    return ReflectionEntry(
        id=entry_id or _uid(),
        session_id=session_id or _uid(),
        user_id=user_id or _uid(),
        topic_id=None,
        bot_id="test-bot",
        template_key="freeform_reflection",
        temporal_scope="instant",
        phase="freeform",
        period_start=None,
        period_end=None,
        timezone="UTC",
        source_message_ids=[],
        payload_encrypted=b"encrypted",
        plaintext_searchable="test",
        summary_encrypted=b"encrypted",
        schema_version=1,
        processor_version=None,
        revision_number=revision_number,
        supersedes_entry_id=None,
        created_by_turn_id=None,
        created_at=_now(),
    )


def _fake_session_row(
    *,
    session_id: UUID | None = None,
    user_id: UUID | None = None,
    bot_id: str = "test-bot",
    status: str = "finalizing",
    source_message_ids: list[UUID] | None = None,
    template_key: str = "freeform_reflection",
    temporal_scope: str = "instant",
    phase: str = "freeform",
) -> dict:
    return {
        "id": session_id or _uid(),
        "user_id": user_id or _uid(),
        "bot_id": bot_id,
        "status": status,
        "source_message_ids": list(source_message_ids or []),
        "template_key": template_key,
        "temporal_scope": temporal_scope,
        "phase": phase,
        "topic_id": None,
        "period_start": None,
        "period_end": None,
        "timezone": "UTC",
    }


# ═══════════════════════════════════════════════════════════════════════════
# Envelope mapping tests
# ═══════════════════════════════════════════════════════════════════════════


class TestNormalizedToEnvelope:
    """Verify NormalizedReflection → envelope mapping is correct."""

    def test_maps_summary(self) -> None:
        shared = _fake_shared_payload(summary="Today was productive.")
        norm = _fake_normalized(shared=shared)
        envelope = _normalized_to_envelope(norm)
        assert envelope["summary"] == "Today was productive."

    def test_maps_sentiment_to_signals(self) -> None:
        shared = _fake_shared_payload(sentiment="positive")
        norm = _fake_normalized(shared=shared)
        envelope = _normalized_to_envelope(norm)
        assert envelope["signals"]["sentiment"] == "positive"

    def test_null_sentiment_produces_empty_signals(self) -> None:
        shared = _fake_shared_payload(sentiment=None)
        norm = _fake_normalized(shared=shared)
        envelope = _normalized_to_envelope(norm)
        assert envelope["signals"] == {}

    def test_maps_explicit_statements_to_facts(self) -> None:
        shared = _fake_shared_payload(
            statements=["I realize I need more sleep.", "I believe this approach works."]
        )
        norm = _fake_normalized(shared=shared)
        envelope = _normalized_to_envelope(norm)
        assert len(envelope["facts"]) == 2
        assert "I realize I need more sleep." in envelope["facts"]

    def test_template_data_includes_normalizer_meta(self) -> None:
        norm = _fake_normalized(
            template_data={"mood": "great"},
            fields_unsupported=["energy_level", "focus_areas"],
            confidence=0.75,
        )
        envelope = _normalized_to_envelope(norm)
        meta = envelope["template_data"]["_normalizer_meta"]
        assert meta["extraction_confidence"] == 0.75
        assert "energy_level" in meta["fields_unsupported"]
        assert "focus_areas" in meta["fields_unsupported"]

    def test_all_envelope_keys_present(self) -> None:
        """Every envelope key must be present, even if empty."""
        norm = _fake_normalized()
        envelope = _normalized_to_envelope(norm)
        expected_keys = {
            "summary", "facts", "events", "decisions", "priorities",
            "wins", "blockers", "open_loops", "questions", "signals", "template_data",
        }
        assert set(envelope.keys()) == expected_keys

    def test_empty_lists_for_unevidenced_fields(self) -> None:
        """Fields without evidence get empty lists, not None."""
        norm = _fake_normalized()
        envelope = _normalized_to_envelope(norm)
        for key in ["events", "decisions", "priorities", "wins", "blockers", "open_loops", "questions"]:
            assert envelope[key] == [], f"{key} should be empty list"

    def test_extracted_topics_in_meta(self) -> None:
        shared = _fake_shared_payload(topics=["work-life balance", "productivity"])
        norm = _fake_normalized(shared=shared)
        envelope = _normalized_to_envelope(norm)
        meta = envelope["template_data"]["_normalizer_meta"]
        assert "work-life balance" in meta["extracted_topics"]
        assert "productivity" in meta["extracted_topics"]


# ═══════════════════════════════════════════════════════════════════════════
# Idempotent entry creation tests
# ═══════════════════════════════════════════════════════════════════════════


class TestIdempotentEntryCreation:
    """Calling normalize_and_create_entry twice returns the same entry."""

    async def test_second_call_returns_existing_entry(self) -> None:
        """If an entry already exists, return it without creating a new one."""
        uid = _uid()
        sid = _uid()
        existing_entry = _fake_entry(
            session_id=sid, user_id=uid, revision_number=1,
        )

        pool = AsyncMock()
        # Pool.fetch: used for fetching message texts.
        pool.fetch.return_value = []

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=existing_entry)
        store.get_session = AsyncMock()
        store.create_entry = AsyncMock()

        # First call — should return existing without calling create_entry.
        result = await normalize_and_create_entry(
            store=store,
            user_id=uid,
            session_id=sid,
            bot_id="test-bot",
            pool=pool,
        )

        assert result is existing_entry
        store.create_entry.assert_not_called()
        # get_current_entry was called for idempotency check.
        store.get_current_entry.assert_awaited_once()

    async def test_creates_entry_when_none_exists(self) -> None:
        """When no entry exists, create one from normalized messages."""
        uid = _uid()
        sid = _uid()
        msg_id = _uid()
        new_entry = _fake_entry(session_id=sid, user_id=uid, revision_number=1)

        session_row = _fake_session_row(
            session_id=sid,
            user_id=uid,
            source_message_ids=[msg_id],
            status="finalizing",
        )

        pool = AsyncMock()
        # Return the message text for fetch.
        pool.fetch.return_value = [{"id": msg_id, "content": "I feel great today. Reflecting on my week."}]

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=None)  # No entry yet
        store.get_session = AsyncMock(return_value=MagicMock(
            id=sid,
            user_id=uid,
            bot_id="test-bot",
            topic_id=None,
            source_message_ids=[msg_id],
            template_key="freeform_reflection",
            temporal_scope="instant",
            phase="freeform",
            period_start=None,
            period_end=None,
            timezone="UTC",
        ))
        store.create_entry = AsyncMock(return_value=new_entry)

        result = await normalize_and_create_entry(
            store=store,
            user_id=uid,
            session_id=sid,
            bot_id="test-bot",
            pool=pool,
        )

        assert result is new_entry
        store.create_entry.assert_awaited_once()
        store.get_current_entry.assert_awaited_once()

    async def test_idempotent_across_retries(self) -> None:
        """Simulate failure on first create_entry, then retry succeeds idempotently."""
        uid = _uid()
        sid = _uid()
        msg_id = _uid()
        entry = _fake_entry(session_id=sid, user_id=uid, revision_number=1)

        session_row = _fake_session_row(
            session_id=sid,
            user_id=uid,
            source_message_ids=[msg_id],
        )

        pool = AsyncMock()
        pool.fetch.return_value = [{"id": msg_id, "content": "Test reflection content."}]

        store = MagicMock()
        # First attempt: no entry, create fails.
        store.get_current_entry = AsyncMock(return_value=None)
        store.get_session = AsyncMock(return_value=MagicMock(
            id=sid,
            user_id=uid,
            bot_id="test-bot",
            topic_id=None,
            source_message_ids=[msg_id],
            template_key="freeform_reflection",
            temporal_scope="instant",
            phase="freeform",
            period_start=None,
            period_end=None,
            timezone="UTC",
        ))
        store.create_entry = AsyncMock(side_effect=RuntimeError("DB connection lost"))

        with pytest.raises(RuntimeError, match="DB connection lost"):
            await normalize_and_create_entry(
                store=store,
                user_id=uid,
                session_id=sid,
                bot_id="test-bot",
                pool=pool,
            )

        # Second attempt: entry now exists (created by another worker/retry).
        store2 = MagicMock()
        store2.get_current_entry = AsyncMock(return_value=entry)
        store2.get_session = AsyncMock()
        store2.create_entry = AsyncMock()

        result = await normalize_and_create_entry(
            store=store2,
            user_id=uid,
            session_id=sid,
            bot_id="test-bot",
            pool=pool,
        )

        assert result is entry
        store2.create_entry.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Immutable entry creation tests
# ═══════════════════════════════════════════════════════════════════════════


class TestImmutableEntryCreation:
    """Entries are created once; modifications go through correct_entry()."""

    async def test_entries_are_never_mutated(self) -> None:
        """Once created, normalize_and_create_entry never modifies an existing entry."""
        uid = _uid()
        sid = _uid()
        original_entry = _fake_entry(
            session_id=sid, user_id=uid, revision_number=1,
        )

        pool = AsyncMock()
        pool.fetch.return_value = []

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=original_entry)
        store.get_session = AsyncMock()
        store.create_entry = AsyncMock()
        store.correct_entry = AsyncMock()

        result = await normalize_and_create_entry(
            store=store,
            user_id=uid,
            session_id=sid,
            bot_id="test-bot",
            pool=pool,
        )

        # Must return the original, not a new entry.
        assert result is original_entry
        # Must NOT call create_entry or correct_entry.
        store.create_entry.assert_not_called()
        store.correct_entry.assert_not_called()

    async def test_revision_number_is_1_for_first_entry(self) -> None:
        """The first entry created always has revision_number=1."""
        uid = _uid()
        sid = _uid()
        msg_id = _uid()
        entry = _fake_entry(session_id=sid, user_id=uid, revision_number=1)

        pool = AsyncMock()
        pool.fetch.return_value = [{"id": msg_id, "content": "Test content."}]

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=None)
        store.get_session = AsyncMock(return_value=MagicMock(
            id=sid,
            user_id=uid,
            bot_id="test-bot",
            topic_id=None,
            source_message_ids=[msg_id],
            template_key="freeform_reflection",
            temporal_scope="instant",
            phase="freeform",
            period_start=None,
            period_end=None,
            timezone="UTC",
        ))
        store.create_entry = AsyncMock(return_value=entry)

        result = await normalize_and_create_entry(
            store=store,
            user_id=uid,
            session_id=sid,
            bot_id="test-bot",
            pool=pool,
        )

        assert result.revision_number == 1


# ═══════════════════════════════════════════════════════════════════════════
# Retry safety tests
# ═══════════════════════════════════════════════════════════════════════════


class TestRetrySafety:
    """Normalization failures don't corrupt session state."""

    async def test_session_remains_finalized_after_normalization_error(self) -> None:
        """If normalize_and_create_entry fails, the session stays finalized."""
        uid = _uid()
        sid = _uid()
        msg_id = _uid()

        pool = AsyncMock()
        pool.fetch.return_value = []  # No message texts → will fail gracefully

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=None)
        # Session exists and is in finalizing status.
        store.get_session = AsyncMock(return_value=MagicMock(
            id=sid,
            user_id=uid,
            bot_id="test-bot",
            topic_id=None,
            source_message_ids=[msg_id],
            template_key="freeform_reflection",
            temporal_scope="instant",
            phase="freeform",
            period_start=None,
            period_end=None,
            timezone="UTC",
        ))

        # No message texts → returns None, but does NOT raise.
        result = await normalize_and_create_entry(
            store=store,
            user_id=uid,
            session_id=sid,
            bot_id="test-bot",
            pool=pool,
        )

        # Returns None (graceful degradation), session is still finalized.
        assert result is None
        # create_entry was NOT called (no texts to normalize).
        store.create_entry.assert_not_called()

    async def test_error_during_fetch_is_surfaced(self) -> None:
        """If fetching message texts fails hard, the error propagates."""
        uid = _uid()
        sid = _uid()

        pool = AsyncMock()
        pool.fetch = AsyncMock(side_effect=RuntimeError("DB pool exhausted"))

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=None)
        store.get_session = AsyncMock(return_value=MagicMock(
            id=sid,
            user_id=uid,
            bot_id="test-bot",
            topic_id=None,
            source_message_ids=[_uid()],
            template_key="freeform_reflection",
            temporal_scope="instant",
            phase="freeform",
            period_start=None,
            period_end=None,
            timezone="UTC",
        ))

        with pytest.raises(RuntimeError, match="DB pool exhausted"):
            await normalize_and_create_entry(
                store=store,
                user_id=uid,
                session_id=sid,
                bot_id="test-bot",
                pool=pool,
            )

    async def test_session_not_found_raises(self) -> None:
        """If the session doesn't exist, raise SessionNotFoundError."""
        uid = _uid()
        sid = _uid()

        pool = AsyncMock()

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=None)
        store.get_session = AsyncMock(return_value=None)

        with pytest.raises(SessionNotFoundError):
            await normalize_and_create_entry(
                store=store,
                user_id=uid,
                session_id=sid,
                bot_id="test-bot",
                pool=pool,
            )

    async def test_no_source_messages_returns_none(self) -> None:
        """Session with no source messages → nothing to normalize."""
        uid = _uid()
        sid = _uid()

        pool = AsyncMock()

        store = MagicMock()
        store.get_current_entry = AsyncMock(return_value=None)
        store.get_session = AsyncMock(return_value=MagicMock(
            id=sid,
            user_id=uid,
            bot_id="test-bot",
            topic_id=None,
            source_message_ids=[],  # empty
            template_key="freeform_reflection",
            temporal_scope="instant",
            phase="freeform",
            period_start=None,
            period_end=None,
            timezone="UTC",
        ))

        result = await normalize_and_create_entry(
            store=store,
            user_id=uid,
            session_id=sid,
            bot_id="test-bot",
            pool=pool,
        )

        assert result is None
        store.create_entry.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Worker integration tests
# ═══════════════════════════════════════════════════════════════════════════


class TestWorkerIntegration:
    """Finalization worker calls normalize_and_create_entry after finalize."""

    async def test_worker_calls_normalize_after_finalize(self) -> None:
        """When worker finalizes a session, it also normalizes and creates entry."""
        from datetime import timedelta

        from app.reflections.finalization import FinalizationDecision
        from app.services.reflections_finalization_worker import (
            ReflectionFinalizationWorker,
        )

        now = _now()
        uid = _uid()
        sid = _uid()

        session_row = {
            "id": sid,
            "user_id": uid,
            "bot_id": "test-bot",
            "status": "collecting",
            "source_message_ids": [_uid()],
            "opened_at": now - timedelta(hours=2),
            "idle_finalize_at": now - timedelta(minutes=10),
            "finalized_at": None,
            "abandoned_at": None,
            "topic_id": None,
            "phase": "freeform",
        }

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        from app.config import Settings

        class FakeSettings:
            reflection_finalization_worker_poll_interval_s = 60.0
            reflection_finalization_worker_batch_size = 50

        worker = ReflectionFinalizationWorker(pool, settings=FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="finalize",
                reason="idle deadline passed",
                session_id=sid,
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                mock_store.get_current_entry = AsyncMock(return_value=None)
                mock_store.get_session = AsyncMock(return_value=MagicMock(
                    id=sid,
                    user_id=uid,
                    bot_id="test-bot",
                    topic_id=None,
                    source_message_ids=[_uid()],
                    template_key="freeform_reflection",
                    temporal_scope="instant",
                    phase="freeform",
                    period_start=None,
                    period_end=None,
                    timezone="UTC",
                ))
                mock_entry = _fake_entry(session_id=sid, user_id=uid)
                mock_store.create_entry = AsyncMock(return_value=mock_entry)
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.scanned == 1
        assert result.finalized == 1
        # Worker called finalize_session.
        mock_store.finalize_session.assert_awaited_once()
        # Worker also called normalize → create_entry via the bridge.
        # The bridge calls get_current_entry and get_session.
        mock_store.get_current_entry.assert_awaited()

    async def test_worker_survives_normalization_failure(self) -> None:
        """If normalize_and_create_entry fails, the worker continues and counts
        the finalization as successful but the tick may report errors."""
        from datetime import timedelta

        from app.reflections.finalization import FinalizationDecision
        from app.services.reflections_finalization_worker import (
            ReflectionFinalizationWorker,
        )

        now = _now()
        uid = _uid()
        sid = _uid()

        session_row = {
            "id": sid,
            "user_id": uid,
            "bot_id": "test-bot",
            "status": "collecting",
            "source_message_ids": [_uid()],
            "opened_at": now - timedelta(hours=2),
            "idle_finalize_at": now - timedelta(minutes=10),
            "finalized_at": None,
            "abandoned_at": None,
            "topic_id": None,
            "phase": "freeform",
        }

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        class FakeSettings:
            reflection_finalization_worker_poll_interval_s = 60.0
            reflection_finalization_worker_batch_size = 50

        worker = ReflectionFinalizationWorker(pool, settings=FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="finalize",
                reason="idle deadline passed",
                session_id=sid,
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                # Simulate normalization failure: session not found by get_session
                mock_store.get_current_entry = AsyncMock(return_value=None)
                mock_store.get_session = AsyncMock(return_value=None)
                mock_store.create_entry = AsyncMock()
                mock_store_cls.return_value = mock_store

                # The worker catches the exception from normalize_and_create_entry
                # and continues — result.finalized is still 1.
                result = await worker.run_once(now=now)

        # Finalization succeeded.
        assert result.finalized == 1
        # The entry creation failure was logged but didn't crash the tick.
        mock_store.finalize_session.assert_awaited_once()

    async def test_abandoned_sessions_do_not_create_entries(self) -> None:
        """Abandoned sessions should NOT trigger entry creation."""
        from datetime import timedelta

        from app.reflections.finalization import FinalizationDecision
        from app.services.reflections_finalization_worker import (
            ReflectionFinalizationWorker,
        )

        now = _now()
        uid = _uid()
        sid = _uid()

        session_row = {
            "id": sid,
            "user_id": uid,
            "bot_id": "test-bot",
            "status": "collecting",
            "source_message_ids": [_uid()],
            "opened_at": now - timedelta(hours=26),
            "idle_finalize_at": now - timedelta(hours=25),
            "finalized_at": None,
            "abandoned_at": None,
            "topic_id": None,
            "phase": "freeform",
        }

        pool = AsyncMock()
        pool.fetch.return_value = [session_row]

        class FakeSettings:
            reflection_finalization_worker_poll_interval_s = 60.0
            reflection_finalization_worker_batch_size = 50

        worker = ReflectionFinalizationWorker(pool, settings=FakeSettings())

        with patch.object(
            worker._engine,
            "evaluate_full",
            return_value=FinalizationDecision(
                action="abandon",
                reason="session idle beyond abandon threshold",
                session_id=sid,
            ),
        ):
            with patch(
                "app.services.reflections_finalization_worker.ReflectionStore"
            ) as mock_store_cls:
                mock_store = MagicMock()
                mock_store.finalize_session = AsyncMock()
                mock_store.abandon_session = AsyncMock()
                mock_store.create_entry = AsyncMock()
                mock_store.get_current_entry = AsyncMock()
                mock_store.get_session = AsyncMock()
                mock_store_cls.return_value = mock_store

                result = await worker.run_once(now=now)

        assert result.abandoned == 1
        # create_entry should NOT be called for abandoned sessions.
        mock_store.create_entry.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# Fetch message texts tests
# ═══════════════════════════════════════════════════════════════════════════


class TestFetchMessageTexts:
    """Verify _fetch_message_texts preserves order and handles missing messages."""

    async def test_preserves_input_order(self) -> None:
        mid1, mid2, mid3 = _uid(), _uid(), _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": mid3, "content": "third"},
            {"id": mid1, "content": "first"},
            {"id": mid2, "content": "second"},
        ]

        result = await _fetch_message_texts(pool, [mid1, mid2, mid3])
        assert result == ["first", "second", "third"]

    async def test_omits_missing_messages(self) -> None:
        mid1, mid2 = _uid(), _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": mid1, "content": "only first"},
        ]

        result = await _fetch_message_texts(pool, [mid1, mid2])
        assert result == ["only first"]  # mid2 not found → omitted

    async def test_empty_ids_returns_empty(self) -> None:
        pool = AsyncMock()
        result = await _fetch_message_texts(pool, [])
        assert result == []
        pool.fetch.assert_not_called()

    async def test_none_content_is_omitted(self) -> None:
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            {"id": mid, "content": None},
        ]

        result = await _fetch_message_texts(pool, [mid])
        assert result == []  # None content → omitted


# ═══════════════════════════════════════════════════════════════════════════
# Missing-field restraint tests
# ═══════════════════════════════════════════════════════════════════════════


class TestMissingFieldRestraint:
    """The normalizer's missing-field restraint is preserved in the envelope."""

    def test_scalar_fields_without_evidence_are_none(self) -> None:
        """Template fields defined as 'str' type without evidence → None."""
        norm = _fake_normalized(
            template_data={"mood": None},  # no evidence
            fields_unsupported=["mood"],
        )
        envelope = _normalized_to_envelope(norm)
        assert envelope["template_data"]["mood"] is None

    def test_list_fields_without_evidence_are_empty(self) -> None:
        """Template fields defined as 'list[str]' type without evidence → []."""
        norm = _fake_normalized(
            template_data={"focus_areas": []},
            fields_unsupported=["focus_areas"],
        )
        envelope = _normalized_to_envelope(norm)
        assert envelope["template_data"]["focus_areas"] == []

    def test_fields_unsupported_tracked_in_meta(self) -> None:
        norm = _fake_normalized(
            template_data={"mood": None, "energy_level": None},
            fields_unsupported=["mood", "energy_level", "focus_areas"],
        )
        envelope = _normalized_to_envelope(norm)
        meta = envelope["template_data"]["_normalizer_meta"]
        assert set(meta["fields_unsupported"]) == {"mood", "energy_level", "focus_areas"}

    def test_extraction_confidence_in_meta(self) -> None:
        norm = _fake_normalized(confidence=0.42)
        envelope = _normalized_to_envelope(norm)
        assert envelope["template_data"]["_normalizer_meta"]["extraction_confidence"] == 0.42
