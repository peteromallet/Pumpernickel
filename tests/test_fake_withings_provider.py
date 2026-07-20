from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.health_sync import (
    FakeWithingsError,
    FakeWithingsProvider,
    HealthResourceType,
    HealthSyncCursor,
    HealthSyncErrorKind,
)


CALLBACK_URL = "https://example.test/api/health/devices/withings/oauth/callback"


async def _rotated_access_token(provider: FakeWithingsProvider) -> str:
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")
    return refreshed.access_token


async def test_exchange_refresh_and_revoke_replay_token_rotation() -> None:
    provider = FakeWithingsProvider()

    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )

    assert exchanged.access_token == "synthetic-access-token-v1"
    assert exchanged.refresh_token == "synthetic-refresh-token-v1"
    assert exchanged.external_user_id == "420001"
    assert exchanged.granted_scopes == frozenset({"user.info", "user.metrics", "user.activity"})
    assert exchanged.expires_at == datetime(2026, 7, 20, 3, 0, tzinfo=UTC)

    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")

    assert refreshed.access_token == "synthetic-access-token-v2"
    assert refreshed.refresh_token == "synthetic-refresh-token-v2"
    assert refreshed.expires_at == datetime(2026, 7, 20, 3, 0, 1, tzinfo=UTC)

    with pytest.raises(FakeWithingsError) as stale_refresh:
        await provider.refresh_token(refresh_token="synthetic-refresh-token-v1")
    assert stale_refresh.value.error.kind is HealthSyncErrorKind.AUTHENTICATION
    assert stale_refresh.value.error.code == "invalid_refresh_token"

    await provider.revoke(
        access_token=refreshed.access_token,
        refresh_token=refreshed.refresh_token,
    )

    with pytest.raises(FakeWithingsError) as revoked_access:
        await provider.fetch_changes(
            access_token=refreshed.access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            cursor=None,
        )
    assert revoked_access.value.error.kind is HealthSyncErrorKind.AUTHENTICATION
    assert revoked_access.value.error.code == "invalid_access_token"


async def test_measurement_fetch_replays_paginated_results_deterministically() -> None:
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)

    first_page = await provider.fetch_changes(
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=None,
    )

    assert len(first_page.records) == 1
    assert first_page.records[0].external_id == "grpid:9001001"
    assert first_page.records[0].provider_revision == "1784509320"
    assert first_page.records[0].source_metadata["measures"][0]["value"] == 70540
    assert first_page.has_more is True
    assert first_page.next_cursor == HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime.fromtimestamp(1784505600, tz=UTC),
        page_offset=100,
    )

    second_page = await provider.fetch_changes(
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=first_page.next_cursor,
    )

    assert len(second_page.records) == 1
    assert second_page.records[0].external_id == "grpid:9001002"
    assert second_page.records[0].provider_revision == "1784510220"
    assert second_page.has_more is False
    assert second_page.next_cursor == HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime.fromtimestamp(1784510220, tz=UTC),
    )

    replayed_first_page = await provider.fetch_changes(
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=None,
    )

    assert replayed_first_page == first_page


async def test_measurement_fetch_replays_revision_then_tombstone() -> None:
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)
    revision_cursor = HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime.fromtimestamp(1784510220, tz=UTC),
    )

    revised = await provider.fetch_changes(
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=revision_cursor,
    )

    assert len(revised.records) == 1
    assert revised.records[0].external_id == "grpid:9001002"
    assert revised.records[0].provider_revision == "1784513040"
    assert revised.records[0].source_metadata["measures"][0]["value"] == 70420
    assert revised.next_cursor == HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime.fromtimestamp(1784513040, tz=UTC),
    )

    tombstones = await provider.fetch_changes(
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=revised.next_cursor,
    )

    assert tombstones.records == ()
    assert len(tombstones.tombstones) == 1
    assert tombstones.tombstones[0].external_id == "grpid:9001002"
    assert tombstones.tombstones[0].provider_revision == "synthetic-delete-rev-1"
    assert tombstones.tombstones[0].deleted_at == datetime(2026, 7, 20, 6, 45, tzinfo=UTC)
    assert tombstones.next_cursor == HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime(2026, 7, 20, 6, 45, tzinfo=UTC),
    )

    empty = await provider.fetch_changes(
        access_token=access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=tombstones.next_cursor,
    )

    assert empty.records == ()
    assert empty.tombstones == ()
    assert empty.next_cursor == tombstones.next_cursor


