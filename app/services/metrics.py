"""Lightweight metrics layer for Project A3 (work item 6).

This module is intentionally NOT a Prometheus / statsd client.  The repo
currently has no metrics infra, and the work item explicitly forbids
introducing a new metrics library.  Instead we emit one structured log
line per counter increment / histogram observation, in a stable format
that a log-shipping pipeline (Vector, Loki, Datadog, etc.) can scrape and
turn into real metrics.

Emit format
-----------
A single ``logger.info("metric <name>", extra={...})`` call with the
``extra`` dict shaped as::

    {
        "metric": "<name>",
        "metric_kind": "counter" | "gauge" | "histogram_obs",
        "value": <number>,
        "labels": { "<label>": "<value>", ... },
    }

Downstream pipelines can then ``json.loads`` the structured record and
materialise a Prometheus / Influx series.

Counter names (Project A3, work item 6)
---------------------------------------
* ``inbound_attempts_started{bot}``
* ``inbound_attempts_completed{bot,failure_class}``
* ``recovery_requeued{bot,reason}``
* ``recovery_skipped_missing_coalescer{bot}``
* ``provider_fallback_invoked{from,to,phase,bot}``
* ``terminal_rows_without_outbound{bot}`` (periodic gauge)
* ``attempt_age_seconds{bot}`` (histogram observation)

Failure-class label values (SD-002): one of ``success`` (only used on
``inbound_attempts_completed`` from :func:`complete_messages`),
``retryable_pre_send``, ``terminal_post_send``, ``infra_bug``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("app.metrics")


def _emit(metric: str, kind: str, value: float, labels: dict[str, Any]) -> None:
    safe_labels = {
        str(k): ("" if v is None else str(v))
        for k, v in labels.items()
    }
    logger.info(
        "metric %s %s=%s labels=%s",
        metric,
        kind,
        value,
        safe_labels,
        extra={
            "metric": metric,
            "metric_kind": kind,
            "value": value,
            "labels": safe_labels,
        },
    )


def incr(metric: str, *, value: float = 1, **labels: Any) -> None:
    """Increment a counter.  Keyword arguments are emitted as labels."""
    _emit(metric, "counter", float(value), labels)


def gauge(metric: str, value: float, **labels: Any) -> None:
    """Emit a gauge sample."""
    _emit(metric, "gauge", float(value), labels)


def observe(metric: str, value: float, **labels: Any) -> None:
    """Emit a histogram observation (single sample of a distribution)."""
    _emit(metric, "histogram_obs", float(value), labels)
