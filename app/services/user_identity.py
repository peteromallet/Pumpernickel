"""User identity resolver — maps user_id to a transport address (§16.3 wi 7).

Architecture spec §16.3 work item 7 says "switch to user_identities lookup.
Column stays for now."  This helper is the single source of truth for that
lookup, used by all sites that previously read `users.phone` directly to
populate a User struct's address.

Resolution order (when transport is None):
  1. Highest-priority registered identity (discord > whatsapp > sms > legacy).
  2. Falls back to users.phone if no user_identities row exists.

When *transport* is provided, only that transport is consulted; the legacy
phone fallback is still attempted as a last resort because the column has
not yet been retired.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

# Priority ordering: discord-first per §16.3 (the active transport for new
# users), then whatsapp, sms, and finally 'legacy' (the back-filled column
# value).  This list is consulted only when the caller did not request a
# specific transport.
_TRANSPORT_PRIORITY: tuple[str, ...] = ("discord", "whatsapp", "sms", "legacy")


async def resolve_user_address(
    pool: Any,
    user_id: UUID,
    *,
    transport: str | None = None,
) -> str | None:
    """Return a registered address for *user_id*.

    When *transport* is None, scan user_identities for the highest-priority
    transport present.  Falls back to ``users.phone`` if nothing matches —
    the column survives §16.3 wi 7 ("column stays for now").

    When *transport* is provided, only that transport is consulted (no
    automatic fallback to phone — explicit callers know what they want).
    """
    if transport is not None:
        row = await pool.fetchrow(
            """
            SELECT address
            FROM user_identities
            WHERE user_id = $1 AND transport = $2
            LIMIT 1
            """,
            user_id,
            transport,
        )
        if row is not None:
            return row["address"]
        return None

    rows = await pool.fetch(
        """
        SELECT transport, address
        FROM user_identities
        WHERE user_id = $1
        """,
        user_id,
    )
    by_transport = {r["transport"]: r["address"] for r in rows or []}
    for t in _TRANSPORT_PRIORITY:
        if t in by_transport:
            return by_transport[t]

    # Fallback: legacy users.phone column (still populated).
    row = await pool.fetchrow(
        "SELECT phone FROM users WHERE id = $1",
        user_id,
    )
    if row is None:
        return None
    return row["phone"]