async def test_sleep_fetch_replays_summary_and_detail_records() -> None:
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)

    result = await provider.fetch_changes(
        access_token=access_token,
        resource_type=HealthResourceType.SLEEP,
        cursor=None,
    )

    assert len(result.records) == 2
    summary_record, detail_record = result.records
    assert summary_record.external_id == "sleep_summary:9203001"
    assert summary_record.source_metadata["data"]["sleep_score"] == 83
    assert detail_record.external_id.startswith("sleep:fallback:")
    assert detail_record.source_metadata["metrics"]["hr"]["1784473200"] == 58
    assert result.next_cursor == HealthSyncCursor(
        resource_type=HealthResourceType.SLEEP,
        last_modified=datetime.fromtimestamp(1784498400, tz=UTC),
    )


async def test_rate_limit_override_surfaces_retry_after() -> None:
    provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "rate_limit_retry_after"}
    )
    access_token = await _rotated_access_token(provider)

    with pytest.raises(FakeWithingsError) as exc:
        await provider.fetch_changes(
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            cursor=None,
        )

    assert exc.value.error.kind is HealthSyncErrorKind.RATE_LIMIT
    assert exc.value.retryable is True
    assert exc.value.error.code == "http_429"
    assert exc.value.error.retry_after_seconds == 120
    assert exc.value.error.provider_status_code == 601


async def test_transient_and_timeout_overrides_surface_retryable_errors() -> None:
    transient_provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.WORKOUT: "transient_service_unavailable"}
    )
    transient_access_token = await _rotated_access_token(transient_provider)

    with pytest.raises(FakeWithingsError) as transient_exc:
        await transient_provider.fetch_changes(
            access_token=transient_access_token,
            resource_type=HealthResourceType.WORKOUT,
            cursor=None,
        )

    assert transient_exc.value.error.kind is HealthSyncErrorKind.TRANSIENT
    assert transient_exc.value.retryable is True
    assert transient_exc.value.error.code == "http_503"
    assert transient_exc.value.error.provider_status_code == 503

    timeout_provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.SLEEP: "request_timeout"}
    )
    timeout_access_token = await _rotated_access_token(timeout_provider)

    with pytest.raises(FakeWithingsError) as timeout_exc:
        await timeout_provider.fetch_changes(
            access_token=timeout_access_token,
            resource_type=HealthResourceType.SLEEP,
            cursor=None,
        )

    assert timeout_exc.value.error.kind is HealthSyncErrorKind.TRANSIENT
    assert timeout_exc.value.retryable is True
    assert timeout_exc.value.error.code == "timeout"
    assert timeout_exc.value.error.provider_status_code == 522


async def test_malformed_json_override_stays_sanitized() -> None:
    provider = FakeWithingsProvider(
        fetch_scenarios={HealthResourceType.MEASUREMENT: "malformed_measurements_body"}
    )
    access_token = await _rotated_access_token(provider)

    with pytest.raises(FakeWithingsError) as exc:
        await provider.fetch_changes(
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            cursor=None,
        )

    assert exc.value.error.kind is HealthSyncErrorKind.MALFORMED_RESPONSE
    assert exc.value.error.code == "invalid_json"
    assert "9001999" not in exc.value.error.detail
    assert "measuregrps" not in exc.value.error.detail


async def test_measurement_cursor_requires_fixture_pagination_offset() -> None:
    provider = FakeWithingsProvider()
    access_token = await _rotated_access_token(provider)

    with pytest.raises(FakeWithingsError) as exc:
        await provider.fetch_changes(
            access_token=access_token,
            resource_type=HealthResourceType.MEASUREMENT,
            cursor=HealthSyncCursor(
                resource_type=HealthResourceType.MEASUREMENT,
                last_modified=datetime.fromtimestamp(1784505600, tz=UTC),
                page_offset=42,
            ),
        )

    assert exc.value.error.kind is HealthSyncErrorKind.INVALID_CURSOR_STATE
    assert exc.value.error.code == "invalid_cursor"
