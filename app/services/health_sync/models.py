"""Provider-facing health sync contract models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import Enum, IntEnum
import json
from typing import Any, Iterable, Mapping
from uuid import UUID


DEFAULT_CURSOR_OVERLAP = timedelta(hours=48)


class HealthProviderSlug(str, Enum):
    WITHINGS = "withings"


class HealthResourceType(str, Enum):
    MEASUREMENT = "measurement"
    WORKOUT = "workout"
    SLEEP = "sleep"


class HealthSyncStatus(str, Enum):
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class HealthSyncErrorKind(str, Enum):
    AUTHENTICATION = "authentication"
    RATE_LIMIT = "rate_limit"
    TRANSIENT = "transient"
    PERMANENT = "permanent"
    MALFORMED_RESPONSE = "malformed_response"
    INVALID_CURSOR_STATE = "invalid_cursor_state"


def _require_text(value: str, *, field_name: str) -> str:
    text = value.strip()
    if not text:
        raise ValueError(f"{field_name} must be a non-empty string")
    return text


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _datetime_to_wire(value: datetime) -> str:
    return _normalize_datetime(value).isoformat().replace("+00:00", "Z")


def _datetime_from_wire(value: str) -> datetime:
    candidate = value.strip()
    if not candidate:
        raise ValueError("datetime value must be non-empty")
    if candidate.endswith("Z"):
        candidate = f"{candidate[:-1]}+00:00"
    return _normalize_datetime(datetime.fromisoformat(candidate))


def _freeze_scopes(values: Iterable[str]) -> frozenset[str]:
    return frozenset(_require_text(value, field_name="required_scope") for value in values)


def _canonicalize_fallback_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return _datetime_to_wire(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {
            _require_text(str(key), field_name="fallback component key"): _canonicalize_fallback_value(raw)
            for key, raw in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonicalize_fallback_value(item) for item in value]
    return value


def build_fallback_external_id(
    resource_type: HealthResourceType | str,
    components: Mapping[str, Any],
) -> str:
    """Build a deterministic provider key from immutable source fields."""
    normalized_resource_type = HealthResourceType(resource_type)
    if not components:
        raise ValueError("fallback components are required when external_id is absent")
    canonical_components = _canonicalize_fallback_value(components)
    encoded = json.dumps(
        canonical_components,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"{normalized_resource_type.value}:fallback:{encoded}"


def resolve_external_id(
    resource_type: HealthResourceType | str,
    *,
    external_id: str | None = None,
    fallback_components: Mapping[str, Any] | None = None,
) -> str:
    if external_id is not None:
        return _require_text(external_id, field_name="external_id")
    if fallback_components is None:
        raise ValueError("fallback_components are required when external_id is absent")
    return build_fallback_external_id(resource_type, fallback_components)


@dataclass(frozen=True, slots=True)
class HealthProviderCategory:
    resource_type: HealthResourceType
    provider_category: str
    required_scopes: frozenset[str]
    supports_tombstones: bool = True
    supports_webhook_hints: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_category",
            _require_text(self.provider_category, field_name="provider_category"),
        )
        object.__setattr__(self, "required_scopes", _freeze_scopes(self.required_scopes))


@dataclass(frozen=True, slots=True)
class HealthProviderCapabilities:
    provider: HealthProviderSlug
    categories: tuple[HealthProviderCategory, ...]
    supports_token_refresh: bool = True
    supports_incremental_sync: bool = True
    supports_disconnect: bool = True

    def __post_init__(self) -> None:
        categories = tuple(self.categories)
        duplicates = len({category.resource_type for category in categories}) != len(categories)
        if duplicates:
            raise ValueError("provider categories must not repeat a resource_type")
        object.__setattr__(self, "categories", categories)

    def category_for(self, resource_type: HealthResourceType | str) -> HealthProviderCategory:
        normalized = HealthResourceType(resource_type)
        for category in self.categories:
            if category.resource_type == normalized:
                return category
        raise KeyError(normalized.value)


WITHINGS_PROVIDER_CAPABILITIES = HealthProviderCapabilities(
    provider=HealthProviderSlug.WITHINGS,
    categories=(
        HealthProviderCategory(
            resource_type=HealthResourceType.MEASUREMENT,
            provider_category="measurements",
            required_scopes=frozenset({"user.metrics"}),
        ),
        HealthProviderCategory(
            resource_type=HealthResourceType.WORKOUT,
            provider_category="workouts",
            required_scopes=frozenset({"user.activity"}),
        ),
        HealthProviderCategory(
            resource_type=HealthResourceType.SLEEP,
            provider_category="sleep",
            required_scopes=frozenset({"user.activity"}),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class HealthOAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None = None
    external_user_id: str | None = None
    granted_scopes: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "access_token", _require_text(self.access_token, field_name="access_token"))
        if self.refresh_token is not None:
            object.__setattr__(
                self,
                "refresh_token",
                _require_text(self.refresh_token, field_name="refresh_token"),
            )
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", _normalize_datetime(self.expires_at))
        if self.external_user_id is not None:
            object.__setattr__(
                self,
                "external_user_id",
                _require_text(self.external_user_id, field_name="external_user_id"),
            )
        object.__setattr__(self, "granted_scopes", _freeze_scopes(self.granted_scopes))


@dataclass(frozen=True, slots=True)
class HealthSyncCursor:
    resource_type: HealthResourceType
    last_modified: datetime | None = None
    page_offset: int | None = None
    etag: str | None = None
    overlap_window: timedelta = DEFAULT_CURSOR_OVERLAP

    def __post_init__(self) -> None:
        if self.last_modified is not None:
            object.__setattr__(self, "last_modified", _normalize_datetime(self.last_modified))
        if self.page_offset is not None and self.page_offset < 0:
            raise ValueError("page_offset must be >= 0 when provided")
        if self.etag is not None:
            object.__setattr__(self, "etag", _require_text(self.etag, field_name="etag"))
        if self.overlap_window <= timedelta(0):
            raise ValueError("overlap_window must be positive")

    def to_state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"resource_type": self.resource_type.value}
        if self.last_modified is not None:
            state["last_modified"] = _datetime_to_wire(self.last_modified)
        if self.page_offset is not None:
            state["page_offset"] = self.page_offset
        if self.etag is not None:
            state["etag"] = self.etag
        return state

    @classmethod
    def from_state(cls, state: Mapping[str, Any]) -> "HealthSyncCursor":
        resource_type = HealthResourceType(state["resource_type"])
        last_modified = state.get("last_modified")
        page_offset = state.get("page_offset")
        etag = state.get("etag")
        return cls(
            resource_type=resource_type,
            last_modified=_datetime_from_wire(last_modified) if isinstance(last_modified, str) else None,
            page_offset=int(page_offset) if page_offset is not None else None,
            etag=str(etag) if etag is not None else None,
        )


@dataclass(frozen=True, slots=True)
class HealthSourceRecord:
    provider: HealthProviderSlug
    resource_type: HealthResourceType
    external_id: str
    source_created_at: datetime | None = None
    source_modified_at: datetime | None = None
    observed_at: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    source_timezone: str | None = None
    source_offset_seconds: int | None = None
    source_device_id: str | None = None
    source_device_model: str | None = None
    payload_hash: str | None = None
    provider_revision: str | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, Any] = field(default_factory=dict)
    is_deleted: bool = False
    deleted_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _require_text(self.external_id, field_name="external_id"))
        for field_name in (
            "source_created_at",
            "source_modified_at",
            "observed_at",
            "starts_at",
            "ends_at",
            "deleted_at",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(self, field_name, _normalize_datetime(value))
        object.__setattr__(self, "source_metadata", dict(self.source_metadata))
        object.__setattr__(self, "attribution", dict(self.attribution))
        if self.ends_at is not None and self.starts_at is not None and self.ends_at < self.starts_at:
            raise ValueError("ends_at must not be earlier than starts_at")
        if self.is_deleted and self.deleted_at is None:
            raise ValueError("deleted_at is required when is_deleted is true")


@dataclass(frozen=True, slots=True)
class HealthTombstone:
    provider: HealthProviderSlug
    resource_type: HealthResourceType
    external_id: str
    deleted_at: datetime
    provider_revision: str | None = None
    reason: str = "provider_deleted"

    def __post_init__(self) -> None:
        object.__setattr__(self, "external_id", _require_text(self.external_id, field_name="external_id"))
        object.__setattr__(self, "deleted_at", _normalize_datetime(self.deleted_at))
        object.__setattr__(self, "reason", _require_text(self.reason, field_name="reason"))


@dataclass(frozen=True, slots=True)
class HealthDirtyState:
    resource_type: HealthResourceType
    first_dirty_at: datetime
    last_dirty_at: datetime
    reason: str = "webhook"
    receipt_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "first_dirty_at", _normalize_datetime(self.first_dirty_at))
        object.__setattr__(self, "last_dirty_at", _normalize_datetime(self.last_dirty_at))
        object.__setattr__(self, "reason", _require_text(self.reason, field_name="reason"))
        if self.receipt_id is not None:
            object.__setattr__(self, "receipt_id", _require_text(self.receipt_id, field_name="receipt_id"))
        if self.last_dirty_at < self.first_dirty_at:
            raise ValueError("last_dirty_at must not be earlier than first_dirty_at")


@dataclass(frozen=True, slots=True)
class HealthSyncError:
    kind: HealthSyncErrorKind
    code: str
    detail: str
    retry_after_seconds: int | None = None
    provider_status_code: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _require_text(self.code, field_name="code"))
        object.__setattr__(self, "detail", self.detail.strip())
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be >= 0 when provided")
        if self.provider_status_code is not None and self.provider_status_code < 0:
            raise ValueError("provider_status_code must be >= 0 when provided")

    @property
    def retryable(self) -> bool:
        return self.kind in {HealthSyncErrorKind.RATE_LIMIT, HealthSyncErrorKind.TRANSIENT}

    @classmethod
    def retryable_error(
        cls,
        *,
        code: str,
        detail: str,
        kind: HealthSyncErrorKind = HealthSyncErrorKind.TRANSIENT,
        retry_after_seconds: int | None = None,
        provider_status_code: int | None = None,
    ) -> "HealthSyncError":
        if kind not in {HealthSyncErrorKind.RATE_LIMIT, HealthSyncErrorKind.TRANSIENT}:
            raise ValueError("retryable_error requires a retryable error kind")
        return cls(
            kind=kind,
            code=code,
            detail=detail,
            retry_after_seconds=retry_after_seconds,
            provider_status_code=provider_status_code,
        )

    @classmethod
    def permanent_error(
        cls,
        *,
        code: str,
        detail: str,
        kind: HealthSyncErrorKind = HealthSyncErrorKind.PERMANENT,
        provider_status_code: int | None = None,
    ) -> "HealthSyncError":
        if kind in {HealthSyncErrorKind.RATE_LIMIT, HealthSyncErrorKind.TRANSIENT}:
            raise ValueError("permanent_error requires a non-retryable error kind")
        return cls(
            kind=kind,
            code=code,
            detail=detail,
            provider_status_code=provider_status_code,
        )


@dataclass(frozen=True, slots=True)
class HealthFetchResult:
    resource_type: HealthResourceType
    records: tuple[HealthSourceRecord, ...] = ()
    tombstones: tuple[HealthTombstone, ...] = ()
    next_cursor: HealthSyncCursor | None = None
    has_more: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "records", tuple(self.records))
        object.__setattr__(self, "tombstones", tuple(self.tombstones))
        if self.next_cursor is not None and self.next_cursor.resource_type != self.resource_type:
            raise ValueError("next_cursor.resource_type must match resource_type")
        if self.has_more and self.next_cursor is None:
            raise ValueError("next_cursor is required when has_more is true")


@dataclass(frozen=True, slots=True)
class HealthSyncOutcome:
    resource_type: HealthResourceType
    status: HealthSyncStatus
    cursor_before: HealthSyncCursor | None = None
    cursor_after: HealthSyncCursor | None = None
    page_count: int = 0
    fetched_count: int = 0
    inserted_count: int = 0
    updated_count: int = 0
    deleted_count: int = 0
    duplicate_count: int = 0
    tombstones: tuple[HealthTombstone, ...] = ()
    error: HealthSyncError | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "page_count",
            "fetched_count",
            "inserted_count",
            "updated_count",
            "deleted_count",
            "duplicate_count",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be >= 0")
        object.__setattr__(self, "tombstones", tuple(self.tombstones))
        if self.cursor_before is not None and self.cursor_before.resource_type != self.resource_type:
            raise ValueError("cursor_before.resource_type must match resource_type")
        if self.cursor_after is not None and self.cursor_after.resource_type != self.resource_type:
            raise ValueError("cursor_after.resource_type must match resource_type")
        if self.status == HealthSyncStatus.COMPLETED and self.error is not None:
            raise ValueError("completed outcomes must not include an error")


class WithingsMeasureType(IntEnum):
    """Canonical Withings measure type constants.

    Only types requested by the live adapter meastypes parameter (1, 6, 8,
    76, 88) are guaranteed to appear in source_metadata measures.  Other
    known types are listed for completeness but are not fetches by default.
    """

    WEIGHT_KG = 1
    FAT_RATIO_PCT = 6
    FAT_MASS_WEIGHT_KG = 8
    MUSCLE_MASS_KG = 76
    BONE_MASS_KG = 88

    # Known but not in the default meastypes fetch set.
    DIASTOLIC_BP_MMHG = 9
    SYSTOLIC_BP_MMHG = 10
    HEART_RATE_BPM = 11
    SPO2_PCT = 54


# Canonical metric name and unit per Withings measure type.
# Only types in the default fetch set are mapped here.  Absence of a
# type key in this map means "do not produce a normalized row."
WITHINGS_METRIC_MAP: dict[int, tuple[str, str]] = {
    WithingsMeasureType.WEIGHT_KG: ("weight", "kg"),
    WithingsMeasureType.FAT_RATIO_PCT: ("fat_ratio", "percent"),
    WithingsMeasureType.FAT_MASS_WEIGHT_KG: ("fat_mass", "kg"),
    WithingsMeasureType.MUSCLE_MASS_KG: ("muscle_mass", "kg"),
    WithingsMeasureType.BONE_MASS_KG: ("bone_mass", "kg"),
}


@dataclass(frozen=True, slots=True)
class NormalizedMeasurement:
    """A single decoded measurement row ready for persistence.

    This is the pure domain shape produced by the normalization layer.
    It carries enough context for upsert and attribution without
    coupling to any particular storage engine.
    """

    metric: str
    measured_at: datetime
    value_numeric: float
    canonical_unit: str
    source_timezone: str | None = None
    source_offset_seconds: int | None = None
    source_device_id: str | None = None
    source_device_model: str | None = None
    attribution: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metric", _require_text(self.metric, field_name="metric"))
        object.__setattr__(self, "measured_at", _normalize_datetime(self.measured_at))
        object.__setattr__(self, "canonical_unit", _require_text(self.canonical_unit, field_name="canonical_unit"))
        object.__setattr__(self, "attribution", dict(self.attribution))


@dataclass(frozen=True, slots=True)
class NormalizedSleep:
    """A single normalized sleep summary row ready for persistence.

    This carries the decoded fields extracted from a Withings sleep
    summary source record.  Detail (stage-timeline) records are
    intentionally excluded — only summary records produce rows in
    ``health_normalized_sleep``.
    """

    started_at: datetime
    ended_at: datetime
    local_sleep_date: date
    local_timezone: str | None = None
    local_offset_seconds: int | None = None
    completeness_state: str = "partial"
    total_in_bed_seconds: int | None = None
    total_asleep_seconds: int | None = None
    awake_seconds: int | None = None
    light_sleep_seconds: int | None = None
    deep_sleep_seconds: int | None = None
    rem_sleep_seconds: int | None = None
    sleep_latency_seconds: int | None = None
    wake_after_sleep_onset_seconds: int | None = None
    wakeups: int | None = None
    sleep_score: int | None = None
    source_device_id: str | None = None
    source_device_model: str | None = None
    attribution: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "started_at", _normalize_datetime(self.started_at))
        object.__setattr__(self, "ended_at", _normalize_datetime(self.ended_at))
        object.__setattr__(self, "completeness_state", _require_text(self.completeness_state, field_name="completeness_state"))
        object.__setattr__(self, "attribution", dict(self.attribution))


__all__ = [
    "DEFAULT_CURSOR_OVERLAP",
    "HealthDirtyState",
    "HealthFetchResult",
    "HealthOAuthTokens",
    "HealthProviderCapabilities",
    "HealthProviderCategory",
    "HealthProviderSlug",
    "HealthResourceType",
    "HealthSourceRecord",
    "HealthSyncCursor",
    "HealthSyncError",
    "HealthSyncErrorKind",
    "HealthSyncOutcome",
    "HealthSyncStatus",
    "HealthTombstone",
    "NormalizedMeasurement",
    "NormalizedSleep",
    "WITHINGS_METRIC_MAP",
    "WITHINGS_PROVIDER_CAPABILITIES",
    "WithingsMeasureType",
    "build_fallback_external_id",
    "resolve_external_id",
]
