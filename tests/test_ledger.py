"""Real-Postgres tests for the inbound_handling_attempts ledger (Project C, C2).

These tests use the ``pg_pool`` fixture from ``tests/fixtures/postgres.py``
and run against a live ``postgres:16`` container (or whatever
``TEST_DATABASE_URL`` points at).  They are tagged ``postgres`` so they
auto-skip when Docker is unavailable and ``TEST_DATABASE_URL`` is not set.

Coverage
--------
* Dual-write flag OFF      → no ledger rows written on claim/complete/fail.
* Dual-write flag ON       → claim opens 'active', complete -> 'succeeded',
                             fail -> 'failed' with failure_class +
                             failure_reason + next_retry_at copied.
* Partial UNIQUE index     → two 'active' rows for the same message_id
                             cannot coexist.
* Backfill idempotence     → run twice, no duplicates.
* Reconciliation           → opens 'catch_up' rows for stuck 'processing'
                             messages lacking an active ledger entry.
"""

from __future__ import annotations

import importlib
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest


pytestmark = pytest.mark.postgres


# ── helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture
def app_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed the env vars the Settings pydantic model requires."""
    REQUIRED_ENV = {
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/db",
        "DATABASE_SCHEMA": "mediator",
        "SUPABASE_URL": "https://example.supabase.co",
        "SUPABASE_SERVICE_ROLE_KEY": "dummy-service-role",
        "ANTHROPIC_API_KEY": "dummy-anthropic",
        "OPENAI_API_KEY": "dummy-openai",
        "GROQ_API_KEY": "dummy-groq",
        "WHATSAPP_TOKEN": "dummy-whatsapp",
        "WHATSAPP_BEARER_TOKEN": "dummy-whatsapp",
        "WHATSAPP_PHONE_NUMBER_ID": "12345",
        "WHATSAPP_VERIFY_TOKEN": "dummy-verify",
        "WHATSAPP_APP_SECRET": "dummy-secret",
        "WHATSAPP_API_VERSION": "v20.0",
        "MESSAGING_PROVIDER": "meta",
        "ADMIN_PASSWORD": "dummy-admin",
        "PARTNER_PHONE_A": "15555550100",
        "PARTNER_PHONE_B": "15555550101",
        "SUPABASE_STORAGE_BUCKET": "mediator-media",
        "MEDIA_FETCH_TIMEOUT_S": "30",
        "DEFAULT_USER_TIMEZONE": "UTC",
    }
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _seed_message(pg_pool, *, bot_id: str, topic_id: UUID, state: str = "raw") -> UUID:
    """Insert a fresh inbound messages row and return its id."""
    msg_id = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (id, direction, content, sent_at,
                                  processing_state, processing_attempts,
                                  bot_id, topic_id)
            VALUES ($1, 'inbound', 'hi', now(), $2, 0, $3, $4)
            """,
            msg_id,
            state,
            bot_id,
            topic_id,
        )
    return msg_id


async def _topic_id(pg_pool) -> UUID:
    """Return a topic id that exists in the seeded fixture DB."""
    return await pg_pool.fetchval(
        "SELECT id FROM topics WHERE slug = 'relationship'"
    )


async def _count_ledger(pg_pool, message_id: UUID) -> int:
    return await pg_pool.fetchval(
        "SELECT count(*) FROM inbound_handling_attempts WHERE message_id = $1",
        message_id,
    )


async def _ledger_rows(pg_pool, message_id: UUID) -> list[dict]:
    rows = await pg_pool.fetch(
        "SELECT * FROM inbound_handling_attempts "
        "WHERE message_id = $1 ORDER BY started_at ASC",
        message_id,
    )
    return [dict(r) for r in rows]


def _enable_dual_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_DUAL_WRITE_ENABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()


def _disable_dual_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEDGER_DUAL_WRITE_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()


# ── tests ────────────────────────────────────────────────────────────────────


