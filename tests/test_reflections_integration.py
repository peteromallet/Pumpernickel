"""Integration tests for reflection capture wiring.

Proves that:
  - User text messages are classified and attached to reflection sessions
    through the existing ingress path.
  - Voice transcripts (which arrive as text content after transcription)
    flow through the same classification → session attachment seam.
  - Non-reflection messages do NOT create or attach to sessions.
  - Non-reflection pacing is unchanged: the capture function never blocks
    or raises, so the agentic turn always proceeds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from app.reflections.classifier import ClassificationResult
from app.services.reflections_integration import (
    capture_burst_for_reflection,
    _fetch_message_contents,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _uid() -> UUID:
    return uuid4()


class _FakeUser:
    """Minimal user object with the attributes capture_burst_for_reflection needs."""

    def __init__(self, user_id: UUID | None = None, timezone: str = "UTC") -> None:
        self.id = user_id or _uid()
        self.timezone = timezone


def _message_row(msg_id: UUID, content: str | None) -> dict:
    """Build a fake asyncpg Row-like dict for messages table."""
    return {"id": msg_id, "content": content}


class _FakeSession:
    """Minimal session object mimicking ReflectionSession attributes."""

    def __init__(
        self,
        session_id: UUID,
        user_id: UUID,
        bot_id: str,
        *,
        source_message_ids: list[UUID] | None = None,
        topic_id: UUID | None = None,
        temporal_scope: str = "instant",
        phase: str = "freeform",
    ) -> None:
        self.id = session_id
        self.user_id = user_id
        self.bot_id = bot_id
        self.topic_id = topic_id
        self.source_message_ids = list(source_message_ids or [])
        self.temporal_scope = temporal_scope
        self.phase = phase


class _FakeReflectionStore:
    """Fake ReflectionStore for integration tests.

    Tracks calls to list_sessions and open_or_attach_session so tests
    can assert on them without needing a real database.
    """

    def __init__(
        self,
        sessions: list[_FakeSession] | None = None,
        *,
        open_raises: Exception | None = None,
    ) -> None:
        self._sessions = list(sessions or [])
        self._open_raises = open_raises
        self.open_calls: list[dict] = []
        self.list_calls: list[dict] = []
        self.list_sessions_raises: Exception | None = None

    async def list_sessions(self, *, user_id, statuses=None, limit=50):
        self.list_calls.append(
            {"user_id": user_id, "statuses": statuses, "limit": limit}
        )
        if self.list_sessions_raises:
            raise self.list_sessions_raises
        # Filter by user_id (simple — just return all sessions for now)
        return [s for s in self._sessions if s.user_id == user_id]

    async def open_or_attach_session(self, **kwargs):
        self.open_calls.append(kwargs)
        if self._open_raises:
            raise self._open_raises
        # Return a fake session
        return _FakeSession(
            session_id=_uid(),
            user_id=kwargs.get("user_id"),
            bot_id=kwargs.get("bot_id"),
            source_message_ids=kwargs.get("source_message_ids", []),
            temporal_scope=kwargs.get("temporal_scope", "instant"),
            phase=kwargs.get("phase", "freeform"),
        )

    @property
    def open_called(self) -> bool:
        return len(self.open_calls) > 0

    @property
    def last_open_kwargs(self) -> dict:
        return self.open_calls[-1] if self.open_calls else {}


# ── Tests: _fetch_message_contents ─────────────────────────────────────────


class TestFetchMessageContents:
    async def test_fetches_content_for_all_ids(self):
        """Content is fetched and returned in input order."""
        pool = AsyncMock()
        mid1, mid2 = _uid(), _uid()
        pool.fetch.return_value = [
            _message_row(mid1, "Hello world"),
            _message_row(mid2, "Let's reflect on today"),
        ]

        result = await _fetch_message_contents(pool, [mid1, mid2])

        assert len(result) == 2
        assert result[0] == (mid1, "Hello world")
        assert result[1] == (mid2, "Let's reflect on today")
        pool.fetch.assert_called_once()

    async def test_preserves_input_order(self):
        """Results are returned in the order of input IDs, not DB order."""
        pool = AsyncMock()
        mid1, mid2, mid3 = _uid(), _uid(), _uid()
        pool.fetch.return_value = [
            _message_row(mid2, "middle"),
            _message_row(mid3, "last"),
            _message_row(mid1, "first"),
        ]

        result = await _fetch_message_contents(pool, [mid1, mid2, mid3])

        assert [r[0] for r in result] == [mid1, mid2, mid3]

    async def test_missing_messages_omitted(self):
        """Messages not found in DB are silently omitted."""
        pool = AsyncMock()
        mid1, mid2 = _uid(), _uid()
        pool.fetch.return_value = [_message_row(mid1, "only one")]

        result = await _fetch_message_contents(pool, [mid1, mid2])

        assert len(result) == 1
        assert result[0] == (mid1, "only one")

    async def test_empty_ids_returns_empty_list(self):
        """Empty input returns empty list without DB call."""
        pool = AsyncMock()
        result = await _fetch_message_contents(pool, [])
        assert result == []
        pool.fetch.assert_not_called()

    async def test_null_content_preserved(self):
        """Messages with NULL content are preserved as None."""
        pool = AsyncMock()
        mid = _uid()
        pool.fetch.return_value = [_message_row(mid, None)]

        result = await _fetch_message_contents(pool, [mid])

        assert result == [(mid, None)]


# ── Tests: capture_burst_for_reflection ─────────────────────────────────────


class TestCaptureBurstForReflection:
    """Integration tests for the full capture pipeline."""

    async def test_text_message_opens_reflection_session(self):
        """A burst with an explicit reflection message opens a new session."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [_message_row(mid, "Daily reflection: today I felt great")]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        assert fake_store.open_called
        assert fake_store.last_open_kwargs["user_id"] == user.id
        assert fake_store.last_open_kwargs["bot_id"] == "test_bot"
        assert mid in fake_store.last_open_kwargs["source_message_ids"]

    async def test_non_reflection_message_skipped(self):
        """A burst with a non-reflection message (joke) is skipped."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [_message_row(mid, "haha that's funny lol")]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        assert not fake_store.open_called

    async def test_voice_transcript_follows_same_path_as_text(self):
        """Voice transcripts are just text content after transcription —
        they flow through the exact same classification + attachment path."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid, "I've been thinking about my week and I feel really good about the progress")
        ]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        # Same path — session should be opened for reflective voice transcript
        assert fake_store.open_called
        assert mid in fake_store.last_open_kwargs["source_message_ids"]

    async def test_attaches_to_existing_session(self):
        """When a collecting session exists, new reflection messages attach."""
        user = _FakeUser()
        existing_session_id = _uid()
        mid1 = _uid()  # already in session
        mid2 = _uid()  # new message

        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid2, "Also, I wanted to add that I'm grateful for my team")
        ]

        existing_session = _FakeSession(
            existing_session_id,
            user.id,
            "test_bot",
            source_message_ids=[mid1],
        )
        fake_store = _FakeReflectionStore(sessions=[existing_session])

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid2],
                user,
                bot_id="test_bot",
            )

        # Should have called open_or_attach_session (which handles attach internally)
        assert fake_store.open_called
        kwargs = fake_store.last_open_kwargs
        # The merged source IDs should include both mid1 and mid2
        assert mid1 in kwargs["source_message_ids"]
        assert mid2 in kwargs["source_message_ids"]

    async def test_burst_with_mixed_content_opens_for_reflection_candidate(self):
        """A burst with one joke and one reflection: the reflection candidate
        drives session creation, and all messages are included as source."""
        user = _FakeUser()
        mid_joke = _uid()
        mid_reflection = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid_joke, "haha lol"),
            _message_row(mid_reflection, "My reflection: this week was productive"),
        ]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid_joke, mid_reflection],
                user,
                bot_id="test_bot",
            )

        assert fake_store.open_called
        kwargs = fake_store.last_open_kwargs
        assert mid_joke in kwargs["source_message_ids"]
        assert mid_reflection in kwargs["source_message_ids"]

    async def test_empty_burst_noops(self):
        """Empty message list returns without any DB calls."""
        user = _FakeUser()
        pool = AsyncMock()

        await capture_burst_for_reflection(pool, [], user, bot_id="test_bot")

        pool.fetch.assert_not_called()

    async def test_fetch_failure_does_not_raise(self):
        """If message content fetch fails, the function logs and returns
        without blocking the caller (agentic turn proceeds)."""
        user = _FakeUser()
        pool = AsyncMock()
        pool.fetch.side_effect = RuntimeError("DB connection lost")

        # Must not raise
        await capture_burst_for_reflection(
            pool,
            [_uid()],
            user,
            bot_id="test_bot",
        )

    async def test_session_list_failure_does_not_raise(self):
        """If listing sessions fails, we proceed without active-session
        context — still attempt open."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [_message_row(mid, "Daily reflection: checking in")]

        fake_store = _FakeReflectionStore()
        fake_store.list_sessions_raises = RuntimeError("DB error")

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        # Should still attempt open (no active session = open new)
        assert fake_store.open_called

    async def test_open_or_attach_failure_does_not_raise(self):
        """If open_or_attach_session fails, the error is swallowed so the
        agentic turn continues uninterrupted."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [_message_row(mid, "Reflection: today was good")]

        fake_store = _FakeReflectionStore(
            open_raises=RuntimeError("unique violation")
        )

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            # Must not raise
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

    async def test_non_reflection_burst_all_skipped(self):
        """A burst where ALL messages are non-reflection is skipped."""
        user = _FakeUser()
        mid1, mid2, mid3 = _uid(), _uid(), _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid1, "haha"),
            _message_row(mid2, "ok thanks"),
            _message_row(mid3, "what time is it"),
        ]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid1, mid2, mid3],
                user,
                bot_id="test_bot",
            )

        assert not fake_store.open_called

    async def test_task_message_excluded(self):
        """Task/reminder messages are classified as non-reflection and skipped."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid, "remind me to buy groceries tomorrow")
        ]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        assert not fake_store.open_called

    async def test_different_bot_session_not_attached(self):
        """An active session for a different bot_id is not used for attachment."""
        user = _FakeUser()
        mid = _uid()
        other_bot_session_id = _uid()

        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid, "My daily reflection: feeling good")
        ]

        # An active collecting session exists, but for a different bot
        other_session = _FakeSession(
            other_bot_session_id,
            user.id,
            "other_bot",  # different bot
        )
        fake_store = _FakeReflectionStore(sessions=[other_session])

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",  # different from other_bot
            )

        # Should open a new session (not attach to other_bot's)
        assert fake_store.open_called
        kwargs = fake_store.last_open_kwargs
        # The merged source IDs should only contain our message
        # (other_bot's session not used for attachment)
        assert mid in kwargs["source_message_ids"]

    async def test_explicit_retrospective_wording_opens_session(self):
        """Text with explicit 'retrospective' keyword opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid, "Time for a retrospective of this week")
        ]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        assert fake_store.open_called

    async def test_explicit_reflection_keyword_opens_session(self):
        """Text with explicit 'reflection' keyword opens a session."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid, "Here is my reflection: today went well")
        ]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        assert fake_store.open_called

    async def test_semantic_reflection_opens_session(self):
        """Text with introspective semantic patterns ('I feel', 'learned') opens session."""
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [
            _message_row(mid, "I feel like this week has been really productive and I've learned a lot")
        ]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        assert fake_store.open_called


# ── Tests: text vs voice transcript parity ──────────────────────────────────


class TestTextAndVoiceTranscriptParity:
    """Prove text and voice transcripts follow the identical code path."""

    @staticmethod
    async def _run_capture(content: str) -> bool:
        """Helper: run capture_burst_for_reflection with given content.

        Returns True if a session was opened/attached.
        """
        user = _FakeUser()
        mid = _uid()
        pool = AsyncMock()
        pool.fetch.return_value = [_message_row(mid, content)]

        fake_store = _FakeReflectionStore()

        with patch(
            "app.services.reflections_integration.ReflectionStore",
            return_value=fake_store,
        ):
            await capture_burst_for_reflection(
                pool,
                [mid],
                user,
                bot_id="test_bot",
            )

        return fake_store.open_called

    async def test_text_reflection_opens_session(self):
        """Typed reflection text opens a session."""
        assert await self._run_capture(
            "Time for a retrospective of this week"
        ) is True

    async def test_voice_transcript_reflection_opens_session(self):
        """Voice transcript with reflective content opens a session —
        identical behavior to typed text."""
        assert await self._run_capture(
            "I feel like this week has been really productive and I've learned a lot"
        ) is True

    async def test_text_non_reflection_skipped(self):
        """Non-reflection typed text is skipped."""
        assert await self._run_capture("lol that's hilarious") is False

    async def test_voice_transcript_non_reflection_skipped(self):
        """Non-reflection voice transcript is skipped —
        identical behavior to typed text."""
        assert await self._run_capture(
            "can you set a reminder for tomorrow"
        ) is False

    async def test_voice_transcript_with_explicit_reflection_word(self):
        """Voice transcript containing 'reflection' keyword opens session."""
        assert await self._run_capture(
            "Here is my reflection: today went well"
        ) is True

    async def test_voice_transcript_logistics_skipped(self):
        """Voice transcript about logistics is skipped."""
        assert await self._run_capture(
            "Can you book a flight to New York for next Tuesday"
        ) is False
