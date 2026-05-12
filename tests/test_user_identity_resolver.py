"""Tests for app.services.user_identity.resolve_user_address (§16.3 wi 7).

Covers:
- When a discord identity is registered, the resolver returns the discord
  address (highest priority over legacy).
- When only the legacy users.phone column is populated and no user_identities
  rows exist, the resolver falls back to the phone column.
- When an explicit transport is requested and that identity is missing, the
  resolver returns None (no automatic fallback for explicit lookups).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.services.user_identity import resolve_user_address
from tests.conftest import FakePool


@pytest.mark.asyncio
async def test_resolve_prefers_discord_over_legacy_phone() -> None:
    pool = FakePool()
    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": "X",
        "phone": "+15551234567",
        "timezone": "UTC",
    }
    pool.user_identities[("discord", "discord-user-1234")] = user_id
    pool.user_identities[("legacy", "+15551234567")] = user_id

    address = await resolve_user_address(pool, user_id)
    assert address == "discord-user-1234"


@pytest.mark.asyncio
async def test_resolve_falls_back_to_phone_when_no_identity_rows() -> None:
    pool = FakePool()
    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": "Y",
        "phone": "+15559876543",
        "timezone": "UTC",
    }
    # No identity rows seeded.
    address = await resolve_user_address(pool, user_id)
    assert address == "+15559876543"


@pytest.mark.asyncio
async def test_resolve_returns_none_for_missing_explicit_transport() -> None:
    pool = FakePool()
    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": "Z",
        "phone": "+15550000000",
        "timezone": "UTC",
    }
    # Only legacy registered; discord lookup must return None.
    pool.user_identities[("legacy", "+15550000000")] = user_id
    address = await resolve_user_address(pool, user_id, transport="discord")
    assert address is None


@pytest.mark.asyncio
async def test_resolve_explicit_transport_returns_match() -> None:
    pool = FakePool()
    user_id = uuid4()
    pool.users[user_id] = {
        "id": user_id,
        "name": "W",
        "phone": "+15551111111",
        "timezone": "UTC",
    }
    pool.user_identities[("discord", "abc#1234")] = user_id
    address = await resolve_user_address(pool, user_id, transport="discord")
    assert address == "abc#1234"
