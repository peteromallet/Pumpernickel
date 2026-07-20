"""OAuth state issuance and one-time validation for health providers."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import logging
import os
import secrets
from typing import Callable
from uuid import UUID

logger = logging.getLogger(__name__)

_DEV_FALLBACK_SECRET = "withings-health-oauth-state-dev-secret"
_DEFAULT_TTL = timedelta(minutes=10)


class OAuthStateError(RuntimeError):
    """Raised when OAuth state is missing, expired, replayed, or tampered."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class IssuedOAuthState:
    state: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ConsumedOAuthState:
    user_id: UUID
    redirect_uri: str
    state_id: str
    expires_at: datetime


@dataclass(slots=True)
class _StoredState:
    user_id: UUID
    redirect_uri: str
    expires_at: datetime
    consumed_at: datetime | None = None


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_to_wire(value: datetime) -> str:
    return _normalize_datetime(value).isoformat().replace("+00:00", "Z")


def _datetime_from_wire(value: str) -> datetime:
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    return _normalize_datetime(datetime.fromisoformat(candidate))


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _default_signing_secret() -> bytes:
    raw = os.environ.get("DATA_ENCRYPTION_KEY", "").strip()
    if raw:
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception:
            decoded = raw.encode("utf-8")
        if decoded:
            return decoded
    logger.warning(
        "DATA_ENCRYPTION_KEY not set for health OAuth state; using dev fallback secret."
    )
    return _DEV_FALLBACK_SECRET.encode("utf-8")


class OAuthStateStore:
    """In-memory one-time OAuth state store bound to a signing secret."""

    def __init__(
        self,
        *,
        signing_secret: bytes | None = None,
        ttl: timedelta = _DEFAULT_TTL,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self._signing_secret = signing_secret or _default_signing_secret()
        self._ttl = ttl
        self._now = now or (lambda: datetime.now(UTC))
        self._states: dict[str, _StoredState] = {}

    def issue(self, *, user_id: UUID, redirect_uri: str) -> IssuedOAuthState:
        normalized_redirect = redirect_uri.strip()
        if not normalized_redirect:
            raise ValueError("redirect_uri must be a non-empty string")
        issued_at = _normalize_datetime(self._now())
        expires_at = issued_at + self._ttl
        state_id = secrets.token_urlsafe(18)
        payload = {
            "state_id": state_id,
            "user_id": str(user_id),
            "redirect_uri": normalized_redirect,
            "issued_at": _datetime_to_wire(issued_at),
            "expires_at": _datetime_to_wire(expires_at),
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(self._signing_secret, serialized, hashlib.sha256).digest()
        self._purge_expired(issued_at)
        self._states[state_id] = _StoredState(
            user_id=user_id,
            redirect_uri=normalized_redirect,
            expires_at=expires_at,
        )
        return IssuedOAuthState(
            state=f"{_b64url_encode(serialized)}.{_b64url_encode(signature)}",
            expires_at=expires_at,
        )

    def consume(
        self,
        *,
        state: str,
        user_id: UUID,
        redirect_uri: str,
    ) -> ConsumedOAuthState:
        normalized_redirect = redirect_uri.strip()
        if not normalized_redirect:
            raise ValueError("redirect_uri must be a non-empty string")
        consumed = self.consume_callback(state=state)
        if consumed.user_id != user_id:
            raise OAuthStateError("wrong_user")
        if consumed.redirect_uri != normalized_redirect:
            raise OAuthStateError("redirect_mismatch")
        return consumed

    def consume_callback(self, *, state: str) -> ConsumedOAuthState:
        now = _normalize_datetime(self._now())
        payload = self._decode_and_verify(state)
        state_id = payload["state_id"]
        record = self._states.get(state_id)
        if record is None:
            raise OAuthStateError("missing_state")
        if record.consumed_at is not None:
            raise OAuthStateError("already_used")
        expires_at = _datetime_from_wire(payload["expires_at"])
        if expires_at < now or record.expires_at < now:
            raise OAuthStateError("expired")
        try:
            issued_user_id = UUID(payload["user_id"])
        except ValueError as exc:
            raise OAuthStateError("tampered") from exc
        if issued_user_id != record.user_id:
            raise OAuthStateError("tampered")
        if record.redirect_uri != payload["redirect_uri"]:
            raise OAuthStateError("tampered")
        record.consumed_at = now
        self._purge_expired(now)
        return ConsumedOAuthState(
            user_id=record.user_id,
            redirect_uri=record.redirect_uri,
            state_id=state_id,
            expires_at=expires_at,
        )

    def _decode_and_verify(self, state: str) -> dict[str, str]:
        try:
            payload_b64, signature_b64 = state.split(".", 1)
            serialized = _b64url_decode(payload_b64)
            provided_signature = _b64url_decode(signature_b64)
        except Exception as exc:
            raise OAuthStateError("tampered") from exc
        if (
            _b64url_encode(serialized) != payload_b64
            or _b64url_encode(provided_signature) != signature_b64
        ):
            raise OAuthStateError("tampered")
        expected_signature = hmac.new(
            self._signing_secret,
            serialized,
            hashlib.sha256,
        ).digest()
        if not hmac.compare_digest(expected_signature, provided_signature):
            raise OAuthStateError("tampered")
        try:
            payload = json.loads(serialized.decode("utf-8"))
        except Exception as exc:
            raise OAuthStateError("tampered") from exc
        for key in ("state_id", "user_id", "redirect_uri", "issued_at", "expires_at"):
            if not isinstance(payload.get(key), str) or not payload[key].strip():
                raise OAuthStateError("tampered")
        return payload

    def _purge_expired(self, now: datetime) -> None:
        expired_ids = [
            state_id
            for state_id, record in self._states.items()
            if record.expires_at < now
        ]
        for state_id in expired_ids:
            self._states.pop(state_id, None)


_DEFAULT_STORE: OAuthStateStore | None = None


def get_oauth_state_store() -> OAuthStateStore:
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = OAuthStateStore()
    return _DEFAULT_STORE


def reset_oauth_state_store_for_tests() -> None:
    global _DEFAULT_STORE
    _DEFAULT_STORE = None


__all__ = [
    "ConsumedOAuthState",
    "IssuedOAuthState",
    "OAuthStateError",
    "OAuthStateStore",
    "get_oauth_state_store",
    "reset_oauth_state_store_for_tests",
]
