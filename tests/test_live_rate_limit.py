"""WS rate limiter tests (Sprint 5)."""

from __future__ import annotations

import pytest

from app.services.live.rate_limit import WsRateLimiter


def test_allows_first_n_requests() -> None:
    limiter = WsRateLimiter(per_minute=10, capacity=10)
    for i in range(10):
        assert limiter.allow("1.2.3.4", now=0.0) is True, f"failed at request {i + 1}"


def test_blocks_after_capacity() -> None:
    limiter = WsRateLimiter(per_minute=10, capacity=10)
    for _ in range(10):
        limiter.allow("1.2.3.4", now=0.0)
    assert limiter.allow("1.2.3.4", now=0.0) is False


def test_refills_over_time() -> None:
    limiter = WsRateLimiter(per_minute=60, capacity=10)
    # Burn the bucket
    for _ in range(10):
        limiter.allow("1.2.3.4", now=0.0)
    assert limiter.allow("1.2.3.4", now=0.0) is False
    # Wait 1s = 1 token at 60/min
    assert limiter.allow("1.2.3.4", now=1.0) is True
    # Without further wait, blocked again
    assert limiter.allow("1.2.3.4", now=1.0) is False


def test_separate_buckets_per_ip() -> None:
    limiter = WsRateLimiter(per_minute=10, capacity=2)
    assert limiter.allow("1.2.3.4", now=0.0) is True
    assert limiter.allow("1.2.3.4", now=0.0) is True
    assert limiter.allow("1.2.3.4", now=0.0) is False
    # Different IP gets its own bucket.
    assert limiter.allow("5.6.7.8", now=0.0) is True
    assert limiter.allow("5.6.7.8", now=0.0) is True
    assert limiter.allow("5.6.7.8", now=0.0) is False


def test_per_minute_zero_disables_limiter() -> None:
    limiter = WsRateLimiter(per_minute=0)
    # Should allow everything regardless of volume.
    for _ in range(1000):
        assert limiter.allow("1.2.3.4", now=0.0) is True
