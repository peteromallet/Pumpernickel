"""In-process per-IP token bucket for the live-voice WS endpoint.

10 connection openings per minute per IP by default, configurable via
``LIVE_VOICE_WS_RATE_PER_MIN``.  In-process is sufficient for the
single-replica deploy in the briefing; multi-replica would replace this
with Redis.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class WsRateLimiter:
    def __init__(self, *, per_minute: int | None = None, capacity: int | None = None) -> None:
        env_rate = os.environ.get("LIVE_VOICE_WS_RATE_PER_MIN")
        self.per_minute = per_minute if per_minute is not None else int(env_rate or "10")
        self.capacity = capacity if capacity is not None else max(2, self.per_minute)
        self._buckets: dict[str, _Bucket] = {}

    def allow(self, ip: str, *, now: float | None = None) -> bool:
        if self.per_minute <= 0:
            return True  # explicit disable
        now = now if now is not None else time.monotonic()
        refill_per_sec = self.per_minute / 60.0
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.capacity), last_refill=now)
            self._buckets[ip] = bucket
        # Refill since last seen.
        elapsed = max(0.0, now - bucket.last_refill)
        bucket.tokens = min(float(self.capacity), bucket.tokens + elapsed * refill_per_sec)
        bucket.last_refill = now
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True


WS_RATE_LIMITER = WsRateLimiter()
