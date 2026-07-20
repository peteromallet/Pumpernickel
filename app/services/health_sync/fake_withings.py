"""Deterministic offline Withings provider backed by frozen fixtures."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Mapping

from app.services.health_sync.models import (
    HealthFetchResult,
    HealthOAuthTokens,
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    HealthSyncCursor,
    HealthSyncError,
    HealthSyncErrorKind,
    HealthTombstone,
    WITHINGS_PROVIDER_CAPABILITIES,
    resolve_external_id,
)


_DEFAULT_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "withings"
_DEFAULT_TOKEN_ISSUED_AT = datetime(2026, 7, 20, 0, 0, tzinfo=UTC)


def _as_datetime(value: int | float | str) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _require_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a mapping")
    return value


def _require_sequence(value: Any, *, context: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{context} must be a list")
    return tuple(value)


def _require_text(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{context} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class _FixtureScenario:
    scenario_id: str
    payload: Mapping[str, Any]


class FakeWithingsError(RuntimeError):
    """Raised when fixture replay simulates a provider-side failure."""

    def __init__(self, error: HealthSyncError):
        super().__init__(error.detail or error.code)
        self.error = error

    @property
    def retryable(self) -> bool:
        return self.error.retryable


class FakeWithingsProvider:
    """Replay the frozen Withings fixtures without any network access."""

    name = "withings"
    capabilities = WITHINGS_PROVIDER_CAPABILITIES

    def __init__(
        self,
        *,
        fixture_dir: Path | str | None = None,
        fetch_scenarios: Mapping[HealthResourceType | str, str] | None = None,
        token_issued_at: datetime = _DEFAULT_TOKEN_ISSUED_AT,
    ) -> None:
        self._fixture_dir = Path(fixture_dir) if fixture_dir is not None else _DEFAULT_FIXTURE_DIR
        self._scenarios = self._load_scenarios()
        self._fetch_scenarios = {
            HealthResourceType(resource_type): scenario_id
            for resource_type, scenario_id in (fetch_scenarios or {}).items()
        }
        self._token_issued_at = token_issued_at.astimezone(UTC)
        self._token_issue_count = 0
        self._revoked_access_tokens: set[str] = set()
        self._revoked_refresh_tokens: set[str] = set()

        exchange_fixture = self._scenario("oauth_token_exchange_success").payload
        refresh_fixture = self._scenario("oauth_token_refresh_rotated").payload
        exchange_body = self._oauth_body(exchange_fixture, scenario_id="oauth_token_exchange_success")
        refresh_body = self._oauth_body(refresh_fixture, scenario_id="oauth_token_refresh_rotated")

        self._authorization_code = _require_text(
            _require_mapping(exchange_fixture["request"], context="oauth exchange request")["form"]["code"],
            context="oauth exchange code",
        )
        self._redirect_uri = _require_text(
            _require_mapping(exchange_fixture["request"], context="oauth exchange request")["form"][
                "redirect_uri"
            ],
            context="oauth redirect uri",
        )
        self._current_refresh_token = _require_text(
            refresh_body["refresh_token"],
            context="oauth refresh token",
        )
        self._accepted_refresh_tokens: set[str] = {
            _require_text(
                _require_mapping(refresh_fixture["request"], context="oauth refresh request")["form"][
                    "refresh_token"
                ],
                context="oauth refresh request token",
            )
        }
        self._active_access_tokens: set[str] = {
            _require_text(exchange_body["access_token"], context="oauth access token")
        }

        measurement_page_1 = self._scenario("measurements_page_1").payload
        measurement_page_2 = self._scenario("measurements_page_2").payload
        measurement_revision = self._scenario("measurements_revision").payload
        measurement_tombstones = self._scenario("measurements_tombstones").payload
        self._measurement_initial_lastupdate = _as_datetime(
            _require_mapping(measurement_page_1["request"], context="measurement page 1 request")["form"][
                "lastupdate"
            ]
        )
        self._measurement_page_2_modified = self._measurement_result(
            measurement_page_2, scenario_id="measurements_page_2", cursor=HealthSyncCursor(
                resource_type=HealthResourceType.MEASUREMENT,
                last_modified=self._measurement_initial_lastupdate,
                page_offset=100,
            )
        ).next_cursor
        if self._measurement_page_2_modified is None or self._measurement_page_2_modified.last_modified is None:
            raise RuntimeError("measurement page 2 fixture must advance the cursor")
        self._measurement_revision_modified = self._measurement_result(
            measurement_revision,
            scenario_id="measurements_revision",
            cursor=self._measurement_page_2_modified,
        ).next_cursor
        if self._measurement_revision_modified is None or self._measurement_revision_modified.last_modified is None:
            raise RuntimeError("measurement revision fixture must advance the cursor")
        tombstone_payload = _require_mapping(measurement_tombstones, context="measurement tombstones fixture")
        tombstone_entries = _require_sequence(tombstone_payload["tombstones"], context="measurement tombstones")
        self._measurement_tombstone_deleted_at = datetime.fromisoformat(
            _require_text(
                _require_mapping(tombstone_entries[0], context="measurement tombstone entry")["deleted_at"],
                context="measurement tombstone deleted_at",
            ).replace("Z", "+00:00")
        ).astimezone(UTC)

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> HealthOAuthTokens:
        if code != self._authorization_code:
            raise self._auth_error(code="invalid_authorization_code", detail="authorization code rejected")
        if redirect_uri != self._redirect_uri:
            raise self._auth_error(code="invalid_redirect_uri", detail="redirect uri rejected")
        body = self._oauth_body(
            self._scenario("oauth_token_exchange_success").payload,
            scenario_id="oauth_token_exchange_success",
        )
        return self._oauth_tokens_from_body(body)

    async def refresh_token(
        self,
        *,
        refresh_token: str,
    ) -> HealthOAuthTokens:
        if refresh_token in self._revoked_refresh_tokens or refresh_token not in self._accepted_refresh_tokens:
            raise self._auth_error(code="invalid_refresh_token", detail="refresh token rejected")
        body = self._oauth_body(
            self._scenario("oauth_token_refresh_rotated").payload,
            scenario_id="oauth_token_refresh_rotated",
        )
        rotated_refresh_token = _require_text(body["refresh_token"], context="rotated refresh token")
        rotated_access_token = _require_text(body["access_token"], context="rotated access token")
        self._accepted_refresh_tokens.remove(refresh_token)
        self._revoked_refresh_tokens.add(refresh_token)
        self._accepted_refresh_tokens.add(rotated_refresh_token)
        self._current_refresh_token = rotated_refresh_token
        self._active_access_tokens.add(rotated_access_token)
        return self._oauth_tokens_from_body(body)

    async def fetch_changes(
        self,
        *,
        access_token: str,
        resource_type: HealthResourceType,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        self._validate_access_token(access_token)
        normalized_resource = HealthResourceType(resource_type)
        normalized_cursor = self._normalize_cursor(resource_type=normalized_resource, cursor=cursor)

        override = self._fetch_scenarios.get(normalized_resource)
        if override is not None:
            return self._fetch_override(
                resource_type=normalized_resource,
                cursor=normalized_cursor,
                scenario_id=override,
            )

        if normalized_resource is HealthResourceType.MEASUREMENT:
            return self._fetch_measurements(normalized_cursor)
        if normalized_resource is HealthResourceType.WORKOUT:
            return self._workout_result(
                self._scenario("workouts_page_1").payload,
                scenario_id="workouts_page_1",
            )
        if normalized_resource is HealthResourceType.SLEEP:
            return self._sleep_result(
                summary_fixture=self._scenario("sleep_summary_page_1").payload,
                detail_fixture=self._scenario("sleep_detail_page_1").payload,
                scenario_id="sleep_summary_page_1+sleep_detail_page_1",
            )
        raise AssertionError(f"unsupported resource type {normalized_resource.value}")

    async def revoke(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
    ) -> None:
        self._revoked_access_tokens.add(access_token)
        if refresh_token is not None:
            self._revoked_refresh_tokens.add(refresh_token)
            self._accepted_refresh_tokens.discard(refresh_token)

    def _load_scenarios(self) -> dict[str, _FixtureScenario]:
        catalog_path = self._fixture_dir / "catalog.json"
        catalog = json.loads(catalog_path.read_text())
        scenarios = _require_sequence(catalog["scenarios"], context="fixture catalog scenarios")
        loaded: dict[str, _FixtureScenario] = {}
        for raw_scenario in scenarios:
            scenario = _require_mapping(raw_scenario, context="fixture catalog scenario")
            scenario_id = _require_text(scenario["id"], context="fixture scenario id")
            payload = json.loads((self._fixture_dir / _require_text(scenario["file"], context="fixture file")).read_text())
            loaded[scenario_id] = _FixtureScenario(scenario_id=scenario_id, payload=payload)
        loaded["measurements_tombstones"] = _FixtureScenario(
            scenario_id="measurements_tombstones",
            payload=json.loads((self._fixture_dir / "measurements_tombstones.json").read_text()),
        )
        return loaded

    def _scenario(self, scenario_id: str) -> _FixtureScenario:
        try:
            return self._scenarios[scenario_id]
        except KeyError as exc:
            raise KeyError(f"unknown fake Withings scenario {scenario_id!r}") from exc

    def _oauth_body(
        self,
        fixture: Mapping[str, Any],
        *,
        scenario_id: str,
    ) -> Mapping[str, Any]:
        response = _require_mapping(fixture["response"], context=f"{scenario_id} response")
        body = _require_mapping(response["json"], context=f"{scenario_id} response json")["body"]
        return _require_mapping(body, context=f"{scenario_id} response body")

    def _oauth_tokens_from_body(self, body: Mapping[str, Any]) -> HealthOAuthTokens:
        issued_at = self._token_issued_at + timedelta(seconds=self._token_issue_count)
        self._token_issue_count += 1
        expires_in = int(body["expires_in"])
        scope_text = _require_text(body["scope"], context="oauth scope")
        return HealthOAuthTokens(
            access_token=_require_text(body["access_token"], context="oauth access token"),
            refresh_token=_require_text(body["refresh_token"], context="oauth refresh token"),
            expires_at=issued_at + timedelta(seconds=expires_in),
            external_user_id=str(int(body["userid"])),
            granted_scopes=frozenset(scope.strip() for scope in scope_text.split(",") if scope.strip()),
        )

    def _normalize_cursor(
        self,
        *,
        resource_type: HealthResourceType,
        cursor: HealthSyncCursor | None,
    ) -> HealthSyncCursor | None:
        if cursor is None:
            return None
        if cursor.resource_type is not resource_type:
            raise FakeWithingsError(
                HealthSyncError.permanent_error(
                    kind=HealthSyncErrorKind.INVALID_CURSOR_STATE,
                    code="cursor_resource_mismatch",
                    detail="cursor resource type does not match the requested resource",
                )
            )
        return cursor

    def _validate_access_token(self, access_token: str) -> None:
        if access_token in self._revoked_access_tokens or access_token not in self._active_access_tokens:
            raise self._auth_error(code="invalid_access_token", detail="access token rejected")

    def _fetch_override(
        self,
        *,
        resource_type: HealthResourceType,
        cursor: HealthSyncCursor | None,
        scenario_id: str,
    ) -> HealthFetchResult:
        fixture = self._scenario(scenario_id).payload
        if scenario_id == "rate_limit_retry_after":
            response = _require_mapping(fixture["response"], context=f"{scenario_id} response")
            headers = _require_mapping(response["headers"], context=f"{scenario_id} headers")
            body = _require_mapping(response["json"], context=f"{scenario_id} response json")
            raise FakeWithingsError(
                HealthSyncError.retryable_error(
                    kind=HealthSyncErrorKind.RATE_LIMIT,
                    code="http_429",
                    detail="fixture replay hit a rate limit",
                    retry_after_seconds=int(headers["Retry-After"]),
                    provider_status_code=int(body["status"]),
                )
            )
        if scenario_id == "transient_service_unavailable":
            response = _require_mapping(fixture["response"], context=f"{scenario_id} response")
            body = _require_mapping(response["json"], context=f"{scenario_id} response json")
            raise FakeWithingsError(
                HealthSyncError.retryable_error(
                    code="http_503",
                    detail="fixture replay hit a transient upstream failure",
                    provider_status_code=int(body["status"]),
                )
            )
        if scenario_id == "request_timeout":
            transport = _require_mapping(fixture["transport_error"], context=f"{scenario_id} transport_error")
            raise FakeWithingsError(
                HealthSyncError.retryable_error(
                    code="timeout",
                    detail="fixture replay timed out waiting for Withings",
                    provider_status_code=522,
                )
            )
        if scenario_id == "malformed_measurements_body":
            response = _require_mapping(fixture["response"], context=f"{scenario_id} response")
            try:
                json.loads(_require_text(response["body_text"], context=f"{scenario_id} body text"))
            except json.JSONDecodeError as exc:
                raise FakeWithingsError(
                    HealthSyncError.permanent_error(
                        kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                        code="invalid_json",
                        detail="fixture replay returned malformed JSON",
                    )
                ) from exc
            raise AssertionError("malformed fixture unexpectedly decoded successfully")
        if scenario_id == "measurements_tombstones":
            return self._tombstone_result(fixture, scenario_id=scenario_id, cursor=cursor)

        if resource_type is HealthResourceType.MEASUREMENT:
            return self._measurement_result(fixture, scenario_id=scenario_id, cursor=cursor)
        if resource_type is HealthResourceType.WORKOUT:
            return self._workout_result(fixture, scenario_id=scenario_id)
        if resource_type is HealthResourceType.SLEEP:
            return self._sleep_result(
                summary_fixture=fixture,
                detail_fixture=self._scenario("sleep_detail_page_1").payload,
                scenario_id=scenario_id,
            )
        raise AssertionError(f"unsupported override resource type {resource_type.value}")

    def _fetch_measurements(self, cursor: HealthSyncCursor | None) -> HealthFetchResult:
        if cursor is not None and cursor.page_offset is not None:
            if cursor.page_offset != 100:
                raise self._invalid_cursor_error("measurement page_offset must match the fixture pagination offset")
            return self._measurement_result(
                self._scenario("measurements_page_2").payload,
                scenario_id="measurements_page_2",
                cursor=cursor,
            )

        if cursor is None or cursor.last_modified is None or cursor.last_modified <= self._measurement_initial_lastupdate:
            return self._measurement_result(
                self._scenario("measurements_page_1").payload,
                scenario_id="measurements_page_1",
                cursor=cursor,
            )

        if cursor.last_modified < self._measurement_page_2_modified.last_modified:
            raise self._invalid_cursor_error("measurement cursor dropped the pagination offset before the final page")

        if cursor.last_modified < self._measurement_revision_modified.last_modified:
            return self._measurement_result(
                self._scenario("measurements_revision").payload,
                scenario_id="measurements_revision",
                cursor=cursor,
            )

        if cursor.last_modified < self._measurement_tombstone_deleted_at:
            return self._tombstone_result(
                self._scenario("measurements_tombstones").payload,
                scenario_id="measurements_tombstones",
                cursor=cursor,
            )

        return HealthFetchResult(
            resource_type=HealthResourceType.MEASUREMENT,
            records=(),
            tombstones=(),
            next_cursor=cursor,
            has_more=False,
        )

    def _measurement_result(
        self,
        fixture: Mapping[str, Any],
        *,
        scenario_id: str,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        body = self._response_body(fixture, scenario_id=scenario_id)
        groups = _require_sequence(body["measuregrps"], context=f"{scenario_id} measuregrps")
        timezone = _require_text(body["timezone"], context=f"{scenario_id} timezone")
        observed_at = _as_datetime(body["updatetime"])
        records = tuple(
            self._measurement_record(group, timezone=timezone, observed_at=observed_at, scenario_id=scenario_id)
            for group in groups
        )
        has_more = bool(body["more"])
        if has_more:
            next_cursor = HealthSyncCursor(
                resource_type=HealthResourceType.MEASUREMENT,
                last_modified=cursor.last_modified if cursor is not None else self._measurement_initial_lastupdate,
                page_offset=int(body["offset"]),
            )
        else:
            latest_modified = max(
                record.source_modified_at for record in records if record.source_modified_at is not None
            )
            next_cursor = HealthSyncCursor(
                resource_type=HealthResourceType.MEASUREMENT,
                last_modified=latest_modified,
            )
        return HealthFetchResult(
            resource_type=HealthResourceType.MEASUREMENT,
            records=records,
            tombstones=(),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def _measurement_record(
        self,
        group: Any,
        *,
        timezone: str,
        observed_at: datetime,
        scenario_id: str,
    ) -> HealthSourceRecord:
        payload = _require_mapping(group, context=f"{scenario_id} measuregrp")
        measures = tuple(_require_mapping(item, context=f"{scenario_id} measure") for item in _require_sequence(payload["measures"], context=f"{scenario_id} measures"))
        modified_at = _as_datetime(payload["modified"])
        return HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.MEASUREMENT,
            external_id=resolve_external_id(
                HealthResourceType.MEASUREMENT,
                external_id=f"grpid:{int(payload['grpid'])}",
            ),
            source_created_at=_as_datetime(payload["created"]),
            source_modified_at=modified_at,
            observed_at=observed_at,
            source_timezone=timezone,
            source_device_id=str(payload.get("deviceid") or payload.get("hash_deviceid") or ""),
            source_device_model=str(payload.get("model") or ""),
            provider_revision=str(int(payload["modified"])),
            source_metadata={
                "attrib": int(payload["attrib"]),
                "category": int(payload["category"]),
                "comment": str(payload.get("comment") or ""),
                "date": int(payload["date"]),
                "hash_deviceid": payload.get("hash_deviceid"),
                "measures": [dict(item) for item in measures],
                "model_id": int(payload["model_id"]),
            },
            attribution={"fixture_scenario": scenario_id},
        )

    def _workout_result(
        self,
        fixture: Mapping[str, Any],
        *,
        scenario_id: str,
    ) -> HealthFetchResult:
        body = self._response_body(fixture, scenario_id=scenario_id)
        series = _require_sequence(body["series"], context=f"{scenario_id} series")
        records = tuple(self._workout_record(item, scenario_id=scenario_id) for item in series)
        latest_modified = max(
            record.source_modified_at for record in records if record.source_modified_at is not None
        )
        return HealthFetchResult(
            resource_type=HealthResourceType.WORKOUT,
            records=records,
            tombstones=(),
            next_cursor=HealthSyncCursor(
                resource_type=HealthResourceType.WORKOUT,
                last_modified=latest_modified,
            ),
            has_more=bool(body["more"]),
        )

    def _workout_record(self, series_entry: Any, *, scenario_id: str) -> HealthSourceRecord:
        payload = _require_mapping(series_entry, context=f"{scenario_id} workout series")
        data = _require_mapping(payload["data"], context=f"{scenario_id} workout data")
        modified_at = _as_datetime(payload["modified"])
        return HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.WORKOUT,
            external_id=resolve_external_id(
                HealthResourceType.WORKOUT,
                external_id=f"workout:{int(payload['id'])}",
            ),
            source_modified_at=modified_at,
            starts_at=_as_datetime(payload["startdate"]),
            ends_at=_as_datetime(payload["enddate"]),
            source_timezone=_require_text(payload["timezone"], context=f"{scenario_id} timezone"),
            source_device_id=str(payload.get("deviceid") or ""),
            source_device_model=str(payload.get("model") or ""),
            provider_revision=str(int(payload["modified"])),
            source_metadata={
                "attrib": int(payload["attrib"]),
                "category": int(payload["category"]),
                "data": dict(data),
                "date": _require_text(payload["date"], context=f"{scenario_id} date"),
            },
            attribution={"fixture_scenario": scenario_id},
        )

    def _sleep_result(
        self,
        *,
        summary_fixture: Mapping[str, Any],
        detail_fixture: Mapping[str, Any],
        scenario_id: str,
    ) -> HealthFetchResult:
        summary_body = self._response_body(summary_fixture, scenario_id="sleep_summary_page_1")
        detail_body = self._response_body(detail_fixture, scenario_id="sleep_detail_page_1")
        summary_series = _require_sequence(summary_body["series"], context="sleep summary series")
        records = [self._sleep_summary_record(item, scenario_id=scenario_id) for item in summary_series]
        records.append(self._sleep_detail_record(detail_body["series"], scenario_id=scenario_id))
        latest_modified = max(
            record.source_modified_at for record in records if record.source_modified_at is not None
        )
        return HealthFetchResult(
            resource_type=HealthResourceType.SLEEP,
            records=tuple(records),
            tombstones=(),
            next_cursor=HealthSyncCursor(
                resource_type=HealthResourceType.SLEEP,
                last_modified=latest_modified,
            ),
            has_more=bool(summary_body["more"]),
        )

    def _sleep_summary_record(self, series_entry: Any, *, scenario_id: str) -> HealthSourceRecord:
        payload = _require_mapping(series_entry, context=f"{scenario_id} sleep summary")
        data = _require_mapping(payload["data"], context=f"{scenario_id} sleep summary data")
        modified_at = _as_datetime(payload["modified"])
        return HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.SLEEP,
            external_id=resolve_external_id(
                HealthResourceType.SLEEP,
                external_id=f"sleep_summary:{int(payload['id'])}",
            ),
            source_created_at=_as_datetime(payload["created"]),
            source_modified_at=modified_at,
            starts_at=_as_datetime(payload["startdate"]),
            ends_at=_as_datetime(payload["enddate"]),
            source_timezone=_require_text(payload["timezone"], context=f"{scenario_id} timezone"),
            source_device_id=str(payload.get("hash_deviceid") or ""),
            source_device_model=str(payload.get("model_id") or payload.get("model") or ""),
            provider_revision=str(int(payload["modified"])),
            source_metadata={
                "completed": bool(payload["completed"]),
                "data": dict(data),
                "date": _require_text(payload["date"], context=f"{scenario_id} date"),
                "model": int(payload["model"]),
                "model_id": int(payload["model_id"]),
            },
            attribution={"fixture_scenario": scenario_id, "sleep_payload": "summary"},
        )

    def _sleep_detail_record(self, series_entry: Any, *, scenario_id: str) -> HealthSourceRecord:
        payload = _require_mapping(series_entry, context=f"{scenario_id} sleep detail")
        metric_maps = {
            key: value
            for key, value in payload.items()
            if isinstance(value, Mapping) and key not in {"data"}
        }
        all_timestamps = [
            int(timestamp)
            for metric_map in metric_maps.values()
            for timestamp in metric_map.keys()
            if str(timestamp).strip()
        ]
        latest_timestamp = max(all_timestamps) if all_timestamps else int(payload["enddate"])
        external_id = resolve_external_id(
            HealthResourceType.SLEEP,
            fallback_components={
                "enddate": int(payload["enddate"]),
                "model": _require_text(payload["model"], context=f"{scenario_id} model"),
                "model_id": int(payload["model_id"]),
                "startdate": int(payload["startdate"]),
            },
        )
        return HealthSourceRecord(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.SLEEP,
            external_id=external_id,
            source_modified_at=_as_datetime(latest_timestamp),
            starts_at=_as_datetime(payload["startdate"]),
            ends_at=_as_datetime(payload["enddate"]),
            source_device_model=_require_text(payload["model"], context=f"{scenario_id} model"),
            provider_revision=str(latest_timestamp),
            source_metadata={
                "metrics": {key: dict(value) for key, value in metric_maps.items()},
                "model": int(payload["model_id"]),
                "state": int(payload["state"]),
            },
            attribution={"fixture_scenario": scenario_id, "sleep_payload": "detail"},
        )

    def _tombstone_result(
        self,
        fixture: Mapping[str, Any],
        *,
        scenario_id: str,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        payload = _require_mapping(fixture, context=f"{scenario_id} payload")
        tombstones = tuple(
            self._tombstone(item, scenario_id=scenario_id)
            for item in _require_sequence(payload["tombstones"], context=f"{scenario_id} tombstones")
        )
        latest_deleted_at = max(tombstone.deleted_at for tombstone in tombstones)
        next_cursor = HealthSyncCursor(
            resource_type=HealthResourceType.MEASUREMENT,
            last_modified=max(
                latest_deleted_at,
                cursor.last_modified if cursor is not None and cursor.last_modified is not None else latest_deleted_at,
            ),
        )
        return HealthFetchResult(
            resource_type=HealthResourceType.MEASUREMENT,
            records=(),
            tombstones=tombstones,
            next_cursor=next_cursor,
            has_more=False,
        )

    def _tombstone(self, value: Any, *, scenario_id: str) -> HealthTombstone:
        payload = _require_mapping(value, context=f"{scenario_id} tombstone")
        return HealthTombstone(
            provider=HealthProviderSlug.WITHINGS,
            resource_type=HealthResourceType.MEASUREMENT,
            external_id=_require_text(payload["external_id"], context=f"{scenario_id} external_id"),
            deleted_at=datetime.fromisoformat(
                _require_text(payload["deleted_at"], context=f"{scenario_id} deleted_at").replace("Z", "+00:00")
            ).astimezone(UTC),
            provider_revision=str(payload.get("provider_revision") or ""),
            reason=_require_text(payload["reason"], context=f"{scenario_id} reason"),
        )

    def _response_body(self, fixture: Mapping[str, Any], *, scenario_id: str) -> Mapping[str, Any]:
        response = _require_mapping(fixture["response"], context=f"{scenario_id} response")
        if int(response["status_code"]) != 200:
            raise RuntimeError(f"{scenario_id} is not a successful fixture")
        response_json = _require_mapping(response["json"], context=f"{scenario_id} response json")
        if int(response_json["status"]) != 0:
            raise RuntimeError(f"{scenario_id} response status must be zero")
        body = response_json["body"]
        return _require_mapping(body, context=f"{scenario_id} response body")

    def _invalid_cursor_error(self, detail: str) -> FakeWithingsError:
        return FakeWithingsError(
            HealthSyncError.permanent_error(
                kind=HealthSyncErrorKind.INVALID_CURSOR_STATE,
                code="invalid_cursor",
                detail=detail,
            )
        )

    def _auth_error(self, *, code: str, detail: str) -> FakeWithingsError:
        return FakeWithingsError(
            HealthSyncError.permanent_error(
                kind=HealthSyncErrorKind.AUTHENTICATION,
                code=code,
                detail=detail,
                provider_status_code=401,
            )
        )


__all__ = ["FakeWithingsError", "FakeWithingsProvider"]
