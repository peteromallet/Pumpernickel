"""Streaming STT interface + a Stub impl + an OpenAI Realtime impl.

Contract (``StreamingTranscriber``):

* ``start()`` — open whatever upstream connection is needed.
* ``push(pcm: bytes)`` — non-blocking; queue the frame for transcription.
* ``aclose()`` — flush + close.

Events are delivered via an ``asyncio.Queue`` of typed dicts:

* ``{"type": "partial", "text": "…", "ts": 1731....}`` — interim hypothesis
* ``{"type": "final",   "text": "…", "ts": 1731....}`` — finalized turn
* ``{"type": "error",   "message": "…"}``               — non-fatal

The WS handler in ``app/routers/live_voice.py`` forwards these events to
the client and persists every ``final`` to ``mediator.transcript_turns``.

This module ships two impls:

* :class:`StubTranscriber` — deterministic events on a timer; powers
  browser-without-mic dev runs and the no-key local stack.
* :class:`OpenAIRealtimeTranscriber` — wraps the ``gpt-4o-mini-transcribe``
  Realtime WS endpoint. Selected when ``OPENAI_API_KEY`` is set AND
  ``LIVE_VOICE_STT_PROVIDER`` is unset or ``=openai``.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Any, AsyncIterator, Protocol

logger = logging.getLogger(__name__)


class StreamingTranscriber(Protocol):
    events: asyncio.Queue[dict[str, Any]]

    async def start(self) -> None: ...
    async def push(self, pcm: bytes) -> None: ...
    async def aclose(self) -> None: ...


def select_transcriber(*, target_sample_rate: int = 16000) -> StreamingTranscriber:
    """Pick the STT impl based on env.

    * ``LIVE_VOICE_STT_PROVIDER=stub`` (or no OpenAI key) → :class:`StubTranscriber`.
    * ``LIVE_VOICE_STT_PROVIDER=openai`` (default if key set) →
      :class:`OpenAIRealtimeTranscriber`.
    """
    provider = (os.environ.get("LIVE_VOICE_STT_PROVIDER") or "").strip().lower()
    has_real_key = bool(
        (os.environ.get("OPENAI_API_KEY") or "").startswith("sk-")
        and "stub" not in (os.environ.get("OPENAI_API_KEY") or "")
    )
    if provider == "stub" or (provider == "" and not has_real_key):
        return StubTranscriber()
    return OpenAIRealtimeTranscriber(sample_rate=target_sample_rate)


# --------------------------------------------------------------------------- #
# Stub impl.
# --------------------------------------------------------------------------- #


class StubTranscriber:
    """Emits a fake partial + final pair every ~2 seconds of audio.

    Useful for dev runs where the OpenAI key is missing or the headless
    browser produces silence frames. The wire protocol exercised here is
    identical to the real transcriber.
    """

    def __init__(self) -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=64)
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        self._bytes_seen = 0
        self._last_final = 0.0
        self._turn_counter = 0

    async def start(self) -> None:
        # No upstream to open.
        pass

    async def push(self, pcm: bytes) -> None:
        if self._stopped:
            return
        self._bytes_seen += len(pcm)
        now = time.time()
        if now - self._last_final >= 2.0 and self._bytes_seen >= 2 * 16000 * 2:
            # ~2 seconds of 16kHz int16 audio.
            self._turn_counter += 1
            await self._safe_emit({
                "type": "partial",
                "text": f"(stub partial #{self._turn_counter})",
                "ts": now,
            })
            await self._safe_emit({
                "type": "final",
                "text": f"This is stub transcript line {self._turn_counter}.",
                "ts": now,
            })
            self._last_final = now
            self._bytes_seen = 0

    async def aclose(self) -> None:
        self._stopped = True

    async def _safe_emit(self, event: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("stub stt: event queue full; dropping %s", event.get("type"))


# --------------------------------------------------------------------------- #
# Real impl: OpenAI Realtime gpt-4o-mini-transcribe over WSS.
# --------------------------------------------------------------------------- #


_OPENAI_REALTIME_URL = "wss://api.openai.com/v1/realtime?intent=transcription"


class OpenAIRealtimeTranscriber:
    """Connect to OpenAI Realtime and stream PCM frames for transcription.

    Audio frames are 16 kHz mono int16, sent as base64-encoded chunks via
    ``input_audio_buffer.append`` events. Partial transcripts arrive as
    ``conversation.item.input_audio_transcription.delta``; finals as
    ``conversation.item.input_audio_transcription.completed``.

    Failures are surfaced as ``{"type": "error", …}`` events; the WS
    handler decides whether to fall back to the stub or close the session.
    """

    def __init__(self, *, sample_rate: int = 16000, model: str = "gpt-4o-mini-transcribe") -> None:
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=128)
        self._sample_rate = sample_rate
        self._model = model
        self._ws: Any = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stopped = False

    async def start(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dep declared at module level
            await self._safe_emit({"type": "error", "message": f"websockets not installed: {exc}"})
            self._stopped = True
            return

        api_key = (os.environ.get("OPENAI_API_KEY") or "").strip()
        if not api_key.startswith("sk-") or "stub" in api_key:
            await self._safe_emit({"type": "error", "message": "OPENAI_API_KEY missing or stub"})
            self._stopped = True
            return

        headers = [
            ("Authorization", f"Bearer {api_key}"),
            ("OpenAI-Beta", "realtime=v1"),
        ]
        try:
            self._ws = await websockets.connect(_OPENAI_REALTIME_URL, additional_headers=headers)
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"openai connect failed: {exc}"})
            self._stopped = True
            return

        await self._ws.send(json.dumps({
            "type": "transcription_session.update",
            "session": {
                "input_audio_format": "pcm16",
                "input_audio_sample_rate_hz": self._sample_rate,
                "input_audio_transcription": {"model": self._model},
                "turn_detection": {"type": "server_vad"},
            },
        }))
        self._reader_task = asyncio.create_task(self._reader_loop())

    async def push(self, pcm: bytes) -> None:
        if self._stopped or self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(pcm).decode("ascii"),
            }))
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"openai push failed: {exc}"})

    async def aclose(self) -> None:
        self._stopped = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _reader_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                kind = payload.get("type")
                text = payload.get("delta") or payload.get("transcript") or ""
                if kind == "conversation.item.input_audio_transcription.delta" and text:
                    await self._safe_emit({"type": "partial", "text": text, "ts": time.time()})
                elif kind == "conversation.item.input_audio_transcription.completed" and text:
                    await self._safe_emit({"type": "final", "text": text, "ts": time.time()})
                elif kind == "error":
                    await self._safe_emit({"type": "error", "message": str(payload.get("error"))})
        except asyncio.CancelledError:
            return
        except Exception as exc:
            await self._safe_emit({"type": "error", "message": f"openai reader crashed: {exc}"})

    async def _safe_emit(self, event: dict[str, Any]) -> None:
        try:
            self.events.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("openai stt: event queue full; dropping %s", event.get("type"))


# --------------------------------------------------------------------------- #
# Helper: drain events into an async iterator.
# --------------------------------------------------------------------------- #


async def drain_events(transcriber: StreamingTranscriber) -> AsyncIterator[dict[str, Any]]:
    """Yield events from the transcriber. Caller decides when to stop."""
    while True:
        event = await transcriber.events.get()
        yield event
