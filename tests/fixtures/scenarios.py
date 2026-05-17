"""Reusable scenario fixtures for real-Postgres integration tests (B.2).

These fixtures seed common message-lifecycle scenarios into the migrated
test database provided by :mod:`tests.fixtures.postgres` (the ``pg_pool``
fixture). Each scenario yields a small dataclass with the row ids it
created so tests can drill in directly without re-querying.

Why three scenarios?
--------------------
Per Project B work item 2, the minimum set that covers the Hector
incident pattern is:

* ``replied_turn``         — happy path: inbound claimed, turn opened,
                             outbound sent, marked ``processed``.
* ``silent_turn``          — intentional silence: turn opened but no
                             outbound message; marked ``processed`` with
                             ``handling_result='silent'``.
* ``failed_pre_send_turn`` — turn started, failed before it could send,
                             ``failure_class='retryable_pre_send'`` and
                             ``next_retry_at`` set; retry-eligible.

All three are composable: tests can request any combination in the same
session and the fixtures use disjoint UUIDs / users / messages so they
do not collide.

Schema notes
------------
* ``messages``  — direction ∈ {inbound, outbound}; ``bot_id`` + ``topic_id``
  are NOT NULL post-migration 0028.
* ``bot_turns`` — ``prompt_snapshot`` / ``system_prompt_version`` /
  ``model_version`` are NOT NULL. We supply fixed placeholder strings.
* Lifecycle-column writes (``next_retry_at``/``failure_class``) require
  ``app.lifecycle_writer='inbound_queue'`` set in the same transaction,
  per the trigger added in migration 0042. The ``_fail_message`` helper
  takes care of this.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest


# Bot id used by all scenarios — seeded by tests.fixtures.postgres
# (the test DB has 'mediator' bot binding already from migration 0020).
BOT_ID = "mediator"


@dataclass(frozen=True)
class RepliedTurn:
    """A successfully completed turn that produced an outbound reply."""

    user_id: UUID
    topic_id: UUID
    inbound_message_id: UUID
    outbound_message_id: UUID
    turn_id: UUID


@dataclass(frozen=True)
class SilentTurn:
    """A completed turn that intentionally did not send any outbound."""

    user_id: UUID
    topic_id: UUID
    inbound_message_id: UUID
    turn_id: UUID


@dataclass(frozen=True)
class FailedPreSendTurn:
    """A retryable failure: turn started, then failed before any send.

    ``next_retry_at`` is set in the past so the sweeper is allowed to
    re-claim the row; tests that care about the retry window can override
    by re-stamping the column.
    """

    user_id: UUID
    topic_id: UUID
    inbound_message_id: UUID
    turn_id: UUID
    failure_reason: str
    failure_class: str
    next_retry_at: datetime


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _ensure_user(conn: Any, *, name: str, phone: str) -> UUID:
    """Insert (or fetch) a user row keyed by phone. Returns the user id."""
    user_id = await conn.fetchval(
        """
        INSERT INTO mediator.users (name, phone, timezone)
        VALUES ($1, $2, 'UTC')
        ON CONFLICT (phone) DO UPDATE SET name = EXCLUDED.name
        RETURNING id
        """,
        name,
        phone,
    )
    return user_id


async def _topic_id(conn: Any) -> UUID:
    """Return the seeded ``relationship`` topic id (added by migration 0020)."""
    topic_id = await conn.fetchval(
        "SELECT id FROM mediator.topics WHERE slug = 'relationship';"
    )
    assert topic_id is not None, "expected the relationship topic to be seeded"
    return topic_id


async def _insert_inbound_processing(
    conn: Any,
    *,
    sender_id: UUID,
    recipient_id: UUID | None,
    topic_id: UUID,
    content: str,
    sent_at: datetime,
) -> UUID:
    """Insert an inbound message in processing_state='processing'."""
    return await conn.fetchval(
        """
        INSERT INTO mediator.messages
            (direction, sender_id, recipient_id, content, sent_at,
             processing_state, processing_started_at,
             bot_id, topic_id, processing_attempts)
        VALUES
            ('inbound', $1, $2, $3, $4,
             'processing', $4,
             $5, $6, 1)
        RETURNING id
        """,
        sender_id,
        recipient_id,
        content,
        sent_at,
        BOT_ID,
        topic_id,
    )


async def _insert_outbound(
    conn: Any,
    *,
    sender_id: UUID | None,
    recipient_id: UUID,
    topic_id: UUID,
    content: str,
    sent_at: datetime,
) -> UUID:
    """Insert an outbound message in processing_state='processed'."""
    return await conn.fetchval(
        """
        INSERT INTO mediator.messages
            (direction, sender_id, recipient_id, content, sent_at,
             processing_state, bot_id, topic_id, processing_attempts)
        VALUES
            ('outbound', $1, $2, $3, $4,
             'processed', $5, $6, 0)
        RETURNING id
        """,
        sender_id,
        recipient_id,
        content,
        sent_at,
        BOT_ID,
        topic_id,
    )


async def _open_turn(
    conn: Any,
    *,
    triggering_message_id: UUID,
    user_id: UUID,
    topic_id: UUID,
    started_at: datetime,
    failure_reason: str | None = None,
) -> UUID:
    """Insert a minimal bot_turns row. Returns the turn id."""
    return await conn.fetchval(
        """
        INSERT INTO mediator.bot_turns
            (triggered_by_message_id, triggering_message_ids,
             user_in_context, prompt_snapshot, system_prompt_version,
             model_version, started_at, bot_id, topic_id, failure_reason)
        VALUES
            ($1, ARRAY[$1]::uuid[], $2, 'test prompt snapshot', 'test-v1',
             'test-model', $3, $4, $5, $6)
        RETURNING id
        """,
        triggering_message_id,
        user_id,
        started_at,
        BOT_ID,
        topic_id,
        failure_reason,
    )


async def _close_turn_with_output(
    conn: Any,
    *,
    turn_id: UUID,
    final_output_message_id: UUID | None,
    completed_at: datetime,
) -> None:
    """Mark a turn complete, optionally pointing at an outbound message."""
    await conn.execute(
        """
        UPDATE mediator.bot_turns
        SET final_output_message_id = $2,
            completed_at = $3,
            duration_ms = 100,
            tool_call_count = 0
        WHERE id = $1
        """,
        turn_id,
        final_output_message_id,
        completed_at,
    )


async def _complete_inbound(
    conn: Any,
    *,
    message_id: UUID,
    handling_result: str,
    handled_by_turn_id: UUID | None,
    handled_at: datetime,
) -> None:
    """Mark an inbound message processed with ``handling_result``.

    Wraps the lifecycle-writer GUC because the trigger from migration 0042
    asserts ``next_retry_at``/``failure_class`` may only be cleared by the
    sanctioned writer.
    """
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.lifecycle_writer', 'inbound_queue', true);"
        )
        await conn.execute(
            """
            UPDATE mediator.messages
            SET processing_state   = 'processed',
                handling_result    = $2,
                handled_at         = $3,
                handled_by_turn_id = $4,
                next_retry_at      = NULL,
                failure_class      = NULL
            WHERE id = $1
            """,
            message_id,
            handling_result,
            handled_at,
            handled_by_turn_id,
        )


async def _fail_inbound(
    conn: Any,
    *,
    message_id: UUID,
    processing_error: str,
    handled_by_turn_id: UUID | None,
    failure_class: str,
    next_retry_at: datetime,
) -> None:
    """Mark an inbound message ``failed`` with the lifecycle-writer trigger
    correctly set.

    ``next_retry_at`` is written explicitly (not computed) so tests can pin
    a deterministic value rather than depending on the live backoff math
    in :mod:`app.services.inbound_queue`.
    """
    async with conn.transaction():
        await conn.execute(
            "SELECT set_config('app.lifecycle_writer', 'inbound_queue', true);"
        )
        await conn.execute(
            """
            UPDATE mediator.messages
            SET processing_state   = 'failed',
                processing_error   = $2,
                handling_result    = 'failed',
                handled_by_turn_id = $3::uuid,
                handled_at         = CASE WHEN $3::uuid IS NOT NULL THEN now() ELSE handled_at END,
                failure_class      = $4,
                next_retry_at      = $5
            WHERE id = $1
            """,
            message_id,
            processing_error,
            handled_by_turn_id,
            failure_class,
            next_retry_at,
        )


# ---------------------------------------------------------------------------
# Public fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def replied_turn(pg_pool: Any) -> AsyncIterator[RepliedTurn]:
    """Seed a happy-path replied turn.

    Yields a :class:`RepliedTurn` carrying every row id; tests can drill
    in via ``pg_pool.fetchrow(..., scenario.turn_id)`` etc.
    """
    async with pg_pool.acquire() as conn:
        topic_id = await _topic_id(conn)
        user_id = await _ensure_user(conn, name="Replied User", phone="+15555550200")
        partner_id = await _ensure_user(conn, name="Partner R", phone="+15555550201")

        now = datetime.now(UTC)
        inbound_id = await _insert_inbound_processing(
            conn,
            sender_id=user_id,
            recipient_id=partner_id,
            topic_id=topic_id,
            content="hey, how are things?",
            sent_at=now - timedelta(minutes=5),
        )
        turn_id = await _open_turn(
            conn,
            triggering_message_id=inbound_id,
            user_id=user_id,
            topic_id=topic_id,
            started_at=now - timedelta(minutes=5),
        )
        outbound_id = await _insert_outbound(
            conn,
            sender_id=None,
            recipient_id=user_id,
            topic_id=topic_id,
            content="things are calm — what's coming up for you?",
            sent_at=now - timedelta(minutes=4, seconds=30),
        )
        await _close_turn_with_output(
            conn,
            turn_id=turn_id,
            final_output_message_id=outbound_id,
            completed_at=now - timedelta(minutes=4),
        )
        await _complete_inbound(
            conn,
            message_id=inbound_id,
            handling_result="replied",
            handled_by_turn_id=turn_id,
            handled_at=now - timedelta(minutes=4),
        )

    yield RepliedTurn(
        user_id=user_id,
        topic_id=topic_id,
        inbound_message_id=inbound_id,
        outbound_message_id=outbound_id,
        turn_id=turn_id,
    )


@pytest.fixture
async def silent_turn(pg_pool: Any) -> AsyncIterator[SilentTurn]:
    """Seed a deliberately-silent turn (turn opened, no outbound)."""
    async with pg_pool.acquire() as conn:
        topic_id = await _topic_id(conn)
        user_id = await _ensure_user(conn, name="Silent User", phone="+15555550210")
        partner_id = await _ensure_user(conn, name="Partner S", phone="+15555550211")

        now = datetime.now(UTC)
        inbound_id = await _insert_inbound_processing(
            conn,
            sender_id=user_id,
            recipient_id=partner_id,
            topic_id=topic_id,
            content="(probably-low-charge filler)",
            sent_at=now - timedelta(minutes=3),
        )
        turn_id = await _open_turn(
            conn,
            triggering_message_id=inbound_id,
            user_id=user_id,
            topic_id=topic_id,
            started_at=now - timedelta(minutes=3),
        )
        await _close_turn_with_output(
            conn,
            turn_id=turn_id,
            final_output_message_id=None,
            completed_at=now - timedelta(minutes=2, seconds=55),
        )
        await _complete_inbound(
            conn,
            message_id=inbound_id,
            handling_result="silent",
            handled_by_turn_id=turn_id,
            handled_at=now - timedelta(minutes=2, seconds=55),
        )

    yield SilentTurn(
        user_id=user_id,
        topic_id=topic_id,
        inbound_message_id=inbound_id,
        turn_id=turn_id,
    )


@pytest.fixture
async def failed_pre_send_turn(pg_pool: Any) -> AsyncIterator[FailedPreSendTurn]:
    """Seed a retryable pre-send failure.

    Mirrors the Hector incident shape: the turn started, a tool error or
    provider failure aborted the loop before any outbound was sent, and
    the inbound row is left in ``processing_state='failed'`` with a
    ``retryable_pre_send`` class and a ``next_retry_at`` in the past.
    """
    async with pg_pool.acquire() as conn:
        topic_id = await _topic_id(conn)
        user_id = await _ensure_user(conn, name="Failed User", phone="+15555550220")
        partner_id = await _ensure_user(conn, name="Partner F", phone="+15555550221")

        now = datetime.now(UTC)
        inbound_id = await _insert_inbound_processing(
            conn,
            sender_id=user_id,
            recipient_id=partner_id,
            topic_id=topic_id,
            content="i'm spiralling, talk to me",
            sent_at=now - timedelta(minutes=2),
        )
        failure_reason = "provider_overloaded"
        turn_id = await _open_turn(
            conn,
            triggering_message_id=inbound_id,
            user_id=user_id,
            topic_id=topic_id,
            started_at=now - timedelta(minutes=2),
            failure_reason=failure_reason,
        )
        # No outbound message exists for this scenario; the turn never
        # reached the send step. ``final_output_message_id`` stays NULL.
        await _close_turn_with_output(
            conn,
            turn_id=turn_id,
            final_output_message_id=None,
            completed_at=now - timedelta(minutes=1, seconds=55),
        )
        # Pin next_retry_at to a deterministic instant slightly in the past.
        next_retry_at = now - timedelta(seconds=30)
        await _fail_inbound(
            conn,
            message_id=inbound_id,
            processing_error="anthropic 529 overloaded",
            handled_by_turn_id=turn_id,
            failure_class="retryable_pre_send",
            next_retry_at=next_retry_at,
        )

    yield FailedPreSendTurn(
        user_id=user_id,
        topic_id=topic_id,
        inbound_message_id=inbound_id,
        turn_id=turn_id,
        failure_reason=failure_reason,
        failure_class="retryable_pre_send",
        next_retry_at=next_retry_at,
    )
