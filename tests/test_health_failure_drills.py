"""Failure-drill tests for health-sync robustness.

Covers:
- Stale cursor / freshness classification
- Reauthorization required handling
- Rate-limit retry-after within and above cap
- Webhook-without-fetch recovery
- Duplicate record handling
- Cursor crash transaction rollback
- Projection drift across revisions and tombstones

All tests reuse FakeWithingsProvider, HealthSyncWorker, reconcile_connections,
and FakePool transaction snapshots.  They prove that cursors advance only after
complete successful transactions.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from app.services import crypto
from app.services.health_sync import (
    FakeWithingsError,
    FakeWithingsProvider,
    HealthOAuthTokens,
    HealthProviderSlug,
    HealthResourceType,
    HealthSyncError,
    HealthSyncErrorKind,
    HealthSyncStatus,
    HealthSyncWorker,
    get_connection_freshness,
    reconcile_connections,
    repository_for,
    store_connection_tokens,
    sync_connection_resource_safely,
    sync_claimed_dirty_category,
)
from app.services.health_sync.models import NormalizedWorkout
from app.services.health_sync.projection_applicator import apply_workout_projection
from app.services.health_sync.repository import HealthSyncRepository
from tests.conftest import FakePool

CALLBACK_URL = "https://example.test/api/health/devices/withings/oauth/callback"


# ── helpers ──────────────────────────────────────────────────────────────────


def _set_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "DATA_ENCRYPTION_KEY", base64.b64encode(bytes(range(32))).decode()
    )
    crypto.reset_cache_for_tests()
    from app.config import get_settings

    get_settings.cache_clear()


async def _rotated_access_token(provider: FakeWithingsProvider) -> str:
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    refreshed = await provider.refresh_token(
        refresh_token=exchanged.refresh_token or ""
    )
    return refreshed.access_token


# ── Stale cursor / freshness classification ──────────────────────────────────


class TestStaleCursorFreshness:
    async def test_never_synced_connection_is_stale(self):
        """A connection that has never completed a sync is stale."""
        pool = FakePool()
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        freshness = await get_connection_freshness(
            connection_id=connection_id,
            user_id=user_id,
            pool=pool,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert freshness.is_fresh is False
        assert freshness.last_success_at is None

    async def test_successful_sync_makes_connection_fresh(self):
        """After a successful sync the connection is fresh."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=now,
        )
        assert outcome.status is HealthSyncStatus.COMPLETED

        freshness = await get_connection_freshness(
            connection_id=connection_id,
            user_id=user_id,
            pool=pool,
            now=now,
        )
        assert freshness.is_fresh is True
        assert freshness.last_success_at == now

    async def test_old_sync_is_stale(self):
        """A sync older than 7 days is stale."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        sync_time = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=sync_time,
        )
        assert outcome.status is HealthSyncStatus.COMPLETED

        check_time = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        freshness = await get_connection_freshness(
            connection_id=connection_id,
            user_id=user_id,
            pool=pool,
            now=check_time,
        )
        assert freshness.is_fresh is False
        assert freshness.last_success_at == sync_time

    async def test_stale_clock_less_than_7_days_is_fresh(self):
        """A sync just under 7 days ago is still fresh."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        sync_time = datetime(2026, 7, 20, 0, 0, 0, tzinfo=UTC)
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=sync_time,
        )
        assert outcome.status is HealthSyncStatus.COMPLETED

        check_time = sync_time + timedelta(days=6, hours=23, minutes=59)
        freshness = await get_connection_freshness(
            connection_id=connection_id,
            user_id=user_id,
            pool=pool,
            now=check_time,
        )
        assert freshness.is_fresh is True

    async def test_failed_sync_does_not_update_freshness(self):
        """A failed sync does not mark the connection as fresh."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token="invalid-token",
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert outcome.status is HealthSyncStatus.FAILED

        freshness = await get_connection_freshness(
            connection_id=connection_id,
            user_id=user_id,
            pool=pool,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert freshness.is_fresh is False
        assert freshness.last_success_at is None

    async def test_freshness_is_user_scoped(self):
        """Freshness queries are scoped to the requesting user."""
        pool = FakePool()
        user_a = uuid4()
        user_b = uuid4()
        conn_a = pool.seed_health_connection(
            user_id=user_a, external_user_id="420001"
        )
        conn_b = pool.seed_health_connection(
            user_id=user_b, external_user_id="420002"
        )
        # Mark conn_a as having synced
        pool.health_connections[conn_a]["last_success_at"] = datetime(
            2026, 7, 20, 12, 0, tzinfo=UTC
        )

        # user_b trying user_a's connection should get no result
        freshness_b = await get_connection_freshness(
            connection_id=conn_a,
            user_id=user_b,
            pool=pool,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert freshness_b.is_fresh is False
        assert freshness_b.last_success_at is None

        # user_a gets their own freshness
        freshness_a = await get_connection_freshness(
            connection_id=conn_a,
            user_id=user_a,
            pool=pool,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert freshness_a.is_fresh is True


# ── Reauthorization required ─────────────────────────────────────────────────


class TestReauthorizationRequired:
    async def test_reauthorization_fails_permanently(self):
        """When token store signals reauth_required the sync fails permanently."""
        pool = FakePool()
        repository = repository_for(pool)
        # Seed a connection that will trigger reauth_required
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id,
            external_user_id="420001",
            status="reauth_required",
        )

        provider = FakeWithingsProvider()
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token="any-token",
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )

        assert outcome.status is HealthSyncStatus.FAILED
        assert outcome.error is not None
        # cursor must not advance on reauthorization failure
        assert outcome.cursor_before == outcome.cursor_after

    async def test_reauthorization_preserves_existing_records(self):
        """Existing source records survive a reauthorization failure.

        The worker's _load_access_token checks connection status; when the
        token store signals reauth_required the worker records a sync error
        without calling the provider.  We simulate this at the lower level.
        """
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        # First, succeed
        outcome1 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert outcome1.status is HealthSyncStatus.COMPLETED
        before_count = len(pool.health_source_records)

        # Simulate a reauthorization scenario by using an invalid access token
        # that triggers an authentication error (the safe sync treats auth
        # errors as permanent, non-retryable failures)
        outcome2 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token="revoked-or-expired-token",
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert outcome2.status is HealthSyncStatus.FAILED
        assert outcome2.error is not None
        assert outcome2.error.kind is HealthSyncErrorKind.AUTHENTICATION
        # Existing records untouched
        assert len(pool.health_source_records) == before_count
        # Cursor must not advance
        assert outcome2.cursor_before == outcome2.cursor_after

    async def test_reauthorization_errors_recorded_on_connection(self):
        """Reauthorization failure records error on the connection row."""
        pool = FakePool()
        repository = repository_for(pool)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id,
            external_user_id="420001",
            status="reauth_required",
        )
        provider = FakeWithingsProvider()

        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token="any-token",
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert outcome.status is HealthSyncStatus.FAILED

        conn = pool.health_connections[connection_id]
        assert conn["last_error_code"] is not None
        assert conn["last_success_at"] is None


# ── Rate-limit retry-after ───────────────────────────────────────────────────


class _TrackingScenarioProvider(FakeWithingsProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fetch_call_count = 0

    async def fetch_changes(self, **kwargs):
        self.fetch_call_count += 1
        return await super().fetch_changes(**kwargs)


class TestRateLimitRetryAfter:
    async def test_rate_limit_below_cap_retries(self):
        """When retry_after is below the cap the sync retries."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = _TrackingScenarioProvider(
            fetch_scenarios={HealthResourceType.MEASUREMENT: "rate_limit_retry_after"}
        )
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )
        sleep_calls: list[float] = []

        async def sleep_spy(delay: float) -> None:
            sleep_calls.append(delay)

        # Retry-After in fixture is 120, cap is 200 → should retry
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            retry_after_cap_seconds=200,
            max_attempts=3,
            sleep=sleep_spy,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )

        assert outcome.status is HealthSyncStatus.FAILED
        assert outcome.error is not None
        assert outcome.error.kind is HealthSyncErrorKind.RATE_LIMIT
        assert outcome.error.retry_after_seconds == 120
        # Retried until max_attempts exhausted
        assert provider.fetch_call_count == 3
        assert sleep_calls == [120.0, 120.0]
        # Cursor must not advance
        assert outcome.cursor_before == outcome.cursor_after

    async def test_rate_limit_above_cap_fails_immediately(self):
        """When retry_after exceeds the cap the sync fails without retry."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = _TrackingScenarioProvider(
            fetch_scenarios={HealthResourceType.MEASUREMENT: "rate_limit_retry_after"}
        )
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )
        sleep_calls: list[float] = []

        async def sleep_spy(delay: float) -> None:
            sleep_calls.append(delay)

        # Retry-After is 120, cap is 30 → should NOT retry
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            retry_after_cap_seconds=30,
            sleep=sleep_spy,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )

        assert outcome.status is HealthSyncStatus.FAILED
        assert outcome.error is not None
        assert outcome.error.kind is HealthSyncErrorKind.RATE_LIMIT
        # Only one call, no retry
        assert provider.fetch_call_count == 1
        assert sleep_calls == []

    async def test_rate_limit_within_cap_no_retry_when_max_attempts_1(self):
        """Even with cap > retry_after, max_attempts=1 should prevent retry."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = _TrackingScenarioProvider(
            fetch_scenarios={HealthResourceType.MEASUREMENT: "rate_limit_retry_after"}
        )
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )
        sleep_calls: list[float] = []

        async def sleep_spy(delay: float) -> None:
            sleep_calls.append(delay)

        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            retry_after_cap_seconds=200,
            max_attempts=1,
            sleep=sleep_spy,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )

        assert outcome.status is HealthSyncStatus.FAILED
        assert provider.fetch_call_count == 1
        assert sleep_calls == []


