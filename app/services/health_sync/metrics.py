"""Sanitized health-sync metrics helpers.

Every helper emits through the existing ``app.services.metrics`` log-based
layer with a strict privacy boundary: labels MUST use only ``provider``,
``resource_type``, ``status``, ``error_kind``, and ``retryable``.  No user
ids, provider user ids, tokens, raw payloads, device ids, or measurement
values may appear in any metric label or value.
"""

from __future__ import annotations

from app.services.health_sync.models import (
    HealthProviderSlug,
    HealthResourceType,
    HealthSyncError,
    HealthSyncErrorKind,
    HealthSyncStatus,
)
from app.services.metrics import gauge as _gauge
from app.services.metrics import incr as _incr
from app.services.metrics import observe as _observe


# ═══════════════════════════════════════════════════════════════════════════════
# Metric name constants
# ═══════════════════════════════════════════════════════════════════════════════

_METRIC_ATTEMPTS_STARTED = "health_sync_attempts_started"
_METRIC_ATTEMPTS_COMPLETED = "health_sync_attempts_completed"
_METRIC_DURATION_SECONDS = "health_sync_duration_seconds"
_METRIC_RECORDS_FETCHED = "health_sync_records_fetched"
_METRIC_RECORDS_DELETED = "health_sync_records_deleted"
_METRIC_RETRY = "health_sync_retry"
_METRIC_CURSOR_ERRORS = "health_sync_cursor_errors"
_METRIC_STALE_FRESHNESS = "health_sync_stale_freshness"
_METRIC_PROJECTION_OUTCOME = "health_sync_projection_outcome"
_METRIC_WORKER_CLAIMED = "health_sync_worker_claimed"
_METRIC_WORKER_SYNCED = "health_sync_worker_synced"
_METRIC_WORKER_FAILED = "health_sync_worker_failed"
_METRIC_WORKER_SKIPPED_DISABLED = "health_sync_worker_skipped_disabled"
_METRIC_WORKER_RECONCILIATION_OUTCOMES = "health_sync_worker_reconciliation_outcomes"
_METRIC_WORKER_SKIPPED_CONNECTIONS = "health_sync_worker_skipped_connections"
_METRIC_WORKER_SCANNED_CONNECTIONS = "health_sync_worker_scanned_connections"


def _provider_label(provider: HealthProviderSlug | str) -> str:
    if isinstance(provider, HealthProviderSlug):
        return provider.value
    return str(provider)


def _resource_label(resource_type: HealthResourceType | str) -> str:
    if isinstance(resource_type, HealthResourceType):
        return resource_type.value
    return str(resource_type)


# ═══════════════════════════════════════════════════════════════════════════════
# Sync attempt / outcome helpers
# ═══════════════════════════════════════════════════════════════════════════════


def record_sync_attempt(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
) -> None:
    """Emit a counter for every sync attempt (before retries)."""
    _incr(
        _METRIC_ATTEMPTS_STARTED,
        provider=_provider_label(provider),
        resource_type=_resource_label(resource_type),
    )


def record_sync_outcome(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
    status: HealthSyncStatus | str,
    error_kind: HealthSyncErrorKind | str | None = None,
    retryable: bool | None = None,
) -> None:
    """Emit a counter for the final sync result (success, partial, failed)."""
    _status_str = status.value if isinstance(status, HealthSyncStatus) else str(status)
    _error_kind_str = ""
    if error_kind is not None:
        _error_kind_str = error_kind.value if isinstance(error_kind, HealthSyncErrorKind) else str(error_kind)
    labels: dict[str, str] = {
        "provider": _provider_label(provider),
        "resource_type": _resource_label(resource_type),
        "status": _status_str,
        "error_kind": _error_kind_str,
        "retryable": "true" if retryable else "false",
    }
    _incr(_METRIC_ATTEMPTS_COMPLETED, **labels)


def record_sync_duration(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
    duration_seconds: float,
    status: HealthSyncStatus | str,
) -> None:
    """Emit a histogram observation for the wall-clock duration of a sync."""
    _status_str = status.value if isinstance(status, HealthSyncStatus) else str(status)
    _observe(
        _METRIC_DURATION_SECONDS,
        float(duration_seconds),
        provider=_provider_label(provider),
        resource_type=_resource_label(resource_type),
        status=_status_str,
    )


