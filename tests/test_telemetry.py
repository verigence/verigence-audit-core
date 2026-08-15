from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient

from audit_core.main import app
from audit_core.telemetry import (
    MetricPoint,
    SpanRecord,
    record_metric,
    set_telemetry_sink,
    trace_span,
)
from audit_core.workflow_telemetry import emit_workflow_health_metrics


@dataclass
class CaptureSink:
    metrics: list[MetricPoint] = field(default_factory=list)
    spans: list[SpanRecord] = field(default_factory=list)

    def metric(self, point: MetricPoint) -> None:
        self.metrics.append(point)

    def span(self, record: SpanRecord) -> None:
        self.spans.append(record)


class FailingSink:
    def metric(self, point: MetricPoint) -> None:
        raise RuntimeError("telemetry backend unavailable")

    def span(self, record: SpanRecord) -> None:
        raise RuntimeError("telemetry backend unavailable")


class _MappingsResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def mappings(self) -> _MappingsResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self._rows

    def one(self) -> dict[str, Any]:
        assert len(self._rows) == 1
        return self._rows[0]


class FakeWorkflowConnection:
    def execute(self, statement) -> _MappingsResult:
        sql = str(statement)
        if "GROUP BY task_status" in sql:
            return _MappingsResult(
                [
                    {"task_status": "READY", "task_count": 4},
                    {"task_status": "RETRY_WAIT", "task_count": 2},
                    {"task_status": "DEAD_LETTER", "task_count": 1},
                ]
            )
        return _MappingsResult(
            [
                {
                    "retry_wait": 2,
                    "stale_tasks": 1,
                    "dead_letter": 1,
                    "oldest_pending_seconds": 95.0,
                }
            ]
        )


def test_request_metrics_and_trace_share_correlation_context() -> None:
    sink = CaptureSink()
    previous = set_telemetry_sink(sink)
    try:
        client = TestClient(app)
        response = client.get(
            "/health",
            headers={"X-Correlation-ID": "corr-telemetry-1", "X-Trace-ID": "trace-parent-1"},
        )
        assert response.status_code == 200
        assert response.headers["X-Correlation-ID"] == "corr-telemetry-1"
        assert response.headers["X-Trace-ID"] == "trace-parent-1"

        request_metrics = [
            point for point in sink.metrics if point.name == "audit_core.http.requests"
        ]
        assert len(request_metrics) == 1
        assert request_metrics[0].labels == {"method": "GET", "status_class": "2xx"}
        assert not {
            "tenant_id",
            "actor_id",
            "workflow_task_id",
            "correlation_id",
        }.intersection(request_metrics[0].labels)

        request_spans = [
            span for span in sink.spans if span.name == "audit_core.http_request"
        ]
        assert len(request_spans) == 1
        assert request_spans[0].trace_id == "trace-parent-1"
        assert request_spans[0].correlation_id == "corr-telemetry-1"
        assert request_spans[0].outcome == "SUCCESS"
    finally:
        set_telemetry_sink(previous)


def test_workflow_health_emits_bounded_queue_retry_and_dead_letter_metrics() -> None:
    sink = CaptureSink()
    previous = set_telemetry_sink(sink)
    try:
        snapshot = emit_workflow_health_metrics(FakeWorkflowConnection())  # type: ignore[arg-type]
        assert snapshot == {
            "retry_wait": 2,
            "stale_tasks": 1,
            "dead_letter": 1,
            "oldest_pending_seconds": 95.0,
        }
        metric_names = {point.name for point in sink.metrics}
        assert {
            "audit_core.workflow.tasks",
            "audit_core.workflow.retry_wait",
            "audit_core.workflow.stale_tasks",
            "audit_core.workflow.dead_letter",
            "audit_core.workflow.oldest_pending_seconds",
        }.issubset(metric_names)
        assert all(
            not {"tenant_id", "actor_id", "workflow_task_id"}.intersection(point.labels)
            for point in sink.metrics
        )
        assert any(
            span.name == "audit_core.workflow.health_snapshot" for span in sink.spans
        )
    finally:
        set_telemetry_sink(previous)


def test_high_cardinality_metric_labels_are_rejected() -> None:
    with pytest.raises(ValueError, match="High-cardinality"):
        record_metric(
            "audit_core.bad_metric",
            labels={"tenant_id": "tenant-secret", "status": "READY"},
        )


def test_telemetry_backend_failure_never_blocks_business_code() -> None:
    previous = set_telemetry_sink(FailingSink())
    try:
        record_metric("audit_core.safe_metric", labels={"result": "SUCCESS"})
        with trace_span("audit_core.safe_span", correlation_id="corr-safe"):
            business_result = 42
        assert business_result == 42
    finally:
        set_telemetry_sink(previous)
