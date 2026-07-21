from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json
from pathlib import Path
from typing import Any

import pytest

from app.services.health_sync import (
    FakeWithingsProvider,
    HealthFetchResult,
    HealthResourceType,
    HealthSourceRecord,
    HealthSyncCursor,
    HealthSyncErrorKind,
)
from app.services.health_sync.withings import (
    WithingsAdapterError,
    WithingsProvider,
    WithingsTransportRequest,
    WithingsTransportResponse,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "withings"
CALLBACK_URL = "https://example.test/api/health/devices/withings/oauth/callback"


def _load_fixture(scenario_id: str) -> dict[str, Any]:
    catalog = json.loads((FIXTURE_DIR / "catalog.json").read_text())
    for scenario in catalog["scenarios"]:
        if scenario["id"] == scenario_id:
            return json.loads((FIXTURE_DIR / scenario["file"]).read_text())
    raise KeyError(scenario_id)


def _record_snapshot(record: HealthSourceRecord) -> dict[str, Any]:
    return {
        "provider": record.provider.value,
        "resource_type": record.resource_type.value,
        "external_id": record.external_id,
        "source_created_at": record.source_created_at,
        "source_modified_at": record.source_modified_at,
        "observed_at": record.observed_at,
        "starts_at": record.starts_at,
        "ends_at": record.ends_at,
        "source_timezone": record.source_timezone,
        "source_offset_seconds": record.source_offset_seconds,
        "source_device_id": record.source_device_id,
        "source_device_model": record.source_device_model,
        "payload_hash": record.payload_hash,
        "provider_revision": record.provider_revision,
        "source_metadata": record.source_metadata,
        "is_deleted": record.is_deleted,
        "deleted_at": record.deleted_at,
    }


def _result_snapshot(result: HealthFetchResult) -> dict[str, Any]:
    return {
        "resource_type": result.resource_type.value,
        "records": [_record_snapshot(record) for record in result.records],
        "tombstones": [
            {
                "external_id": tombstone.external_id,
                "deleted_at": tombstone.deleted_at,
                "provider_revision": tombstone.provider_revision,
                "reason": tombstone.reason,
            }
            for tombstone in result.tombstones
        ],
        "next_cursor": result.next_cursor,
        "has_more": result.has_more,
    }


def _fixture_lastupdate(scenario_id: str) -> datetime:
    fixture = _load_fixture(scenario_id)
    return datetime.fromtimestamp(int(fixture["request"]["form"]["lastupdate"]), tz=UTC)


async def _rotated_access_token(provider: FakeWithingsProvider) -> str:
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")
    return refreshed.access_token


class FixtureTransport:
    def __init__(self, *scenario_ids: str) -> None:
        self._fixtures = [_load_fixture(scenario_id) for scenario_id in scenario_ids]
        self.requests: list[WithingsTransportRequest] = []

    async def request(self, request: WithingsTransportRequest) -> WithingsTransportResponse:
        self.requests.append(request)
        matches = [fixture for fixture in self._fixtures if self._matches(fixture, request)]
        if matches:
            fixture = max(matches, key=lambda candidate: len(candidate["request"].get("form", {})))
            if "transport_error" in fixture:
                raise TimeoutError("fixture timeout")
            response = fixture["response"]
            if "json" in response:
                body_text = json.dumps(response["json"], separators=(",", ":"))
            else:
                body_text = response["body_text"]
            return WithingsTransportResponse(
                status_code=int(response["status_code"]),
                headers=dict(response["headers"]),
                body_text=body_text,
            )
        raise AssertionError(f"unexpected request {request.path} {dict(request.form)}")

    def _matches(self, fixture: Mapping[str, Any], request: WithingsTransportRequest) -> bool:
        expected_request = fixture["request"]
        if expected_request["method"] != request.method:
            return False
        if expected_request["path"] != request.path:
            return False

        expected_form = expected_request.get("form", {})
        if any(str(request.form.get(key)) != str(value) for key, value in expected_form.items()):
            return False

        expected_headers = expected_request.get("headers", {})
        return all(request.headers.get(key) == value for key, value in expected_headers.items())


async def test_oauth_exchange_and_refresh_use_injected_transport_and_fixed_clock() -> None:
    transport = FixtureTransport("oauth_token_exchange_success", "oauth_token_refresh_rotated")
    provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=transport,
        request_timeout_seconds=7.5,
        clock=lambda: datetime(2026, 7, 20, 0, 0, tzinfo=UTC),
    )

    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    refreshed = await provider.refresh_token(refresh_token=exchanged.refresh_token or "")

    assert exchanged.access_token == "synthetic-access-token-v1"
    assert exchanged.refresh_token == "synthetic-refresh-token-v1"
    assert exchanged.external_user_id == "420001"
    assert exchanged.expires_at == datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    assert refreshed.access_token == "synthetic-access-token-v2"
    assert refreshed.refresh_token == "synthetic-refresh-token-v2"
    assert refreshed.expires_at == datetime(2026, 7, 20, 3, 0, tzinfo=UTC)
    assert [request.path for request in transport.requests] == ["/v2/oauth2", "/v2/oauth2"]
    assert all(request.timeout_seconds == 7.5 for request in transport.requests)


