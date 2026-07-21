from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import get_type_hints

from app.services.health_sync import (
    DEFAULT_CURSOR_OVERLAP,
    HealthFetchResult,
    HealthOAuthTokens,
    HealthResourceType,
    HealthSyncCursor,
    HealthSyncProvider,
    WITHINGS_PROVIDER_CAPABILITIES,
    build_fallback_external_id,
    resolve_external_id,
)


def test_provider_protocol_stays_minimal_and_withings_shaped() -> None:
    expected = {
        "exchange_code": (("code", "redirect_uri"), HealthOAuthTokens),
        "refresh_token": (("refresh_token",), HealthOAuthTokens),
        "fetch_changes": (("access_token", "resource_type", "cursor"), HealthFetchResult),
        "revoke": (("access_token", "refresh_token"), type(None)),
    }

    for method_name, (parameter_names, return_type) in expected.items():
        method = getattr(HealthSyncProvider, method_name)
        signature = inspect.signature(method)
        parameters = list(signature.parameters.values())
        assert [parameter.name for parameter in parameters] == ["self", *parameter_names]
        assert all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY for parameter in parameters[1:]
        )
        hints = get_type_hints(method)
        assert hints["return"] is return_type


def test_withings_capabilities_are_category_driven() -> None:
    categories = WITHINGS_PROVIDER_CAPABILITIES.categories

    assert WITHINGS_PROVIDER_CAPABILITIES.provider.value == "withings"
    assert tuple(category.resource_type for category in categories) == (
        HealthResourceType.MEASUREMENT,
        HealthResourceType.WORKOUT,
        HealthResourceType.SLEEP,
    )
    assert WITHINGS_PROVIDER_CAPABILITIES.category_for("measurement").required_scopes == frozenset(
        {"user.metrics"}
    )
    assert WITHINGS_PROVIDER_CAPABILITIES.category_for("workout").required_scopes == frozenset(
        {"user.activity"}
    )
    assert WITHINGS_PROVIDER_CAPABILITIES.category_for("sleep").required_scopes == frozenset(
        {"user.activity"}
    )


def test_cursor_state_round_trip_uses_expected_json_shape() -> None:
    cursor = HealthSyncCursor(
        resource_type=HealthResourceType.MEASUREMENT,
        last_modified=datetime(2026, 7, 20, 6, 5, tzinfo=UTC),
        page_offset=75,
        etag="etag-1",
    )

    assert cursor.overlap_window == DEFAULT_CURSOR_OVERLAP
    assert cursor.to_state() == {
        "resource_type": "measurement",
        "last_modified": "2026-07-20T06:05:00Z",
        "page_offset": 75,
        "etag": "etag-1",
    }
    assert HealthSyncCursor.from_state(cursor.to_state()) == cursor


def test_build_fallback_external_id_is_deterministic() -> None:
    first = build_fallback_external_id(
        HealthResourceType.SLEEP,
        {
            "end": datetime(2026, 7, 20, 7, 0, tzinfo=UTC),
            "start": datetime(2026, 7, 20, 6, 0, tzinfo=UTC),
            "timezone": "UTC",
            "device": {"model": "ScanWatch", "id": "sw-1"},
        },
    )
    second = build_fallback_external_id(
        "sleep",
        {
            "timezone": "UTC",
            "device": {"id": "sw-1", "model": "ScanWatch"},
            "start": datetime(2026, 7, 20, 6, 0, tzinfo=UTC),
            "end": datetime(2026, 7, 20, 7, 0, tzinfo=UTC),
        },
    )

    assert first == second
    assert first == (
        'sleep:fallback:{"device":{"id":"sw-1","model":"ScanWatch"},'
        '"end":"2026-07-20T07:00:00Z","start":"2026-07-20T06:00:00Z","timezone":"UTC"}'
    )


def test_resolve_external_id_prefers_native_identifier() -> None:
    resolved = resolve_external_id(
        HealthResourceType.WORKOUT,
        external_id=" workout-123 ",
        fallback_components={"start": 1, "end": 2},
    )

    assert resolved == "workout-123"
