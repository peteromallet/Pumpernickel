from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.services.health_sync.oauth_state import OAuthStateError, OAuthStateStore


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_valid_state_consumes_exactly_once() -> None:
    clock = _Clock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    store = OAuthStateStore(signing_secret=b"test-secret", now=clock)
    user_id = uuid4()
    issued = store.issue(user_id=user_id, redirect_uri="https://app.example/health/return")

    consumed = store.consume(
        state=issued.state,
        user_id=user_id,
        redirect_uri="https://app.example/health/return",
    )

    assert consumed.user_id == user_id
    assert consumed.redirect_uri == "https://app.example/health/return"
    assert consumed.expires_at == datetime(2026, 7, 20, 12, 10, tzinfo=UTC)


def test_replay_is_rejected() -> None:
    clock = _Clock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    store = OAuthStateStore(signing_secret=b"test-secret", now=clock)
    user_id = uuid4()
    issued = store.issue(user_id=user_id, redirect_uri="https://app.example/health/return")

    store.consume(
        state=issued.state,
        user_id=user_id,
        redirect_uri="https://app.example/health/return",
    )

    with pytest.raises(OAuthStateError, match="already_used"):
        store.consume(
            state=issued.state,
            user_id=user_id,
            redirect_uri="https://app.example/health/return",
        )


def test_wrong_user_is_rejected() -> None:
    clock = _Clock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    store = OAuthStateStore(signing_secret=b"test-secret", now=clock)
    issued = store.issue(
        user_id=uuid4(),
        redirect_uri="https://app.example/health/return",
    )

    with pytest.raises(OAuthStateError, match="wrong_user"):
        store.consume(
            state=issued.state,
            user_id=uuid4(),
            redirect_uri="https://app.example/health/return",
        )


def test_expired_state_is_rejected() -> None:
    clock = _Clock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    store = OAuthStateStore(
        signing_secret=b"test-secret",
        ttl=timedelta(minutes=2),
        now=clock,
    )
    user_id = uuid4()
    issued = store.issue(user_id=user_id, redirect_uri="https://app.example/health/return")
    clock.now = datetime(2026, 7, 20, 12, 3, tzinfo=UTC)

    with pytest.raises(OAuthStateError, match="expired"):
        store.consume(
            state=issued.state,
            user_id=user_id,
            redirect_uri="https://app.example/health/return",
        )


def test_redirect_mismatch_is_rejected() -> None:
    clock = _Clock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    store = OAuthStateStore(signing_secret=b"test-secret", now=clock)
    user_id = uuid4()
    issued = store.issue(user_id=user_id, redirect_uri="https://app.example/health/return")

    with pytest.raises(OAuthStateError, match="redirect_mismatch"):
        store.consume(
            state=issued.state,
            user_id=user_id,
            redirect_uri="https://app.example/health/other",
        )


def test_tampering_is_rejected() -> None:
    clock = _Clock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    store = OAuthStateStore(signing_secret=b"test-secret", now=clock)
    user_id = uuid4()
    issued = store.issue(user_id=user_id, redirect_uri="https://app.example/health/return")
    tampered = issued.state[:-1] + ("A" if issued.state[-1] != "A" else "B")

    with pytest.raises(OAuthStateError, match="tampered"):
        store.consume(
            state=tampered,
            user_id=user_id,
            redirect_uri="https://app.example/health/return",
        )


def test_noncanonical_signature_encoding_is_rejected() -> None:
    clock = _Clock(datetime(2026, 7, 20, 12, 0, tzinfo=UTC))
    store = OAuthStateStore(signing_secret=b"test-secret", now=clock)
    user_id = uuid4()
    issued = store.issue(user_id=user_id, redirect_uri="https://app.example/health/return")
    payload, signature = issued.state.split(".")
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    final_index = alphabet.index(signature[-1])
    noncanonical_signature = signature[:-1] + alphabet[final_index | 1]
    assert noncanonical_signature != signature

    with pytest.raises(OAuthStateError, match="tampered"):
        store.consume_callback(state=f"{payload}.{noncanonical_signature}")
