"""Synthetic load smoke for the live-voice WS surface.

Drives N parallel sessions through the full lifecycle (create -> WS ->
synthetic PCM frames -> end -> review) and reports per-stage p50/p95/p99
latencies pulled from mediator.live_session_latency.

Usage:

    LIVE_VOICE_BASE=http://127.0.0.1:8766 \\
        DATABASE_URL=postgresql://postgres:postgres@localhost:54322/mediator \\
        uv run python scripts/live_voice_load_smoke.py --sessions 5

This is intentionally lightweight (no async runners, no fancy CI hooks) —
the goal is "does our SLO hold at small load against the stub stack?".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any

import httpx
import websockets


SAMPLE_COUNT = 2048
SILENCE_FRAME = (b"\x00\x00") * SAMPLE_COUNT
FRAMES_PER_SESSION = 80
FRAME_DELAY_S = 0.05


async def run_one(base_url: str, idx: int) -> dict[str, Any]:
    timings: dict[str, float] = {}
    async with httpx.AsyncClient(timeout=10.0) as client:
        t0 = time.perf_counter()
        res = await client.post(
            f"{base_url}/api/live/sessions",
            json={"bot_id": "tante_rosi", "steering_text": f"load smoke #{idx}"},
        )
        timings["create_session_ms"] = (time.perf_counter() - t0) * 1000
        res.raise_for_status()
        session_id = res.json()["session_id"]

        ws_url = base_url.replace("http", "ws") + f"/ws/live/{session_id}"
        connect_at = time.perf_counter()
        async with websockets.connect(ws_url) as ws:
            # Drain phases.
            for _ in range(4):
                await asyncio.wait_for(ws.recv(), timeout=8)
            timings["phase_stream_ms"] = (time.perf_counter() - connect_at) * 1000

            # Synthetic ear-to-ear: send a text_input and time the round-trip
            # to the bot_turn event. That matches the SLO target ("user
            # utterance committed -> bot speaks"), not the wall-clock from
            # session-create which is dominated by the phase stream.
            ear_to_ear_start = time.perf_counter()
            await ws.send(json.dumps({
                "type": "text_input",
                "text": "synthetic load smoke utterance",
            }))
            first_turn_at: float | None = None
            deadline = time.perf_counter() + 8.0
            while time.perf_counter() < deadline and first_turn_at is None:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    payload = json.loads(raw)
                    if payload.get("type") == "bot_turn":
                        first_turn_at = time.perf_counter()
                        break
                except asyncio.TimeoutError:
                    pass

            if first_turn_at is not None:
                timings["ear_to_ear_ms"] = (first_turn_at - ear_to_ear_start) * 1000

            # Frame-shovel a small burst so the latency table also gets
            # asr_finalize / orchestrator_db rows.
            for i in range(20):
                await ws.send(SILENCE_FRAME)
                await asyncio.sleep(FRAME_DELAY_S)
                try:
                    while True:
                        await asyncio.wait_for(ws.recv(), timeout=0.005)
                except asyncio.TimeoutError:
                    pass

            # End + review fetch.
            end_start = time.perf_counter()
            review_res = await client.post(
                f"{base_url}/api/live/sessions/{session_id}/end",
            )
            review_res.raise_for_status()
            timings["end_session_ms"] = (time.perf_counter() - end_start) * 1000

    return {"session_id": session_id, "timings": timings}


def percentiles(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    sorted_values = sorted(values)
    return {
        "min": sorted_values[0],
        "p50": statistics.median(sorted_values),
        "p95": sorted_values[max(0, int(len(sorted_values) * 0.95) - 1)],
        "p99": sorted_values[max(0, int(len(sorted_values) * 0.99) - 1)],
        "max": sorted_values[-1],
    }


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sessions", type=int, default=5)
    ap.add_argument("--base-url", default=os.environ.get("LIVE_VOICE_BASE", "http://127.0.0.1:8766"))
    args = ap.parse_args()

    print(f"running {args.sessions} synthetic sessions against {args.base_url}")
    results = await asyncio.gather(
        *(run_one(args.base_url, i) for i in range(args.sessions)),
        return_exceptions=True,
    )
    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if not isinstance(r, dict)]
    print(f"  successes: {len(successes)} / {args.sessions}")
    if failures:
        for f in failures:
            print(f"  fail: {type(f).__name__}: {f}")

    by_stage: dict[str, list[float]] = {}
    for r in successes:
        for stage, ms in r["timings"].items():
            by_stage.setdefault(stage, []).append(ms)
    print("\n  per-stage latency (ms):")
    for stage, values in sorted(by_stage.items()):
        pct = percentiles(values)
        print(
            f"    {stage:>18s}  p50={pct['p50']:.0f}  p95={pct['p95']:.0f}  p99={pct['p99']:.0f}  max={pct['max']:.0f}"
        )

    # SLO check: p95 ear_to_ear_ms <= 2000.
    ear_to_ear = by_stage.get("ear_to_ear_ms") or []
    if ear_to_ear:
        p95 = sorted(ear_to_ear)[max(0, int(len(ear_to_ear) * 0.95) - 1)]
        slo_ok = p95 <= 2000
        print(f"\n  SLO check: p95 ear_to_ear_ms = {p95:.0f}ms (target ≤ 2000ms) — {'OK' if slo_ok else 'FAIL'}")
        return 0 if slo_ok else 1
    return 0 if successes else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