async def test_measurement_pagination_matches_fake_provider_without_live_network() -> None:
    transport = FixtureTransport("measurements_page_1", "measurements_page_2")
    provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=transport,
    )
    fake_provider = FakeWithingsProvider()
    fake_access_token = await _rotated_access_token(fake_provider)
    cursor = HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=_fixture_lastupdate("measurements_page_1"),
    )

    first_page = await provider.fetch_changes(
        access_token="synthetic-access-token-v2",
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=cursor,
    )
    second_page = await provider.fetch_changes(
        access_token="synthetic-access-token-v2",
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=first_page.next_cursor,
    )

    fake_first_page = await fake_provider.fetch_changes(
        access_token=fake_access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=cursor,
    )
    fake_second_page = await fake_provider.fetch_changes(
        access_token=fake_access_token,
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=fake_first_page.next_cursor,
    )

    assert _result_snapshot(first_page) == _result_snapshot(fake_first_page)
    assert _result_snapshot(second_page) == _result_snapshot(fake_second_page)
    assert transport.requests[0].headers["Authorization"] == "Bearer synthetic-access-token-v2"
    assert transport.requests[0].form["meastypes"] == "1,6,8,76,88"
    assert transport.requests[1].form["offset"] == "100"


async def test_measurement_accepts_live_withings_modelid_spelling() -> None:
    transport = FixtureTransport("measurements_page_1")
    body = transport._fixtures[0]["response"]["json"]["body"]
    for group in body["measuregrps"]:
        group["modelid"] = group.pop("model_id")
    provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=transport,
    )
    cursor = HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=_fixture_lastupdate("measurements_page_1"),
    )

    result = await provider.fetch_changes(
        access_token="synthetic-access-token-v2",
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=cursor,
    )

    assert result.records
    assert all(record.source_metadata["model_id"] == 18 for record in result.records)


async def test_measurement_accepts_null_live_withings_modelid() -> None:
    transport = FixtureTransport("measurements_page_1")
    body = transport._fixtures[0]["response"]["json"]["body"]
    for group in body["measuregrps"]:
        group.pop("model_id")
        group["modelid"] = None
    provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=transport,
    )

    result = await provider.fetch_changes(
        access_token="synthetic-access-token-v2",
        resource_type=HealthResourceType.MEASUREMENT,
        cursor=HealthSyncCursor(
            resource_type=HealthResourceType.MEASUREMENT,
            last_modified=_fixture_lastupdate("measurements_page_1"),
        ),
    )

    assert result.records
    assert all(record.source_metadata["model_id"] is None for record in result.records)