def record_sync_fetched(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
    count: int,
) -> None:
    """Emit a counter for the number of records (including tombstones) fetched."""
    if count <= 0:
        return
    _incr(
        _METRIC_RECORDS_FETCHED,
        value=float(count),
        provider=_provider_label(provider),
        resource_type=_resource_label(resource_type),
    )


def record_sync_deleted(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
    count: int,
) -> None:
    """Emit a counter for the number of tombstones received."""
    if count <= 0:
        return
    _incr(
        _METRIC_RECORDS_DELETED,
        value=float(count),
        provider=_provider_label(provider),
        resource_type=_resource_label(resource_type),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Retry / error helpers
# ═══════════════════════════════════════════════════════════════════════════════


def record_sync_retry(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
    retryable: bool,
) -> None:
    """Emit a counter each time a sync retry is attempted or skipped."""
    _incr(
        _METRIC_RETRY,
        provider=_provider_label(provider),
        resource_type=_resource_label(resource_type),
        retryable="true" if retryable else "false",
    )


def record_cursor_error(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
    error_kind: HealthSyncErrorKind | str = "invalid_cursor_state",
) -> None:
    """Emit a counter for cursor-state errors that abort a sync."""
    _error_kind_str = error_kind.value if isinstance(error_kind, HealthSyncErrorKind) else str(error_kind)
    _incr(
        _METRIC_CURSOR_ERRORS,
        provider=_provider_label(provider),
        resource_type=_resource_label(resource_type),
        error_kind=_error_kind_str,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Freshness / staleness helpers
# ═══════════════════════════════════════════════════════════════════════════════


def record_stale_freshness(
    *,
    provider: HealthProviderSlug | str,
    resource_type: HealthResourceType | str,
) -> None:
    """Emit a counter when a connection's freshness is stale (>24h since last sync)."""
    _incr(
        _METRIC_STALE_FRESHNESS,
        provider=_provider_label(provider),
        resource_type=_resource_label(resource_type),
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Projection outcome helpers
# ═══════════════════════════════════════════════════════════════════════════════


def record_projection_outcome(
    *,
    provider: HealthProviderSlug | str,
    status: str,
    error_kind: str | None = None,
) -> None:
    """Emit a counter for each projection decision (projected, no_match, removed, error).

    ``resource_type`` is always ``workout`` for projection events.
    """
    labels: dict[str, str] = {
        "provider": _provider_label(provider),
        "resource_type": HealthResourceType.WORKOUT.value,
        "status": status,
    }
    if error_kind is not None:
        labels["error_kind"] = error_kind
    else:
        labels["error_kind"] = ""
    labels["retryable"] = "false"
    _incr(_METRIC_PROJECTION_OUTCOME, **labels)


# ═══════════════════════════════════════════════════════════════════════════════
# Worker scan helpers
# ═══════════════════════════════════════════════════════════════════════════════


def record_worker_scan(
    *,
    provider: HealthProviderSlug | str,
    claimed: int = 0,
    synced: int = 0,
    failed: int = 0,
    skipped_disabled: int = 0,
    reconciliation_outcomes: int = 0,
    skipped_connections: int = 0,
    scanned_connections: int = 0,
) -> None:
    """Emit gauge observations for a single worker scan cycle."""
    provider_label = _provider_label(provider)
    _gauge(_METRIC_WORKER_CLAIMED, float(claimed), provider=provider_label)
    _gauge(_METRIC_WORKER_SYNCED, float(synced), provider=provider_label)
    _gauge(_METRIC_WORKER_FAILED, float(failed), provider=provider_label)
    _gauge(_METRIC_WORKER_SKIPPED_DISABLED, float(skipped_disabled), provider=provider_label)
    _gauge(_METRIC_WORKER_RECONCILIATION_OUTCOMES, float(reconciliation_outcomes), provider=provider_label)
    _gauge(_METRIC_WORKER_SKIPPED_CONNECTIONS, float(skipped_connections), provider=provider_label)
    _gauge(_METRIC_WORKER_SCANNED_CONNECTIONS, float(scanned_connections), provider=provider_label)


__all__ = [
    "record_sync_attempt",
    "record_sync_outcome",
    "record_sync_duration",
    "record_sync_fetched",
    "record_sync_deleted",
    "record_sync_retry",
    "record_cursor_error",
    "record_stale_freshness",
    "record_projection_outcome",
    "record_worker_scan",
]