async def test_dual_write_off_writes_no_ledger_rows(
    pg_pool, app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_dual_write(monkeypatch)
    from app.services.inbound_queue import (
        claim_messages_for_turn,
        complete_messages,
        fail_messages,
    )

    topic_id = await _topic_id(pg_pool)
    bot_id = "mediator"

    # claim
    raw_id = await _seed_message(pg_pool, bot_id=bot_id, topic_id=topic_id)
    claimed = await claim_messages_for_turn(
        pg_pool, [raw_id], bot_id=bot_id, topic_id=topic_id
    )
    assert claimed == [raw_id]
    assert await _count_ledger(pg_pool, raw_id) == 0

    # complete
    await complete_messages(
        pg_pool, [raw_id], handling_result="replied", bot_id=bot_id, topic_id=topic_id
    )
    assert await _count_ledger(pg_pool, raw_id) == 0

    # fail
    fail_id = await _seed_message(pg_pool, bot_id=bot_id, topic_id=topic_id)
    await claim_messages_for_turn(
        pg_pool, [fail_id], bot_id=bot_id, topic_id=topic_id
    )
    await fail_messages(
        pg_pool,
        [fail_id],
        processing_error="boom",
        bot_id=bot_id,
        topic_id=topic_id,
        failure_class="retryable_pre_send",
        failure_reason="provider_send_failed",
    )
    assert await _count_ledger(pg_pool, fail_id) == 0


async def test_dual_write_on_records_claim_complete_fail(
    pg_pool, app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_dual_write(monkeypatch)
    from app.services.inbound_queue import (
        claim_messages_for_turn,
        complete_messages,
        fail_messages,
    )

    topic_id = await _topic_id(pg_pool)
    bot_id = "mediator"

    # --- claim opens an 'active' row -------------------------------------
    msg_id = await _seed_message(pg_pool, bot_id=bot_id, topic_id=topic_id)
    claimed = await claim_messages_for_turn(
        pg_pool, [msg_id], bot_id=bot_id, topic_id=topic_id
    )
    assert claimed == [msg_id]
    rows = await _ledger_rows(pg_pool, msg_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["created_by"] == "live"
    assert rows[0]["bot_id"] == bot_id
    assert rows[0]["attempt_number"] == 1
    assert rows[0]["completed_at"] is None

    # --- complete flips the row to 'succeeded' ----------------------------
    await complete_messages(
        pg_pool, [msg_id], handling_result="replied", bot_id=bot_id, topic_id=topic_id
    )
    rows = await _ledger_rows(pg_pool, msg_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "succeeded"
    assert rows[0]["completed_at"] is not None
    assert rows[0]["failure_class"] is None
    assert rows[0]["failure_reason"] is None

    # --- fail flips a fresh attempt to 'failed' with class+reason --------
    fail_id = await _seed_message(pg_pool, bot_id=bot_id, topic_id=topic_id)
    await claim_messages_for_turn(
        pg_pool, [fail_id], bot_id=bot_id, topic_id=topic_id
    )
    await fail_messages(
        pg_pool,
        [fail_id],
        processing_error="provider_send_failed: timeout",
        bot_id=bot_id,
        topic_id=topic_id,
        failure_class="retryable_pre_send",
        failure_reason="llm_timeout",
    )
    rows = await _ledger_rows(pg_pool, fail_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert rows[0]["failure_class"] == "retryable_pre_send"
    assert rows[0]["failure_reason"] == "llm_timeout"
    # next_retry_at is copied from messages (set by the fail_messages CASE).
    assert rows[0]["next_retry_at"] is not None


async def test_unique_active_per_message_index_enforced(pg_pool, app_env) -> None:
    """The partial UNIQUE index forbids two 'active' rows for one message."""
    import asyncpg

    topic_id = await _topic_id(pg_pool)
    bot_id = "mediator"
    msg_id = await _seed_message(pg_pool, bot_id=bot_id, topic_id=topic_id)

    async with pg_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO inbound_handling_attempts "
            "(message_id, bot_id, topic_id, attempt_number, status, created_by) "
            "VALUES ($1, $2, $3, 1, 'active', 'live')",
            msg_id,
            bot_id,
            topic_id,
        )

        with pytest.raises(asyncpg.exceptions.UniqueViolationError):
            await conn.execute(
                "INSERT INTO inbound_handling_attempts "
                "(message_id, bot_id, topic_id, attempt_number, status, created_by) "
                "VALUES ($1, $2, $3, 2, 'active', 'live')",
                msg_id,
                bot_id,
                topic_id,
            )

        # Switching the first to 'failed' frees the partial-unique slot.
        await conn.execute(
            "UPDATE inbound_handling_attempts SET status='failed', "
            "completed_at=now() WHERE message_id=$1 AND status='active'",
            msg_id,
        )
        await conn.execute(
            "INSERT INTO inbound_handling_attempts "
            "(message_id, bot_id, topic_id, attempt_number, status, created_by) "
            "VALUES ($1, $2, $3, 2, 'active', 'recovery')",
            msg_id,
            bot_id,
            topic_id,
        )


async def test_backfill_idempotent(pg_pool, app_env) -> None:
    """Run the backfill twice; the second run inserts nothing new."""
    from scripts.backfill_inbound_handling_attempts import backfill_loop

    topic_id = await _topic_id(pg_pool)
    bot_id = "mediator"
    # Seed two messages with no ledger rows.
    raw_id = await _seed_message(pg_pool, bot_id=bot_id, topic_id=topic_id)
    failed_id = await _seed_message(
        pg_pool, bot_id=bot_id, topic_id=topic_id, state="failed"
    )

    first = await backfill_loop(
        pg_pool, batch_size=100, dry_run=False, max_batches=10
    )
    total_first = first.get("active", 0) + first.get("failed", 0)
    assert total_first >= 2, first

    after_first = await pg_pool.fetchval(
        "SELECT count(*) FROM inbound_handling_attempts"
    )
    # Run again; idempotence means no new rows.
    second = await backfill_loop(
        pg_pool, batch_size=100, dry_run=False, max_batches=10
    )
    assert second.get("active", 0) == 0
    assert second.get("failed", 0) == 0
    after_second = await pg_pool.fetchval(
        "SELECT count(*) FROM inbound_handling_attempts"
    )
    assert after_second == after_first

    # Spot-check: the raw row maps to status='active', the failed to 'failed'.
    raw_status = await pg_pool.fetchval(
        "SELECT status FROM inbound_handling_attempts WHERE message_id = $1",
        raw_id,
    )
    assert raw_status == "active"
    failed_status = await pg_pool.fetchval(
        "SELECT status FROM inbound_handling_attempts WHERE message_id = $1",
        failed_id,
    )
    assert failed_status == "failed"


async def test_reconcile_opens_catch_up_rows_for_stuck_processing(
    pg_pool, app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_dual_write(monkeypatch)
    from app.services.inbound_queue import reconcile_ledger_active_attempts

    topic_id = await _topic_id(pg_pool)
    bot_id = "mediator"

    # Seed a message stuck in 'processing' with no ledger entry.  Use
    # set_config so the writer-marker trigger doesn't fire on the insert
    # path (it shouldn't, since failure_class/next_retry_at aren't touched).
    stuck_id = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (id, direction, content, sent_at,
                                  processing_state, processing_started_at,
                                  processing_attempts, bot_id, topic_id)
            VALUES ($1, 'inbound', 'stuck', now(), 'processing', now(),
                    2, $2, $3)
            """,
            stuck_id,
            bot_id,
            topic_id,
        )

    inserted = await reconcile_ledger_active_attempts(pg_pool)
    assert inserted >= 1

    rows = await _ledger_rows(pg_pool, stuck_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "active"
    assert rows[0]["created_by"] == "catch_up"
    assert rows[0]["attempt_number"] == 2

    # Re-running reconciliation is a no-op now that an active row exists.
    inserted_again = await reconcile_ledger_active_attempts(pg_pool)
    assert inserted_again == 0


async def test_reconcile_noop_when_flag_off(
    pg_pool, app_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _disable_dual_write(monkeypatch)
    from app.services.inbound_queue import reconcile_ledger_active_attempts

    topic_id = await _topic_id(pg_pool)
    stuck_id = uuid4()
    async with pg_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO messages (id, direction, content, sent_at,
                                  processing_state, processing_started_at,
                                  processing_attempts, bot_id, topic_id)
            VALUES ($1, 'inbound', 'stuck', now(), 'processing', now(),
                    1, 'mediator', $2)
            """,
            stuck_id,
            topic_id,
        )
    inserted = await reconcile_ledger_active_attempts(pg_pool)
    assert inserted == 0
    assert await _count_ledger(pg_pool, stuck_id) == 0
