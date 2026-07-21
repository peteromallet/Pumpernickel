"""Withings provider adapter with injected transport and sanitized failures."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import json
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from app.services.health_sync.models import (
    HealthFetchResult,
    HealthOAuthTokens,
    HealthProviderSlug,
    HealthResourceType,
    HealthSourceRecord,
    HealthSyncCursor,
    HealthSyncError,
    HealthSyncErrorKind,
    WITHINGS_PROVIDER_CAPABILITIES,
    resolve_external_id,
)


DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_REQUEST_BODY_BYTES = 4_096
DEFAULT_MAX_RESPONSE_BODY_BYTES = 1_048_576
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0
DEFAULT_WITHINGS_API_BASE_URL = "https://wbsapi.withings.net"

_MEASUREMENT_TYPES = "1,6,8,76,88"
_WORKOUT_DATA_FIELDS = "calories,distance,steps,elevation,hr_average,hr_max,hr_min,pause_duration"
_SLEEP_SUMMARY_DATA_FIELDS = (
    "total_timeinbed,total_sleep_time,lightsleepduration,remsleepduration,"
    "deepsleepduration,wakeupcount,sleep_score"
)
_SLEEP_DETAIL_DATA_FIELDS = "hr,rr,snoring"
_DEFAULT_SLEEP_BACKFILL_WINDOW = timedelta(days=30)
_HTTP_TIMEOUT_STATUS = 522


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _as_datetime(value: int | float | str) -> datetime:
    return datetime.fromtimestamp(int(value), tz=UTC)


def _require_text(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TypeError(f"{context} must be a non-empty string")
    return value.strip()


def _require_mapping(value: Any, *, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{context} must be a mapping")
    return value


def _require_sequence(value: Any, *, context: str) -> tuple[Any, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{context} must be a list")
    return tuple(value)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_retry_after(headers: Mapping[str, str]) -> int | None:
    raw_value = headers.get("Retry-After") or headers.get("retry-after")
    if raw_value is None:
        return None
    return _optional_int(raw_value)


@dataclass(frozen=True, slots=True)
class WithingsTransportRequest:
    method: str
    path: str
    form: Mapping[str, str] = field(default_factory=dict)
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_response_body_bytes: int = DEFAULT_MAX_RESPONSE_BODY_BYTES


@dataclass(frozen=True, slots=True)
class WithingsTransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body_text: str


class WithingsTransport(Protocol):
    async def request(self, request: WithingsTransportRequest) -> WithingsTransportResponse: ...


class WithingsAdapterError(RuntimeError):
    """Provider-facing error wrapper with a normalized sync error payload."""

    def __init__(self, error: HealthSyncError):
        super().__init__(error.detail or error.code)
        self.error = error

    @property
    def retryable(self) -> bool:
        return self.error.retryable


class HttpxWithingsTransport:
    """Default transport for live HTTP calls; tests should inject a fixture transport."""

    def __init__(self, *, base_url: str = DEFAULT_WITHINGS_API_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")

    async def request(self, request: WithingsTransportRequest) -> WithingsTransportResponse:
        timeout = httpx.Timeout(
            request.timeout_seconds,
            connect=min(request.timeout_seconds, DEFAULT_CONNECT_TIMEOUT_SECONDS),
        )
        async with httpx.AsyncClient(base_url=self._base_url, timeout=timeout) as client:
            response = await client.request(
                method=request.method,
                url=request.path,
                data=dict(request.form),
                headers=dict(request.headers),
            )
        return WithingsTransportResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            body_text=response.text,
        )


class WithingsProvider:
    """Minimal Withings-shaped provider adapter."""

    name = "withings"
    capabilities = WITHINGS_PROVIDER_CAPABILITIES

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        transport: WithingsTransport | None = None,
        api_base_url: str = DEFAULT_WITHINGS_API_BASE_URL,
        request_timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_request_body_bytes: int = DEFAULT_MAX_REQUEST_BODY_BYTES,
        max_response_body_bytes: int = DEFAULT_MAX_RESPONSE_BODY_BYTES,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._client_id = _require_text(client_id, context="client_id")
        self._client_secret = _require_text(client_secret, context="client_secret")
        self._request_timeout_seconds = float(request_timeout_seconds)
        self._max_request_body_bytes = int(max_request_body_bytes)
        self._max_response_body_bytes = int(max_response_body_bytes)
        self._clock = clock
        if self._request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be positive")
        if self._max_request_body_bytes <= 0:
            raise ValueError("max_request_body_bytes must be positive")
        if self._max_response_body_bytes <= 0:
            raise ValueError("max_response_body_bytes must be positive")
        self._transport = transport if transport is not None else HttpxWithingsTransport(base_url=api_base_url)

    async def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
    ) -> HealthOAuthTokens:
        body = await self._post_form(
            operation="oauth_exchange",
            path="/v2/oauth2",
            form={
                "action": "requesttoken",
                "grant_type": "authorization_code",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": _require_text(code, context="authorization code"),
                "redirect_uri": _require_text(redirect_uri, context="redirect uri"),
            },
        )
        return self._oauth_tokens_from_body(body)

    async def refresh_token(
        self,
        *,
        refresh_token: str,
    ) -> HealthOAuthTokens:
        body = await self._post_form(
            operation="oauth_refresh",
            path="/v2/oauth2",
            form={
                "action": "requesttoken",
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": _require_text(refresh_token, context="refresh token"),
            },
        )
        return self._oauth_tokens_from_body(body)

    async def fetch_changes(
        self,
        *,
        access_token: str,
        resource_type: HealthResourceType,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        normalized_resource = HealthResourceType(resource_type)
        normalized_cursor = self._normalize_cursor(resource_type=normalized_resource, cursor=cursor)
        bearer_token = _require_text(access_token, context="access token")

        if normalized_resource is HealthResourceType.MEASUREMENT:
            return await self._fetch_measurements(
                access_token=bearer_token,
                cursor=normalized_cursor,
            )
        if normalized_resource is HealthResourceType.WORKOUT:
            return await self._fetch_workouts(
                access_token=bearer_token,
                cursor=normalized_cursor,
            )
        if normalized_resource is HealthResourceType.SLEEP:
            return await self._fetch_sleep(
                access_token=bearer_token,
                cursor=normalized_cursor,
            )
        raise AssertionError(f"unsupported resource type {normalized_resource.value}")

    async def revoke(
        self,
        *,
        access_token: str,
        refresh_token: str | None = None,
    ) -> None:
        _require_text(access_token, context="access token")
        if refresh_token is not None:
            _require_text(refresh_token, context="refresh token")
        # The current provider contract does not carry the external user id that
        # the official revoke endpoint requires, so disconnect remains a local
        # state transition until a later milestone extends the call site.
        return None

    async def _fetch_measurements(
        self,
        *,
        access_token: str,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        body = await self._post_form(
            operation="measurements",
            path="/measure",
            form=self._measurement_form(cursor),
            access_token=access_token,
        )
        try:
            records = tuple(
                self._measurement_record(
                    group,
                    timezone=_require_text(body["timezone"], context="measurement timezone"),
                    observed_at=_as_datetime(body["updatetime"]),
                )
                for group in _require_sequence(body["measuregrps"], context="measurement measuregrps")
            )
            return HealthFetchResult(
                resource_type=HealthResourceType.MEASUREMENT,
                records=records,
                tombstones=(),
                next_cursor=self._page_cursor(
                    resource_type=HealthResourceType.MEASUREMENT,
                    body=body,
                    cursor=cursor,
                    records=records,
                ),
                has_more=self._body_has_more(body),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="invalid_measurement_body",
                detail="measurements returned an invalid response body",
            ) from exc

    async def _fetch_workouts(
        self,
        *,
        access_token: str,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        body = await self._post_form(
            operation="workouts",
            path="/v2/measure",
            form=self._workout_form(cursor),
            access_token=access_token,
        )
        try:
            records = tuple(
                self._workout_record(entry)
                for entry in _require_sequence(body["series"], context="workout series")
            )
            return HealthFetchResult(
                resource_type=HealthResourceType.WORKOUT,
                records=records,
                tombstones=(),
                next_cursor=self._page_cursor(
                    resource_type=HealthResourceType.WORKOUT,
                    body=body,
                    cursor=cursor,
                    records=records,
                ),
                has_more=self._body_has_more(body),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="invalid_workout_body",
                detail="workouts returned an invalid response body",
            ) from exc

    async def _fetch_sleep(
        self,
        *,
        access_token: str,
        cursor: HealthSyncCursor | None,
    ) -> HealthFetchResult:
        summary_body = await self._post_form(
            operation="sleep_summary",
            path="/v2/sleep",
            form=self._sleep_summary_form(cursor),
            access_token=access_token,
        )
        try:
            summary_records = [
                self._sleep_summary_record(entry)
                for entry in _require_sequence(summary_body["series"], context="sleep summary series")
            ]
            detail_records = []
            for summary_record in summary_records:
                detail_body = await self._post_form(
                    operation="sleep_detail",
                    path="/v2/sleep",
                    form=self._sleep_detail_form(summary_record),
                    access_token=access_token,
                )
                detail_records.extend(self._sleep_detail_records(detail_body["series"]))

            records = tuple(summary_records + detail_records)
            return HealthFetchResult(
                resource_type=HealthResourceType.SLEEP,
                records=records,
                tombstones=(),
                next_cursor=self._page_cursor(
                    resource_type=HealthResourceType.SLEEP,
                    body=summary_body,
                    cursor=cursor,
                    records=records,
                ),
                has_more=self._body_has_more(summary_body),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="invalid_sleep_body",
                detail="sleep returned an invalid response body",
            ) from exc

    async def _post_form(
        self,
        *,
        operation: str,
        path: str,
        form: Mapping[str, Any],
        access_token: str | None = None,
    ) -> Mapping[str, Any]:
        request = self._build_request(path=path, form=form, access_token=access_token)
        try:
            response = await self._transport.request(request)
        except AssertionError:
            raise
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.TRANSIENT,
                code="timeout",
                detail=f"{operation.replace('_', ' ')} timed out waiting for Withings",
                provider_status_code=_HTTP_TIMEOUT_STATUS,
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.TRANSIENT,
                code="transport_error",
                detail=f"{operation.replace('_', ' ')} hit a transport error contacting Withings",
            ) from exc
        except Exception as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.TRANSIENT,
                code="transport_error",
                detail=f"{operation.replace('_', ' ')} hit a transport error contacting Withings",
            ) from exc

        if len(response.body_text.encode("utf-8")) > self._max_response_body_bytes:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.PERMANENT,
                code="response_too_large",
                detail=f"{operation.replace('_', ' ')} exceeded the response size limit",
            )

        payload: Mapping[str, Any] | None = None
        body_text = response.body_text.strip()
        if body_text:
            try:
                raw_payload = json.loads(body_text)
            except json.JSONDecodeError as exc:
                if response.status_code >= 400:
                    raise self._classify_error(
                        operation=operation,
                        response_status_code=response.status_code,
                        payload=None,
                        headers=response.headers,
                    ) from exc
                raise self._adapter_error(
                    kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                    code="invalid_json",
                    detail=f"{operation.replace('_', ' ')} returned malformed JSON",
                ) from exc
            try:
                payload = _require_mapping(raw_payload, context=f"{operation} response payload")
            except TypeError as exc:
                raise self._adapter_error(
                    kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                    code="invalid_response_shape",
                    detail=f"{operation.replace('_', ' ')} returned an invalid response payload",
                ) from exc

        if response.status_code >= 400:
            raise self._classify_error(
                operation=operation,
                response_status_code=response.status_code,
                payload=payload,
                headers=response.headers,
            )

        if payload is None:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="empty_response",
                detail=f"{operation.replace('_', ' ')} returned an empty response body",
            )

        provider_status_code = _optional_int(payload.get("status"))
        if provider_status_code is None:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="invalid_status",
                detail=f"{operation.replace('_', ' ')} returned an invalid status field",
            )
        if provider_status_code != 0:
            raise self._classify_error(
                operation=operation,
                response_status_code=response.status_code,
                payload=payload,
                headers=response.headers,
            )

        try:
            body = _require_mapping(payload.get("body"), context=f"{operation} response body")
        except TypeError as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="invalid_response_shape",
                detail=f"{operation.replace('_', ' ')} returned an invalid response body",
            ) from exc
        return body

    def _build_request(
        self,
        *,
        path: str,
        form: Mapping[str, Any],
        access_token: str | None,
    ) -> WithingsTransportRequest:
        normalized_form: dict[str, str] = {}
        for key, raw_value in form.items():
            normalized_key = _require_text(str(key), context="form key")
            if raw_value is None:
                continue
            if isinstance(raw_value, bool):
                value_text = "true" if raw_value else "false"
            else:
                value_text = str(raw_value).strip()
            if not value_text:
                continue
            normalized_form[normalized_key] = value_text

        encoded = urlencode(sorted(normalized_form.items()), doseq=False).encode("utf-8")
        if len(encoded) > self._max_request_body_bytes:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.PERMANENT,
                code="request_too_large",
                detail="Withings request exceeded the configured size limit",
            )

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if access_token is not None:
            headers["Authorization"] = f"Bearer {access_token}"
        return WithingsTransportRequest(
            method="POST",
            path=path,
            form=normalized_form,
            headers=headers,
            timeout_seconds=self._request_timeout_seconds,
            max_response_body_bytes=self._max_response_body_bytes,
        )

    def _oauth_tokens_from_body(self, body: Mapping[str, Any]) -> HealthOAuthTokens:
        try:
            issued_at = _normalize_datetime(self._clock())
            expires_in = int(body["expires_in"])
            scope_text = _require_text(body["scope"], context="oauth scope")
            return HealthOAuthTokens(
                access_token=_require_text(body["access_token"], context="oauth access token"),
                refresh_token=_require_text(body["refresh_token"], context="oauth refresh token"),
                expires_at=issued_at + timedelta(seconds=expires_in),
                external_user_id=str(int(body["userid"])),
                granted_scopes=frozenset(scope.strip() for scope in scope_text.split(",") if scope.strip()),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="invalid_oauth_body",
                detail="oauth exchange returned an invalid token payload",
            ) from exc

    def _normalize_cursor(
        self,
        *,
        resource_type: HealthResourceType,
        cursor: HealthSyncCursor | None,
    ) -> HealthSyncCursor | None:
        if cursor is None:
            return None
        if cursor.resource_type is not resource_type:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.INVALID_CURSOR_STATE,
                code="cursor_resource_mismatch",
                detail="cursor resource type does not match the requested resource",
            )
        return cursor

    def _measurement_form(self, cursor: HealthSyncCursor | None) -> dict[str, Any]:
        form: dict[str, Any] = {
            "action": "getmeas",
            "meastypes": _MEASUREMENT_TYPES,
        }
        self._apply_cursor_fields(form, cursor)
        return form

    def _workout_form(self, cursor: HealthSyncCursor | None) -> dict[str, Any]:
        form: dict[str, Any] = {
            "action": "getworkouts",
            "data_fields": _WORKOUT_DATA_FIELDS,
        }
        self._apply_cursor_fields(form, cursor)
        return form

    def _sleep_summary_form(self, cursor: HealthSyncCursor | None) -> dict[str, Any]:
        form: dict[str, Any] = {
            "action": "getsummary",
            "data_fields": _SLEEP_SUMMARY_DATA_FIELDS,
        }
        # Unlike measurements and workouts, Withings requires sleep summary
        # requests to include either ``lastupdate`` or a YMD date range.  Dirty
        # category syncs can legitimately reach the adapter before a cursor has
        # been persisted, so give that first request the same bounded window as
        # reconciliation instead of sending an invalid selector-less request.
        if cursor is None:
            form["lastupdate"] = int(
                (self._clock() - _DEFAULT_SLEEP_BACKFILL_WINDOW).timestamp()
            )
            return form
        self._apply_cursor_fields(form, cursor)
        return form

    def _sleep_detail_form(self, summary_record: HealthSourceRecord) -> dict[str, Any]:
        if summary_record.starts_at is None or summary_record.ends_at is None:
            raise self._adapter_error(
                kind=HealthSyncErrorKind.MALFORMED_RESPONSE,
                code="missing_sleep_window",
                detail="sleep summary record is missing its start or end time",
            )
        return {
            "action": "get",
            "startdate": int(summary_record.starts_at.timestamp()),
            "enddate": int(summary_record.ends_at.timestamp()),
            "data_fields": _SLEEP_DETAIL_DATA_FIELDS,
        }

    def _apply_cursor_fields(self, form: dict[str, Any], cursor: HealthSyncCursor | None) -> None:
        if cursor is None:
            return
        if cursor.last_modified is not None:
            form["lastupdate"] = int(cursor.last_modified.timestamp())
        if cursor.page_offset is not None:
            form["offset"] = cursor.page_offset

    def _page_cursor(
        self,
        *,
        resource_type: HealthResourceType,
        body: Mapping[str, Any],
        cursor: HealthSyncCursor | None,
        records: tuple[HealthSourceRecord, ...],
    ) -> HealthSyncCursor:
        if self._body_has_more(body):
            return HealthSyncCursor(
                resource_type=resource_type,
                last_modified=cursor.last_modified if cursor is not None else None,
                page_offset=int(body["offset"]),
            )
        latest_modified = max(
            (
                record.source_modified_at
                for record in records
                if record.source_modified_at is not None
            ),
            default=cursor.last_modified if cursor is not None else None,
        )
        return HealthSyncCursor(
            resource_type=resource_type,
            last_modified=latest_modified,
        )

    def _body_has_more(self, body: Mapping[str, Any]) -> bool:
        return bool(body.get("more"))

    def _measurement_record(
        self,
        group: Any,
        *,
        timezone: str,
        observed_at: datetime,
    ) -> HealthSourceRecord:
        payload = _require_mapping(group, context="measurement group")
        measures = tuple(
            _require_mapping(item, context="measurement value")
            for item in _require_sequence(payload["measures"], context="measurement values")
        )
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
                # Withings' live API uses ``modelid`` while older fixtures and
                # some documented payloads use ``model_id``.
                "model_id": _optional_int(
                    payload.get("modelid", payload.get("model_id"))
                ),
            },
            attribution={"adapter": "withings"},
        )

    def _workout_record(self, series_entry: Any) -> HealthSourceRecord:
        payload = _require_mapping(series_entry, context="workout series")
        data = _require_mapping(payload["data"], context="workout data")
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
            source_timezone=_require_text(payload["timezone"], context="workout timezone"),
            source_device_id=str(payload.get("deviceid") or ""),
            source_device_model=str(payload.get("model") or ""),
            provider_revision=str(int(payload["modified"])),
            source_metadata={
                "attrib": int(payload["attrib"]),
                "category": int(payload["category"]),
                "data": dict(data),
                "date": _require_text(payload["date"], context="workout date"),
            },
            attribution={"adapter": "withings"},
        )

    def _sleep_summary_record(self, series_entry: Any) -> HealthSourceRecord:
        payload = _require_mapping(series_entry, context="sleep summary")
        data = _require_mapping(payload["data"], context="sleep summary data")
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
            source_timezone=_require_text(payload["timezone"], context="sleep summary timezone"),
            source_device_id=str(payload.get("hash_deviceid") or ""),
            source_device_model=str(payload.get("model_id") or payload.get("model") or ""),
            provider_revision=str(int(payload["modified"])),
            source_metadata={
                "completed": bool(payload["completed"]),
                "data": dict(data),
                "date": _require_text(payload["date"], context="sleep summary date"),
                "model": int(payload["model"]),
                "model_id": int(payload["model_id"]),
            },
            attribution={"adapter": "withings", "sleep_payload": "summary"},
        )

    def _sleep_detail_records(self, series: Any) -> tuple[HealthSourceRecord, ...]:
        """Normalize both legacy aggregate and live segmented detail shapes.

        Older fixtures and some Withings responses expose ``body.series`` as a
        single object.  The live API currently returns a list of sleep-state
        segments.  Each segment has its own stable start/end/state identity, so
        persist it as its own idempotent source record.
        """
        if isinstance(series, Mapping):
            return (self._sleep_detail_record(series),)
        if isinstance(series, list):
            return tuple(self._sleep_detail_record(entry) for entry in series)
        raise TypeError("sleep detail series must be an object or list")

    def _sleep_detail_record(self, series_entry: Any) -> HealthSourceRecord:
        payload = _require_mapping(series_entry, context="sleep detail")
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
                "model": _require_text(payload["model"], context="sleep detail model"),
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
            source_device_model=_require_text(payload["model"], context="sleep detail model"),
            provider_revision=str(latest_timestamp),
            source_metadata={
                "metrics": {key: dict(value) for key, value in metric_maps.items()},
                "model": int(payload["model_id"]),
                "state": int(payload["state"]),
            },
            attribution={"adapter": "withings", "sleep_payload": "detail"},
        )

    def _classify_error(
        self,
        *,
        operation: str,
        response_status_code: int,
        payload: Mapping[str, Any] | None,
        headers: Mapping[str, str],
    ) -> WithingsAdapterError:
        provider_status_code = _optional_int(payload.get("status")) if payload is not None else None
        retry_after_seconds = _parse_retry_after(headers)
        label = operation.replace("_", " ")

        if response_status_code == 429 or provider_status_code == 601:
            return self._adapter_error(
                kind=HealthSyncErrorKind.RATE_LIMIT,
                code="http_429" if response_status_code == 429 else "withings_status_601",
                detail=f"{label} hit the Withings rate limit",
                retry_after_seconds=retry_after_seconds,
                provider_status_code=provider_status_code,
            )
        if response_status_code in {401, 403} or provider_status_code == 401:
            return self._adapter_error(
                kind=HealthSyncErrorKind.AUTHENTICATION,
                code="authentication_failed",
                detail=f"{label} failed Withings authentication",
                provider_status_code=provider_status_code,
            )
        # Withings uses JSON status 503 for invalid parameters even when the
        # HTTP transport succeeded.  A real HTTP 5xx remains transient below.
        if provider_status_code == 503 and response_status_code < 500:
            return self._adapter_error(
                kind=HealthSyncErrorKind.PERMANENT,
                code="invalid_request",
                detail=f"{label} rejected invalid request parameters",
                provider_status_code=provider_status_code,
            )
        if response_status_code >= 500 or provider_status_code in {503, _HTTP_TIMEOUT_STATUS}:
            return self._adapter_error(
                kind=HealthSyncErrorKind.TRANSIENT,
                code=f"http_{response_status_code}" if response_status_code >= 500 else "withings_transient",
                detail=f"{label} hit a temporary Withings failure",
                provider_status_code=provider_status_code,
            )
        return self._adapter_error(
            kind=HealthSyncErrorKind.PERMANENT,
            code=(
                f"withings_status_{provider_status_code}"
                if provider_status_code is not None
                else f"http_{response_status_code}"
            ),
            detail=f"{label} failed with a non-retryable Withings error",
            provider_status_code=provider_status_code,
        )

    def _adapter_error(
        self,
        *,
        kind: HealthSyncErrorKind,
        code: str,
        detail: str,
        retry_after_seconds: int | None = None,
        provider_status_code: int | None = None,
    ) -> WithingsAdapterError:
        if kind in {HealthSyncErrorKind.RATE_LIMIT, HealthSyncErrorKind.TRANSIENT}:
            error = HealthSyncError.retryable_error(
                kind=kind,
                code=code,
                detail=detail,
                retry_after_seconds=retry_after_seconds,
                provider_status_code=provider_status_code,
            )
        else:
            error = HealthSyncError.permanent_error(
                kind=kind,
                code=code,
                detail=detail,
                provider_status_code=provider_status_code,
            )
        return WithingsAdapterError(error)


__all__ = [
    "DEFAULT_MAX_REQUEST_BODY_BYTES",
    "DEFAULT_MAX_RESPONSE_BODY_BYTES",
    "DEFAULT_TIMEOUT_SECONDS",
    "HttpxWithingsTransport",
    "WithingsAdapterError",
    "WithingsProvider",
    "WithingsTransport",
    "WithingsTransportRequest",
    "WithingsTransportResponse",
]
