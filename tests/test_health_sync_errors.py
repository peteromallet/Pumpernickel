from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from app.services.health_sync import (
    FakeWithingsError,
    FakeWithingsProvider,
    HealthFetchResult,
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    HealthSyncCursor,
    HealthSyncError,
    HealthSyncErrorKind,
    HealthSyncStatus,
    WITHINGS_PROVIDER_CAPABILITIES,
    repository_for,
    sync_connection_resource_safely,
)
from tests.conftest import FakePool


CALLBACK_URL = "https://example.test/api/health/devices/withings/oauth/callback"


async def _rotated_access_token(provider: FakeWithingsProvider) -> str:
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")
    return refreshed.access_token


class _TrackingScenarioProvider(FakeWithingsProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fetch_call_count = 0

    async def fetch_changes(self, **kwargs):  # type: ignore[override]
        self.fetch_call_count += 1
        return await super().fetch_changes(**kwargs)


class _FlakyTransientProvider(FakeWithingsProvider):
    def __init__(self) -> None:
        super().__init__()
        self.fetch_call_count = 0

    async def fetch_changes(self, **kwargs):  # type: ignore[override]
        self.fetch_call_count += 1
        if self.fetch_call_count == 1:
            raise FakeWithingsError(
                HealthSyncError.retryable_error(
                    code="http_503",
                    detail="fixture replay hit a transient upstream failure",
                )
            )
        return await super().fetch_changes(**kwargs)


class _PermanentFailureProvider:
    name = "withings"
    capabilities = WITHINGS_PROVIDER_CAPABILITIES

    def __init__(self) -> None:
        self.fetch_call_count = 0

    async def exchange_code(self, *, code: str, redirect_uri: str):
        raise NotImplementedError

    async def refresh_token(self, *, refresh_token: str):
        raise NotImplementedError

    async def fetch_changes(self, *, access_token: str, resource_type: HealthResourceType, cursor):
        self.fetch_call_count += 1
        raise RuntimeError(
            HealthSyncError.permanent_error(
                code="provider_rejected_request",
                detail="provider rejected the sync request",
            )
        )

    async def revoke(self, *, access_token: str, refresh_token: str | None = None) -> None:
        raise NotImplementedError


async def test_safe_sync_authentication_error_is_bounded_and_sanitized() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = FakeWithingsProvider()
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")

    outcome = await sync_connection_resource_safely(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token="invalid-access-token",
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 9, 0, tzinfo=UTC),
    )

    assert outcome.status is HealthSyncStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.kind is HealthSyncErrorKind.AUTHENTICATION
    assert outcome.error.code == "invalid_access_token"
    assert pool.health_source_records == {}
    persisted = pool.health_connections[connection_id]
    assert persisted["last_error_code"] == "invalid_access_token"
    assert persisted["last_error_detail"] == "access token rejected"
    assert persisted["last_success_at"] is None


async def test_safe_sync_rate_limit_honors_retry_after_cap_without_looping() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = _TrackingScenarioProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "rate_limit_retry_after"}
    )
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
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
        retry_after_cap_seconds=30,
        sleep=sleep_spy,
        now=datetime(2026, 7, 20, 9, 5, tzinfo=UTC),
    )

    assert outcome.status is HealthSyncStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.kind is HealthSyncErrorKind.RATE_LIMIT
    assert outcome.error.retry_after_seconds == 120
    assert provider.fetch_call_count == 1
    assert sleep_calls == []


async def test_safe_sync_retries_transient_failures_then_succeeds() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = _FlakyTransientProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
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
        sleep=sleep_spy,
        now=datetime(2026, 7, 20, 9, 10, tzinfo=UTC),
    )

    assert outcome.status is HealthSyncStatus.COMPLETED
    assert outcome.page_count == 2
    assert provider.fetch_call_count == 3
    assert sleep_calls == [1.0]
    persisted = pool.health_connections[connection_id]
    assert persisted["last_success_at"] == datetime(2026, 7, 20, 9, 10, tzinfo=UTC)
    assert persisted["last_error_code"] is None
    assert len(pool.health_source_records) == 2


async def test_safe_sync_timeouts_retry_with_bounded_backoff_then_fail() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = _TrackingScenarioProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "request_timeout"}
    )
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
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
        max_attempts=3,
        sleep=sleep_spy,
        now=datetime(2026, 7, 20, 9, 15, tzinfo=UTC),
    )

    assert outcome.status is HealthSyncStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.kind is HealthSyncErrorKind.TRANSIENT
    assert outcome.error.code == "timeout"
    assert provider.fetch_call_count == 3
    assert sleep_calls == [1.0, 2.0]
    assert pool.health_source_records == {}
    assert pool.health_connections[connection_id]["last_error_code"] == "timeout"


async def test_safe_sync_permanent_errors_do_not_retry() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = _PermanentFailureProvider()
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
    sleep_calls: list[float] = []

    async def sleep_spy(delay: float) -> None:
        sleep_calls.append(delay)

    outcome = await sync_connection_resource_safely(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token="unused",
        resource_type=HealthResourceType.MEASUREMENT,
        sleep=sleep_spy,
        now=datetime(2026, 7, 20, 9, 20, tzinfo=UTC),
    )

    assert outcome.status is HealthSyncStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.kind is HealthSyncErrorKind.PERMANENT
    assert provider.fetch_call_count == 1
    assert sleep_calls == []


async def test_safe_sync_malformed_provider_responses_do_not_retry() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = _TrackingScenarioProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "malformed_measurements_body"}
    )
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(user_id=user_id, external_user_id="420001")
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
        sleep=sleep_spy,
        now=datetime(2026, 7, 20, 9, 25, tzinfo=UTC),
    )

    assert outcome.status is HealthSyncStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.kind is HealthSyncErrorKind.MALFORMED_RESPONSE
    assert provider.fetch_call_count == 1
    assert sleep_calls == []


async def test_safe_sync_invalid_cursor_fails_without_provider_fetch() -> None:
    pool = FakePool()
    repository = repository_for(pool)
    provider = _TrackingScenarioProvider()
    access_token = await _rotated_access_token(provider)
    user_id = uuid4()
    connection_id = pool.seed_health_connection(
        user_id=user_id,
        external_user_id="420001",
        cursor_state={
            "measurement": {
                "resource_type": "measurement",
                "last_modified": "2026-07-20T07:00:00Z",
                "page_offset": "not-an-int",
            }
        },
    )

    outcome = await sync_connection_resource_safely(
        repository=repository,
        provider=provider,
        connection_id=connection_id,
        user_id=user_id,
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        now=datetime(2026, 7, 20, 9, 30, tzinfo=UTC),
    )

    assert outcome.status is HealthSyncStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.kind is HealthSyncErrorKind.INVALID_CURSOR_STATE
    assert provider.fetch_call_count == 0
    assert pool.health_source_records == {}