async def test_workout_and_sleep_normalization_match_fake_provider() -> None:
    transport = FixtureTransport("workouts_page_1", "sleep_summary_page_1", "sleep_detail_page_1")
    provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=transport,
    )
    fake_provider = FakeWithingsProvider()
    fake_access_token = await _rotated_access_token(fake_provider)

    workout_cursor = HealthSyncCursor(
        resource_type=HealthResourceType.WORKOUT,
        last_modified=_fixture_lastupdate("workouts_page_1"),
    )
    sleep_cursor = HealthSyncCursor(
        resource_type=HealthResourceType.SLEEP,
        last_modified=_fixture_lastupdate("sleep_summary_page_1"),
    )

    workout_result = await provider.fetch_changes(
        access_token="synthetic-access-token-v2",
        resource_type=HealthResourceType.WORKOUT,
        cursor=workout_cursor,
    )
    sleep_result = await provider.fetch_changes(
        access_token="synthetic-access-token-v2",
        resource_type=HealthResourceType.SLEEP,
        cursor=sleep_cursor,
    )

    fake_workout_result = await fake_provider.fetch_changes(
        access_token=fake_access_token,
        resource_type=HealthResourceType.WORKOUT,
        cursor=workout_cursor,
    )
    fake_sleep_result = await fake_provider.fetch_changes(
        access_token=fake_access_token,
        resource_type=HealthResourceType.SLEEP,
        cursor=sleep_cursor,
    )

    assert _result_snapshot(workout_result) == _result_snapshot(fake_workout_result)
    assert _result_snapshot(sleep_result) == _result_snapshot(fake_sleep_result)
    assert [request.form["action"] for request in transport.requests] == ["getworkouts", "getsummary", "get"]


async def test_rate_limit_and_timeout_failures_are_retryable_and_sanitized() -> None:
    rate_limit_provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=FixtureTransport("rate_limit_retry_after"),
    )
    timeout_provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=FixtureTransport("request_timeout"),
    )

    with pytest.raises(WithingsAdapterError) as rate_limit_exc:
        await rate_limit_provider.fetch_changes(
            access_token="synthetic-access-token-v2",
            resource_type=HealthResourceType.MEASUREMENT,
            cursor=HealthSyncCursor(
                resource_type=HealthResourceType.MEASUREMENT,
                last_modified=_fixture_lastupdate("rate_limit_retry_after"),
            ),
        )

    assert rate_limit_exc.value.error.kind is HealthSyncErrorKind.RATE_LIMIT
    assert rate_limit_exc.value.retryable is True
    assert rate_limit_exc.value.error.code == "http_429"
    assert rate_limit_exc.value.error.retry_after_seconds == 120
    assert rate_limit_exc.value.error.provider_status_code == 601
    assert "synthetic-access-token-v2" not in rate_limit_exc.value.error.detail

    with pytest.raises(WithingsAdapterError) as timeout_exc:
        await timeout_provider.fetch_changes(
            access_token="synthetic-access-token-v2",
            resource_type=HealthResourceType.SLEEP,
            cursor=HealthSyncCursor(
                resource_type=HealthResourceType.SLEEP,
                last_modified=_fixture_lastupdate("request_timeout"),
            ),
        )

    assert timeout_exc.value.error.kind is HealthSyncErrorKind.TRANSIENT
    assert timeout_exc.value.retryable is True
    assert timeout_exc.value.error.code == "timeout"
    assert timeout_exc.value.error.provider_status_code == 522
    assert "synthetic timeout while waiting" not in timeout_exc.value.error.detail


async def test_request_size_limit_rejects_before_transport() -> None:
    transport = FixtureTransport("oauth_token_exchange_success")
    provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=transport,
        max_request_body_bytes=32,
    )

    with pytest.raises(WithingsAdapterError) as exc:
        await provider.exchange_code(
            code="synthetic-auth-code-001",
            redirect_uri=CALLBACK_URL,
        )

    assert exc.value.error.kind is HealthSyncErrorKind.PERMANENT
    assert exc.value.error.code == "request_too_large"
    assert transport.requests == []


async def test_malformed_json_failure_stays_sanitized() -> None:
    provider = WithingsProvider(
        client_id="synthetic-client-id",
        client_secret="synthetic-client-secret",
        transport=FixtureTransport("malformed_measurements_body"),
    )

    with pytest.raises(WithingsAdapterError) as exc:
        await provider.fetch_changes(
            access_token="synthetic-access-token-v2",
            resource_type=HealthResourceType.MEASUREMENT,
            cursor=HealthSyncCursor(
                resource_type=HealthResourceType.MEASUREMENT,
                last_modified=_fixture_lastupdate("malformed_measurements_body"),
            ),
        )

    assert exc.value.error.kind is HealthSyncErrorKind.MALFORMED_RESPONSE
    assert exc.value.error.code == "invalid_json"
    assert "9001999" not in exc.value.error.detail
    assert "measuregrps" not in exc.value.error.detail