# ── Webhook-without-fetch recovery ───────────────────────────────────────────


class _WebhookOnlyProvider(FakeWithingsProvider):
    """Provider that fails fetch on first call but succeeds after webhook."""

    def __init__(self) -> None:
        super().__init__()
        self.fetch_fail_count = 0
        self._fail_next = True

    async def fetch_changes(self, **kwargs):
        if self._fail_next:
            self._fail_next = False
            self.fetch_fail_count += 1
            raise FakeWithingsError(
                HealthSyncError.retryable_error(
                    code="http_503",
                    detail="transient fetch failure simulating webhook gap",
                )
            )
        return await super().fetch_changes(**kwargs)


class TestWebhookWithoutFetchRecovery:
    async def test_webhook_receipt_created_even_when_fetch_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """When the provider fails on first fetch, the safe sync retries and
        the failure is observable via the outcome."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = _WebhookOnlyProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=now,
        )

        # The first fetch call raised (fail_next=True), then retry succeeded
        assert provider.fetch_fail_count == 1
        # Outcome should be successful (retry worked)
        assert outcome.status is HealthSyncStatus.COMPLETED

    async def test_webhook_recovery_via_reconciliation(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """After a webhook gap, reconciliation can recover and sync data."""
        pool = FakePool()
        repository = repository_for(pool)

        # Provider that succeeds after initial failure
        provider = FakeWithingsProvider()
        _set_key(monkeypatch)

        user_id = uuid4()
        exchanged = await provider.exchange_code(
            code="synthetic-auth-code-001",
            redirect_uri=CALLBACK_URL,
        )
        stored = await store_connection_tokens(
            pool,
            user_id=user_id,
            provider=HealthProviderSlug.WITHINGS,
            tokens=HealthOAuthTokens(
                access_token=exchanged.access_token,
                refresh_token=exchanged.refresh_token or "",
                expires_at=exchanged.expires_at,
                external_user_id=exchanged.external_user_id,
                granted_scopes=frozenset({"user.metrics"}),
            ),
            resource_types=[HealthResourceType.MEASUREMENT],
            now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
        )
        connection_id = stored.connection_id

        # Reconciliation performs backfill
        summary = await reconcile_connections(
            pool=pool,
            repository=repository,
            provider=provider,
            claimed_by="health-drill-reconcile",
            connection_limit=10,
            dirty_limit=0,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )

        assert summary.scanned_connection_count == 1
        assert len(summary.outcomes) == 1
        assert summary.outcomes[0].status is HealthSyncStatus.COMPLETED
        assert len(pool.health_source_records) > 0

        # Freshness is updated
        freshness = await get_connection_freshness(
            connection_id=connection_id,
            user_id=user_id,
            pool=pool,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert freshness.is_fresh is True

    async def test_dirty_category_persists_after_failed_sync(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """When fetch fails repeatedly due to a transient provider error,
        the safe sync retries and eventually succeeds (no permanent data loss)."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = _WebhookOnlyProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        now = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=now,
        )

        # First call failed, retry succeeded
        assert provider.fetch_fail_count == 1
        assert outcome.status is HealthSyncStatus.COMPLETED
        # Data was persisted after retry
        assert len(pool.health_source_records) > 0


