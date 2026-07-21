"""Tests for health-sync observability (sanitized metrics emission).

These tests verify that the metrics helpers in
``app.services.health_sync.metrics`` emit structured log records through
``app.services.metrics``, and that the instrumentation points in the sync
and worker modules actually call those helpers.

Every label assertion enforces the privacy boundary: only ``provider``,
``resource_type``, ``status``, ``error_kind``, and ``retryable`` may appear
in any metric label.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from app.services.health_sync import (
    FakeWithingsError,
    FakeWithingsProvider,
    HealthResourceType,
    HealthSyncError,
    HealthSyncErrorKind,
    HealthSyncStatus,
    WITHINGS_PROVIDER_CAPABILITIES,
    repository_for,
    sync_connection_resource_safely,
)
from app.services.health_sync import metrics as health_metrics
from tests.conftest import FakePool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CALLBACK_URL = "https://example.test/api/health/devices/withings/oauth/callback"

APPROVED_LABEL_KEYS = frozenset({"provider", "resource_type", "status", "error_kind", "retryable"})

FORBIDDEN_LABEL_SUBSTRINGS = (
    "user_id",
    "provider_user_id",
    "external_user_id",
    "access_token",
    "refresh_token",
    "device_id",
    "payload",
)


def _collect_metric_records(caplog, metric_name: str) -> list[dict]:
    """Return the ``extra`` dicts for every metric log matching *metric_name*."""
    records = []
    for record in caplog.records:
        if not hasattr(record, "metric"):
            continue
        if record.metric != metric_name:  # type: ignore[attr-defined]
            continue
        labels = getattr(record, "labels", {})
        records.append(
            {
                "metric": getattr(record, "metric", None),
                "metric_kind": getattr(record, "metric_kind", None),
                "value": getattr(record, "value", None),
                "labels": dict(labels) if labels else {},
            }
        )
    return records


def _assert_labels_approved(record: dict) -> None:
    """Verify every label key is in the approved set."""
    for key in record["labels"]:
        assert key in APPROVED_LABEL_KEYS, (
            f"metric {record['metric']} contains disallowed label key '{key}'. "
            f"Approved keys: {sorted(APPROVED_LABEL_KEYS)}"
        )


def _assert_no_forbidden_values(record: dict) -> None:
    """Verify no label value contains forbidden substrings."""
    for key, value in record["labels"].items():
        value_str = str(value).lower() if value else ""
        for forbidden in FORBIDDEN_LABEL_SUBSTRINGS:
            assert forbidden not in value_str, (
                f"metric {record['metric']} label '{key}' contains forbidden substring "
                f"'{forbidden}' in value '{value}'"
            )


async def _rotated_access_token(provider: FakeWithingsProvider) -> str:
    exchanged = await provider.exchange_code(
        code="synthetic-auth-code-001",
        redirect_uri=CALLBACK_URL,
    )
    token = exchanged.refresh_token or ""
    refreshed = await provider.refresh_token(refresh_token=token)
    return refreshed.access_token


# ---------------------------------------------------------------------------
# Direct metrics helper tests
# ---------------------------------------------------------------------------


class TestMetricsHelpersSanitized:
    """Verify the helper functions emit records with only approved labels."""

    def test_incr_metric_has_only_approved_labels(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_sync_attempt(
            provider="withings",
            resource_type="measurement",
        )
        records = _collect_metric_records(caplog, "health_sync_attempts_started")
        assert len(records) == 1
        rec = records[0]
        _assert_labels_approved(rec)
        _assert_no_forbidden_values(rec)
        assert rec["metric_kind"] == "counter"
        assert rec["value"] == 1.0

    def test_observe_metric_has_only_approved_labels(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_sync_duration(
            provider="withings",
            resource_type="sleep",
            duration_seconds=3.14,
            status="completed",
        )
        records = _collect_metric_records(caplog, "health_sync_duration_seconds")
        assert len(records) == 1
        rec = records[0]
        _assert_labels_approved(rec)
        _assert_no_forbidden_values(rec)
        assert rec["metric_kind"] == "histogram_obs"
        assert rec["value"] == 3.14

    def test_gauge_metric_has_only_approved_labels(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_worker_scan(
            provider="withings",
            claimed=3,
            synced=2,
            failed=1,
            scanned_connections=10,
        )
        for metric_name in (
            "health_sync_worker_claimed",
            "health_sync_worker_synced",
            "health_sync_worker_failed",
            "health_sync_worker_scanned_connections",
        ):
            records = _collect_metric_records(caplog, metric_name)
            assert len(records) == 1, f"no record for {metric_name}"
            rec = records[0]
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)
            assert rec["metric_kind"] == "gauge"

    def test_sync_outcome_includes_status_and_error_kind(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_sync_outcome(
            provider="withings",
            resource_type="workout",
            status="failed",
            error_kind="transient",
            retryable=True,
        )
        records = _collect_metric_records(caplog, "health_sync_attempts_completed")
        assert len(records) == 1
        rec = records[0]
        assert rec["labels"]["status"] == "failed"
        assert rec["labels"]["error_kind"] == "transient"
        assert rec["labels"]["retryable"] == "true"

    def test_cursor_error_includes_error_kind(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_cursor_error(
            provider="withings",
            resource_type="measurement",
            error_kind="invalid_cursor_state",
        )
        records = _collect_metric_records(caplog, "health_sync_cursor_errors")
        assert len(records) == 1
        rec = records[0]
        assert rec["labels"]["error_kind"] == "invalid_cursor_state"

    def test_retry_record_has_retryable_label(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_sync_retry(
            provider="withings",
            resource_type="sleep",
            retryable=True,
        )
        records = _collect_metric_records(caplog, "health_sync_retry")
        assert len(records) == 1
        rec = records[0]
        assert rec["labels"]["retryable"] == "true"

    def test_fetched_deleted_zero_count_noops(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_sync_fetched(
            provider="withings",
            resource_type="measurement",
            count=0,
        )
        health_metrics.record_sync_deleted(
            provider="withings",
            resource_type="measurement",
            count=0,
        )
        fetched = _collect_metric_records(caplog, "health_sync_records_fetched")
        deleted = _collect_metric_records(caplog, "health_sync_records_deleted")
        assert len(fetched) == 0
        assert len(deleted) == 0

    def test_projection_outcome_has_workout_resource_type(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_projection_outcome(
            provider="withings",
            status="projected",
        )
        records = _collect_metric_records(caplog, "health_sync_projection_outcome")
        assert len(records) == 1
        rec = records[0]
        assert rec["labels"]["resource_type"] == "workout"
        assert rec["labels"]["status"] == "projected"

    def test_stale_freshness_has_connection_resource_type(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        health_metrics.record_stale_freshness(
            provider="withings",
            resource_type="connection",
        )
        records = _collect_metric_records(caplog, "health_sync_stale_freshness")
        assert len(records) == 1
        rec = records[0]
        assert rec["labels"]["provider"] == "withings"


# ---------------------------------------------------------------------------
# Sync instrumentation tests
# ---------------------------------------------------------------------------


class TestSyncMetricsEmission:
    """Verify instrumentation in sync_connection_resource_safely emits metrics."""

    async def test_successful_sync_emits_attempt_and_outcome(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420001"
        )
        access_token = await _rotated_access_token(provider)

        with caplog.at_level(logging.INFO, logger="app.metrics"):
            outcome = await sync_connection_resource_safely(
                repository=repository,
                provider=provider,
                connection_id=connection_id,
                user_id=user_id,
                access_token=access_token,
                resource_type=HealthResourceType.MEASUREMENT,
                now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            )

        assert outcome.status == HealthSyncStatus.COMPLETED

        attempts = _collect_metric_records(caplog, "health_sync_attempts_started")
        assert len(attempts) >= 1
        for rec in attempts:
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)

        completed = _collect_metric_records(caplog, "health_sync_attempts_completed")
        assert len(completed) >= 1
        for rec in completed:
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)

        durations = _collect_metric_records(caplog, "health_sync_duration_seconds")
        assert len(durations) >= 1
        for rec in durations:
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)

        fetched = _collect_metric_records(caplog, "health_sync_records_fetched")
        assert len(fetched) >= 1
        for rec in fetched:
            _assert_labels_approved(rec)

        deleted = _collect_metric_records(caplog, "health_sync_records_deleted")
        # Deleted count may be zero in default scenarios without tombstones.
        for rec in deleted:
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)

    async def test_failed_sync_emits_failed_outcome(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")

        class _FailProvider:
            name = "withings"
            capabilities = WITHINGS_PROVIDER_CAPABILITIES

            async def exchange_code(self, *, code: str, redirect_uri: str):
                raise NotImplementedError

            async def refresh_token(self, *, refresh_token: str):
                raise NotImplementedError

            async def fetch_changes(self, **kwargs):
                raise RuntimeError(
                    HealthSyncError.permanent_error(
                        code="provider_rejected_request",
                        detail="test permanent failure",
                    )
                )

            async def revoke(self, *, access_token: str, refresh_token: str | None = None) -> None:
                raise NotImplementedError

        pool = FakePool()
        repository = repository_for(pool)
        provider = _FailProvider()
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420002"
        )

        with caplog.at_level(logging.INFO, logger="app.metrics"):
            outcome = await sync_connection_resource_safely(
                repository=repository,
                provider=provider,
                connection_id=connection_id,
                user_id=user_id,
                access_token="test-token",
                resource_type=HealthResourceType.MEASUREMENT,
                max_attempts=1,
                now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            )

        assert outcome.status == HealthSyncStatus.FAILED

        completed = _collect_metric_records(caplog, "health_sync_attempts_completed")
        assert len(completed) >= 1
        fail_rec = completed[-1]
        assert fail_rec["labels"]["status"] == "failed"
        assert fail_rec["labels"]["error_kind"] is not None
        assert fail_rec["labels"]["error_kind"] != ""

        durations = _collect_metric_records(caplog, "health_sync_duration_seconds")
        assert len(durations) >= 1
        assert durations[-1]["labels"]["status"] == "failed"

    async def test_retryable_sync_emits_retry_metric(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")

        class _RetryProvider(FakeWithingsProvider):
            def __init__(self) -> None:
                super().__init__()
                self.call_count = 0

            async def fetch_changes(self, **kwargs):
                self.call_count += 1
                if self.call_count == 1:
                    raise FakeWithingsError(
                        HealthSyncError.retryable_error(
                            code="http_503",
                            detail="transient failure",
                        )
                    )
                return await super().fetch_changes(**kwargs)

        pool = FakePool()
        repository = repository_for(pool)
        provider = _RetryProvider()
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420003"
        )
        access_token = await _rotated_access_token(provider)
        # Reset call count after token rotation
        provider.call_count = 0

        with caplog.at_level(logging.INFO, logger="app.metrics"):
            outcome = await sync_connection_resource_safely(
                repository=repository,
                provider=provider,
                connection_id=connection_id,
                user_id=user_id,
                access_token=access_token,
                resource_type=HealthResourceType.MEASUREMENT,
                now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            )

        assert outcome.status == HealthSyncStatus.COMPLETED

        retries = _collect_metric_records(caplog, "health_sync_retry")
        assert len(retries) >= 1
        for rec in retries:
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)

    async def test_cursor_error_emits_cursor_error_metric(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id,
            external_user_id="420004",
            cursor_state={"measurement": {"resource_type": "measurement", "page_offset": 5}},
        )
        access_token = await _rotated_access_token(provider)

        with caplog.at_level(logging.INFO, logger="app.metrics"):
            outcome = await sync_connection_resource_safely(
                repository=repository,
                provider=provider,
                connection_id=connection_id,
                user_id=user_id,
                access_token=access_token,
                resource_type=HealthResourceType.MEASUREMENT,
                now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            )

        assert outcome.status == HealthSyncStatus.FAILED
        assert outcome.error is not None
        assert outcome.error.kind == HealthSyncErrorKind.INVALID_CURSOR_STATE

        cursor_errors = _collect_metric_records(caplog, "health_sync_cursor_errors")
        assert len(cursor_errors) >= 1
        for rec in cursor_errors:
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)

    async def test_no_user_id_in_any_metric_label(self, caplog):
        """Prove that metrics emitted during sync never leak a user id."""
        caplog.set_level(logging.INFO, logger="app.metrics")
        pool = FakePool()
        repository = repository_for(pool)
        provider = FakeWithingsProvider()
        user_id = uuid4()
        connection_id = pool.seed_health_connection(
            user_id=user_id, external_user_id="420005"
        )
        access_token = await _rotated_access_token(provider)
        user_id_str = str(user_id)

        with caplog.at_level(logging.INFO, logger="app.metrics"):
            await sync_connection_resource_safely(
                repository=repository,
                provider=provider,
                connection_id=connection_id,
                user_id=user_id,
                access_token=access_token,
                resource_type=HealthResourceType.MEASUREMENT,
                now=datetime(2026, 7, 20, 10, 0, tzinfo=UTC),
            )

        for record in caplog.records:
            if not hasattr(record, "metric"):
                continue
            labels = getattr(record, "labels", {})
            labels_str = json.dumps(labels, default=str).lower()
            assert user_id_str.lower() not in labels_str, (
                f"metric {getattr(record, 'metric', '?')} leaked user_id "
                f"in labels: {labels}"
            )
            # Also check extra data
            extra = getattr(record, "extra", {}) if hasattr(record, "extra") else {}
            extra_str = json.dumps(extra, default=str).lower()
            assert user_id_str.lower() not in extra_str, (
                f"metric {getattr(record, 'metric', '?')} leaked user_id "
                f"in extra: {extra}"
            )


# ---------------------------------------------------------------------------
# Worker scan metrics tests
# ---------------------------------------------------------------------------


class TestWorkerMetricsEmission:
    """Verify the worker emits gauge metrics on each scan."""

    async def test_worker_gauge_helpers_emit_approved_labels(self, caplog):
        """Test that the worker gauge helper emits correctly with sanitized labels."""
        caplog.set_level(logging.INFO, logger="app.metrics")

        health_metrics.record_worker_scan(
            provider="withings",
            claimed=3,
            synced=2,
            failed=1,
            skipped_disabled=0,
            reconciliation_outcomes=5,
            skipped_connections=1,
            scanned_connections=10,
        )

        for metric_name in (
            "health_sync_worker_claimed",
            "health_sync_worker_synced",
            "health_sync_worker_failed",
            "health_sync_worker_skipped_disabled",
            "health_sync_worker_reconciliation_outcomes",
            "health_sync_worker_skipped_connections",
            "health_sync_worker_scanned_connections",
        ):
            records = _collect_metric_records(caplog, metric_name)
            assert len(records) == 1, f"no record for {metric_name}"
            rec = records[0]
            _assert_labels_approved(rec)
            _assert_no_forbidden_values(rec)
            assert rec["metric_kind"] == "gauge"

    async def test_worker_gauge_no_user_id_leak(self, caplog):
        """Prove worker gauge labels never contain forbidden substrings."""
        caplog.set_level(logging.INFO, logger="app.metrics")

        health_metrics.record_worker_scan(
            provider="withings",
            claimed=1,
            synced=1,
        )

        for record in caplog.records:
            if not hasattr(record, "metric"):
                continue
            labels = getattr(record, "labels", {})
            labels_str = json.dumps(labels, default=str).lower()
            for forbidden in ("user_id", "external_user_id", "access_token", "refresh_token", "device_id", "payload"):
                assert forbidden not in labels_str, (
                    f"worker metric {getattr(record, 'metric', '?')} leaked "
                    f"'{forbidden}' in labels: {labels}"
                )


# ---------------------------------------------------------------------------
# Stale freshness metric tests
# ---------------------------------------------------------------------------


class TestStaleFreshnessMetric:
    """Verify stale freshness helper emits sanitized metrics.

    The ``record_stale_freshness`` helper is available for callers that know
    the provider context (e.g., reconciliation loop or worker scan).  This
    test class validates the helper's label contract directly.
    """

    def test_record_stale_freshness_emits_approved_labels(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")

        health_metrics.record_stale_freshness(
            provider="withings",
            resource_type="measurement",
        )

        records = _collect_metric_records(caplog, "health_sync_stale_freshness")
        assert len(records) == 1
        rec = records[0]
        _assert_labels_approved(rec)
        _assert_no_forbidden_values(rec)
        assert rec["labels"]["provider"] == "withings"
        assert rec["labels"]["resource_type"] == "measurement"
        assert rec["metric_kind"] == "counter"

    def test_record_stale_freshness_no_user_id_leak(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")

        health_metrics.record_stale_freshness(
            provider="withings",
            resource_type="sleep",
        )

        for record in caplog.records:
            if not hasattr(record, "metric"):
                continue
            labels = getattr(record, "labels", {})
            labels_str = json.dumps(labels, default=str).lower()
            for forbidden in ("user_id", "access_token", "device_id", "payload"):
                assert forbidden not in labels_str, (
                    f"stale freshness metric leaked '{forbidden}': {labels}"
                )


# ---------------------------------------------------------------------------
# Projection outcome metric tests
# ---------------------------------------------------------------------------


class TestProjectionOutcomeMetric:
    """Verify projection outcome metrics use sanitized labels."""

    def test_projection_outcome_labels_do_not_leak_ids(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")

        health_metrics.record_projection_outcome(
            provider="withings",
            status="projected",
        )

        records = _collect_metric_records(caplog, "health_sync_projection_outcome")
        assert len(records) == 1
        rec = records[0]
        _assert_labels_approved(rec)
        _assert_no_forbidden_values(rec)

    def test_projection_error_outcome_includes_error_kind(self, caplog):
        caplog.set_level(logging.INFO, logger="app.metrics")

        health_metrics.record_projection_outcome(
            provider="withings",
            status="error",
            error_kind="permanent",
        )

        records = _collect_metric_records(caplog, "health_sync_projection_outcome")
        assert len(records) == 1
        rec = records[0]
        assert rec["labels"]["status"] == "error"
        assert rec["labels"]["error_kind"] == "permanent"
