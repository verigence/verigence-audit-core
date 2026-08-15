from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Protocol
from uuid import uuid4

logger = logging.getLogger("audit_core.telemetry")

_trace_id: ContextVar[str | None] = ContextVar("audit_core_trace_id", default=None)
_span_id: ContextVar[str | None] = ContextVar("audit_core_span_id", default=None)

_FORBIDDEN_METRIC_LABELS = {
    "tenant_id",
    "actor_id",
    "booking_id",
    "customer_id",
    "journey_id",
    "subject_id",
    "document_id",
    "job_id",
    "workflow_task_id",
    "correlation_id",
}


def _service_version() -> str:
    try:
        return version("verigence-audit-core")
    except PackageNotFoundError:
        return "unknown"


@dataclass(frozen=True)
class MetricPoint:
    name: str
    value: float
    kind: str
    labels: Mapping[str, str]


@dataclass(frozen=True)
class SpanRecord:
    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    duration_ms: float
    outcome: str
    correlation_id: str | None
    attributes: Mapping[str, str]


class TelemetrySink(Protocol):
    def metric(self, point: MetricPoint) -> None: ...

    def span(self, record: SpanRecord) -> None: ...


class StructuredLogTelemetrySink:
    """Provider-neutral default sink suitable for asynchronous log export."""

    def metric(self, point: MetricPoint) -> None:
        logger.info(
            "metric_point",
            extra={
                "telemetry_type": "metric",
                "metric_name": point.name,
                "metric_value": point.value,
                "metric_kind": point.kind,
                "metric_labels": dict(point.labels),
                **_platform_context(),
            },
        )

    def span(self, record: SpanRecord) -> None:
        logger.info(
            "trace_span",
            extra={
                "telemetry_type": "trace",
                "span_name": record.name,
                "trace_id": record.trace_id,
                "span_id": record.span_id,
                "parent_span_id": record.parent_span_id,
                "duration_ms": record.duration_ms,
                "outcome": record.outcome,
                "correlation_id": record.correlation_id,
                "span_attributes": dict(record.attributes),
                **_platform_context(),
            },
        )


def _platform_context() -> dict[str, str]:
    return {
        "service_name": os.environ.get("SERVICE_NAME", "verigence-audit-core"),
        "service_version": _service_version(),
        "environment": os.environ.get("APP_ENV", "unknown"),
    }


_sink: TelemetrySink = StructuredLogTelemetrySink()


def set_telemetry_sink(sink: TelemetrySink) -> TelemetrySink:
    """Replace the sink for tests/adapters and return the previous sink."""
    global _sink
    previous = _sink
    _sink = sink
    return previous


def record_metric(
    name: str,
    value: float = 1,
    *,
    kind: str = "counter",
    labels: Mapping[str, str] | None = None,
) -> None:
    """Record a bounded-cardinality metric without risking the business path."""
    safe_labels = _safe_metric_labels(labels or {})
    point = MetricPoint(
        name=name,
        value=float(value),
        kind=kind,
        labels=safe_labels,
    )
    try:
        _sink.metric(point)
    except Exception:  # noqa: BLE001 -- telemetry must never block business processing
        logger.warning("telemetry_metric_export_failed", exc_info=False)


def _safe_metric_labels(labels: Mapping[str, str]) -> dict[str, str]:
    forbidden = _FORBIDDEN_METRIC_LABELS.intersection(labels)
    if forbidden:
        names = ", ".join(sorted(forbidden))
        raise ValueError(f"High-cardinality metric labels are forbidden: {names}")
    return {str(key): str(value) for key, value in labels.items()}


def current_trace_id() -> str | None:
    return _trace_id.get()


def current_span_id() -> str | None:
    return _span_id.get()


@contextmanager
def trace_span(
    name: str,
    *,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    attributes: Mapping[str, str] | None = None,
) -> Iterator[tuple[str, str]]:
    """Create a fail-safe local span that can be adapted to an external tracer."""
    parent_span_id = _span_id.get()
    resolved_trace_id = trace_id or _trace_id.get() or uuid4().hex
    span_id = uuid4().hex[:16]
    trace_token = _trace_id.set(resolved_trace_id)
    span_token = _span_id.set(span_id)
    started = time.perf_counter()
    outcome = "SUCCESS"
    try:
        yield resolved_trace_id, span_id
    except Exception:
        outcome = "FAILURE"
        raise
    finally:
        duration_ms = (time.perf_counter() - started) * 1000.0
        _span_id.reset(span_token)
        _trace_id.reset(trace_token)
        record = SpanRecord(
            name=name,
            trace_id=resolved_trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            duration_ms=duration_ms,
            outcome=outcome,
            correlation_id=correlation_id,
            attributes={str(k): str(v) for k, v in (attributes or {}).items()},
        )
        try:
            _sink.span(record)
        except Exception:  # noqa: BLE001 -- telemetry must never block business processing
            logger.warning("telemetry_trace_export_failed", exc_info=False)