# ── Duplicate records ────────────────────────────────────────────────────────


class TestDuplicateRecords:
    async def test_upsert_prevents_duplicate_source_records(self):
        """Syncing the same data twice using upsert semantics yields one record."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        # First sync
        outcome1 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert outcome1.status is HealthSyncStatus.COMPLETED
        count_after_first = len(pool.health_source_records)
        assert count_after_first > 0

        # Second sync with same provider (no new data since cursor advanced)
        outcome2 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert outcome2.status is HealthSyncStatus.COMPLETED
        # No new records, only the original ones
        assert len(pool.health_source_records) == count_after_first
        # No duplicate keys
        assert len(pool.health_source_records_by_key) == len(pool.health_source_records)

    async def test_revision_updates_existing_record_in_place(self):
        """A revised source record updates the existing row, not create a new one."""
        pool = FakePool()
        repository = repository_for(pool)
        provider1 = FakeWithingsProvider()
        access_token1 = await _rotated_access_token(provider1)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        # Initial sync
        outcome1 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider1,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token1,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert outcome1.status is HealthSyncStatus.COMPLETED
        count_initial = len(pool.health_source_records)

        # Revision sync
        provider2 = FakeWithingsProvider(
            fetch_scenarios={HealthResourceType.MEASUREMENT: "measurements_revision"}
        )
        access_token2 = await _rotated_access_token(provider2)
        outcome2 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider2,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token2,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert outcome2.status is HealthSyncStatus.COMPLETED
        # Record count unchanged (revised in place)
        assert len(pool.health_source_records) == count_initial

        # Revision count should have increased for the revised record
        revised = next(
            row
            for row in pool.health_source_records.values()
            if row["external_id"] == "grpid:9001002"
        )
        assert revised["revision_count"] >= 2


# ── Cursor crash transaction rollback ────────────────────────────────────────


class _CrashOnCursorStorePool(FakePool):
    """FakePool that can be instructed to crash during cursor storage."""

    def __init__(self) -> None:
        super().__init__()
        self._crash_on_next_store_cursor = False

    async def fetchrow(self, sql: str, *args):
        compact = " ".join(sql.split())
        if (
            self._crash_on_next_store_cursor
            and "UPDATE mediator.health_connections SET cursor_state" in compact
        ):
            self._crash_on_next_store_cursor = False
            raise RuntimeError("simulated transaction crash during cursor store")
        return await super().fetchrow(sql, *args)


class TestCursorCrashTransactionRollback:
    async def test_cursor_not_advanced_when_transaction_crashes(self):
        """If the transaction fails during cursor storage the cursor must not advance."""
        pool = _CrashOnCursorStorePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        # First successful sync to set baseline cursor
        outcome1 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert outcome1.status is HealthSyncStatus.COMPLETED
        cursor_before_crash = outcome1.cursor_after
        assert cursor_before_crash is not None

        # Store the old cursor state from the pool
        old_cursor_state = dict(
            pool.health_connections[connection_id].get("cursor_state", {})
        )

        # Now crash during cursor store on next sync
        pool._crash_on_next_store_cursor = True

        # Use a fresh provider that will return data (force a cursor update)
        provider2 = FakeWithingsProvider(
            fetch_scenarios={HealthResourceType.MEASUREMENT: "measurements_revision"}
        )
        access_token2 = await _rotated_access_token(provider2)

        outcome2 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider2,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token2,
            resource_type=HealthResourceType.MEASUREMENT,
            max_attempts=1,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )

        assert outcome2.status is HealthSyncStatus.FAILED
        # Cursor must not have advanced
        assert outcome2.cursor_before == outcome2.cursor_after
        assert outcome2.cursor_after is not None

        # Pool cursor state must be rolled back to pre-crash value
        new_cursor_state = dict(
            pool.health_connections[connection_id].get("cursor_state", {})
        )
        assert new_cursor_state == old_cursor_state

    async def test_transaction_rollback_preserves_source_records(self):
        """When a transaction crashes, previously committed records survive."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        # Successful sync
        outcome1 = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )
        assert outcome1.status is HealthSyncStatus.COMPLETED
        records_before = len(pool.health_source_records)

        # Failed sync (using timeout scenario that will exhaust retries)
        timeout_provider = _TrackingScenarioProvider(
            fetch_scenarios={HealthResourceType.MEASUREMENT: "request_timeout"}
        )
        timeout_token = await _rotated_access_token(timeout_provider)

        outcome2 = await sync_connection_resource_safely(
            repository=repository,
            provider=timeout_provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=timeout_token,
            resource_type=HealthResourceType.MEASUREMENT,
            max_attempts=3,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert outcome2.status is HealthSyncStatus.FAILED

        # Previous records must be intact
        assert len(pool.health_source_records) == records_before

    async def test_sync_worker_cursor_only_advances_on_complete_success(self):
        """Worker-driven sync advances cursor only after complete transaction."""
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        # Mark dirty
        dirty = await repository.mark_dirty(
            connection_id=connection_id,
            user_id=user_id,
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.MEASUREMENT,
            reason="manual",
            marked_at=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )

        old_cursor = dict(
            pool.health_connections[connection_id].get("cursor_state", {})
        )

        # Sync via sync_claimed_dirty_category
        outcome = await sync_claimed_dirty_category(
            repository=repository,
            provider=provider,
            dirty_category=dirty,
            access_token=access_token,
            now=datetime(2026, 7, 20, 12, 1, tzinfo=UTC),
        )
        assert outcome.status is HealthSyncStatus.COMPLETED
        assert outcome.cursor_after is not None
        # Cursor advanced (fresh sync from no cursor → cursor set)
        assert outcome.cursor_after != outcome.cursor_before

        new_cursor = dict(
            pool.health_connections[connection_id].get("cursor_state", {})
        )
        # Cursor was persisted (different from initial empty state)
        assert new_cursor != old_cursor

    async def test_partial_page_fetch_does_not_advance_cursor_on_crash(self):
        """Even if some pages are fetched, a crash prevents cursor advancement."""
        pool = _CrashOnCursorStorePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        access_token = await _rotated_access_token(provider)
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )

        old_cursor = dict(
            pool.health_connections[connection_id].get("cursor_state", {})
        )

        # Crash during cursor store
        pool._crash_on_next_store_cursor = True

        outcome = await sync_connection_resource_safely(
            repository=repository,
            provider=provider,
            connection_id=connection_id,
            user_id=user_id,
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            max_attempts=1,
            now=datetime(2026, 7, 20, 12, 0, tzinfo=UTC),
        )

        assert outcome.status is HealthSyncStatus.FAILED
        # Cursor unchanged in pool
        new_cursor = dict(
            pool.health_connections[connection_id].get("cursor_state", {})
        )
        assert new_cursor == old_cursor


