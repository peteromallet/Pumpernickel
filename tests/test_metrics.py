"""Unit tests for the lightweight metrics layer."""

from __future__ import annotations

import logging

import pytest

from app.services import metrics


def _last_record(caplog: pytest.LogCaptureFixture) -> logging.LogRecord:
    recs = [r for r in caplog.records if r.name == "app.metrics"]
    assert recs, "no metrics records emitted"
    return recs[-1]


def test_incr_emits_counter_with_labels(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="app.metrics"):
        metrics.incr("inbound_attempts_started", bot="mediator")
    rec = _last_record(caplog)
    assert rec.metric == "inbound_attempts_started"
    assert rec.metric_kind == "counter"
    assert rec.value == 1.0
    assert rec.labels == {"bot": "mediator"}


def test_incr_value_kwarg_overrides_count(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="app.metrics"):
        metrics.incr(
            "inbound_attempts_completed",
            value=5,
            bot="hector",
            failure_class="retryable_pre_send",
        )
    rec = _last_record(caplog)
    assert rec.value == 5.0
    assert rec.labels == {"bot": "hector", "failure_class": "retryable_pre_send"}


def test_gauge_emits_gauge(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="app.metrics"):
        metrics.gauge("terminal_rows_without_outbound", 3, bot="mediator")
    rec = _last_record(caplog)
    assert rec.metric_kind == "gauge"
    assert rec.value == 3.0


def test_observe_emits_histogram_obs(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="app.metrics"):
        metrics.observe("attempt_age_seconds", 0.42, bot="mediator", quantile="p95")
    rec = _last_record(caplog)
    assert rec.metric_kind == "histogram_obs"
    assert rec.value == pytest.approx(0.42)
    assert rec.labels == {"bot": "mediator", "quantile": "p95"}


def test_none_label_is_stringified_empty(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="app.metrics"):
        metrics.incr("recovery_skipped_missing_coalescer", bot=None)
    rec = _last_record(caplog)
    assert rec.labels == {"bot": ""}