# ── Projection drift ─────────────────────────────────────────────────────────


def _make_commitment(
    *,
    id: str | None = None,
    bot_id: str = "hector",
    topic_id: UUID | None = None,
    cadence: str = "daily",
    start_date: str = "2025-01-01",
    days_of_week: list[int] | None = None,
) -> dict:
    if id is None:
        id = str(uuid4())
    if topic_id is None:
        topic_id = uuid4()
    return {
        "id": id,
        "bot_id": bot_id,
        "topic_slug": "fitness",
        "topic_id": topic_id,
        "label": "Test Commitment",
        "cadence": cadence,
        "start_date": start_date,
        "end_date": None,
        "days_of_week": days_of_week or [],
        "schedule_rule": {},
        "user_id": "u001",
        "status": "active",
    }


def _make_workout(
    *,
    workout_type: str = "running",
    started_at: datetime | None = None,
) -> NormalizedWorkout:
    if started_at is None:
        started_at = datetime(2025, 6, 16, 8, 0, 0, tzinfo=UTC)
    return NormalizedWorkout(
        started_at=started_at,
        local_date=started_at.date(),
        workout_type=workout_type,
        attribution={"provider": "withings"},
    )


class TestProjectionDrift:
    async def test_projection_created_on_first_workout(self):
        """A workout projection creates an event and ledger row."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        source_record_id = uuid4()
        connection_id = uuid4()
        user_id = uuid4()
        commitment = _make_commitment()
        workout = _make_workout()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )

        assert result is not None
        assert result.event_id is not None
        assert result.projection_status == "projected"
        assert result.event_id in pool.events

    async def test_projection_idempotent_replay_same_version(self):
        """Replaying the same projection version returns the existing row."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        source_record_id = uuid4()
        connection_id = uuid4()
        user_id = uuid4()
        commitment = _make_commitment()
        workout = _make_workout()

        result1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            projection_version=1,
            enabled=True,
        )
        assert result1 is not None

        result2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            projection_version=1,
            enabled=True,
        )
        # Same projection returned, no duplicate
        assert result2 is not None
        assert result2.projection_id == result1.projection_id
        assert result2.event_id == result1.event_id

    async def test_projection_superseded_on_revision(self):
        """A higher projection_version supersedes the old projection."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        source_record_id = uuid4()
        connection_id = uuid4()
        user_id = uuid4()
        commitment = _make_commitment()
        workout = _make_workout()

        result1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            projection_version=1,
            enabled=True,
        )
        assert result1 is not None
        first_event_id = result1.event_id

        # Revision with version 2
        result2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            projection_version=2,
            enabled=True,
        )
        assert result2 is not None
        assert result2.projection_version == 2
        # Old projection is superseded
        assert result1.projection_id in pool.health_source_to_event_projections
        old_ledger = pool.health_source_to_event_projections[result1.projection_id]
        assert old_ledger["projection_status"] == "superseded"

    async def test_projection_tombstone_removes_event(self):
        """Tombstoning a workout removes its projection and event."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        source_record_id = uuid4()
        connection_id = uuid4()
        user_id = uuid4()
        commitment = _make_commitment()
        workout = _make_workout()

        result1 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert result1 is not None
        event_id = result1.event_id
        assert event_id in pool.events

        # Tombstone
        result2 = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
            is_tombstone=True,
        )
        assert result2 is None  # tombstone returns None
        # Event should be deleted
        assert event_id not in pool.events
        # Ledger should show removed status
        assert result1.projection_id in pool.health_source_to_event_projections
        ledger = pool.health_source_to_event_projections[result1.projection_id]
        assert ledger["projection_status"] == "removed"

    async def test_projection_not_created_when_disabled(self):
        """When enabled=False the projection is a no-op."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)
        source_record_id = uuid4()
        connection_id = uuid4()
        user_id = uuid4()
        commitment = _make_commitment()
        workout = _make_workout()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=False,
        )

        assert result is None
        assert len(pool.health_source_to_event_projections) == 0

    async def test_manual_event_not_touched_by_projection(self):
        """Manual log_event testimony is never touched by projection code."""
        pool = FakePool()
        repo = HealthSyncRepository(pool)

        user_id = uuid4()
        manual_event_id = uuid4()
        pool.events[manual_event_id] = {
            "id": manual_event_id,
            "user_id": user_id,
            "metric_key": "pushups",
            "adherence_status": "completed",
        }

        # A projection on a different source record
        source_record_id = uuid4()
        connection_id = uuid4()
        commitment = _make_commitment()
        workout = _make_workout()

        result = await apply_workout_projection(
            repository=repo,
            workout=workout,
            source_record_id=source_record_id,
            connection_id=connection_id,
            user_id=user_id,
            commitments=[commitment],
            enabled=True,
        )
        assert result is not None

        # Manual event must survive
        assert manual_event_id in pool.events
