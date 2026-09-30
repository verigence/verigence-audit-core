from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from typing import Any

import httpx
import structlog
from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry._logs import SeverityNumber, set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from audit_core.config import Settings
from audit_core.telemetry import configure_otel_telemetry

_REQUIRED_OTLP_ENDPOINTS = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
)

_OTEL_LOGGER: Any | None = None

_SAFE_LOG_ATTRIBUTES = {
    "correlation_id",
    "user_id",
    "actor_id",
    "tenant_id",
    "project_id",
    "journey_id",
    "evidence_id",
    "document_id",
    "stage",
    "method",
    "path",
    "route",
    "status_code",
    "duration_ms",
    "error_code",
    "error_category",
    "retryable",
    "dependency",
    "operation",
    "result",
    "attempt",
    "downstream_http_status",
    "exc_type",
    "pgcode",
    "stage_code",
    "task_id",
    "work_id",
    "work_type",
    "rule",
    "reason_code",
    "http_status",
}

_SEVERITY = {
    "debug": SeverityNumber.DEBUG,
    "info": SeverityNumber.INFO,
    "warning": SeverityNumber.WARN,
    "error": SeverityNumber.ERROR,
    "critical": SeverityNumber.FATAL,
}


def _service_version() -> str:
    from audit_core.logging_config import release_version

    return os.getenv("VERIGENCE_RELEASE") or release_version()


def _otlp_endpoints_configured() -> bool:
    return all(os.getenv(name, "").strip() for name in _REQUIRED_OTLP_ENDPOINTS)


def _bootstrap_warning(reason: str, exception_type: str | None = None) -> None:
    payload = {
        "severity": "WARNING",
        "event_name": "observability_bootstrap_disabled",
        "service_name": "verigence-audit-core",
        "reason": reason,
    }
    if exception_type:
        payload["exception_type"] = exception_type
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def emit_otel_log(event_dict: Mapping[str, Any]) -> None:
    """Queue one controlled structured log event for batched OTLP export.

    Only an explicit allow-list of operational fields is copied. The call never performs a
    synchronous network request; the SDK BatchLogRecordProcessor owns remote export.
    """
    if _OTEL_LOGGER is None:
        return
    try:
        event_name = str(event_dict.get("event", "audit_core_event"))
        level = str(event_dict.get("level", "info")).lower()
        attributes = {
            key: value
            for key, value in event_dict.items()
            if key in _SAFE_LOG_ATTRIBUTES and value is not None
        }
        _OTEL_LOGGER.emit(
            severity_number=_SEVERITY.get(level, SeverityNumber.INFO),
            severity_text=level.upper(),
            body=event_name,
            event_name=event_name,
            attributes=attributes,
        )
    except Exception:
        return


def attach_trusted_user_id(user_id: str) -> None:
    """Attach an authenticated opaque Verigence user ID to logs and the active trace."""
    if not user_id:
        return
    structlog.contextvars.bind_contextvars(user_id=user_id)
    span = trace.get_current_span()
    if span.is_recording():
        span.set_attribute("verigence.user.id", user_id)


def attach_business_context(context: Mapping[str, str]) -> None:
    """Attach opaque business identifiers to logs/traces, never to metric labels."""
    safe_context = {key: value for key, value in context.items() if value}
    if not safe_context:
        return
    structlog.contextvars.bind_contextvars(**safe_context)
    span = trace.get_current_span()
    if span.is_recording():
        for key, value in safe_context.items():
            span.set_attribute(f"verigence.{key.replace('_', '.')}", value)


def _current_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return str(value) if value else None


def _httpx_request_hook(span: Any, request: Any) -> None:
    correlation_id = _current_correlation_id()
    if not correlation_id:
        return
    if request.headers is not None:
        request.headers["X-Correlation-ID"] = correlation_id
    if span is not None and span.is_recording():
        span.set_attribute("verigence.correlation_id", correlation_id)


async def _httpx_async_request_hook(span: Any, request: Any) -> None:
    _httpx_request_hook(span, request)


_PROPAGATION_INSTALLED = False


def _with_correlation(request: httpx.Request) -> None:
    correlation_id = _current_correlation_id()
    # Presigned object-storage URLs are signed for exact headers; leave them untouched.
    if correlation_id and "X-Amz-Signature" not in str(request.url.query):
        request.headers.setdefault("X-Correlation-ID", correlation_id)


_outbound_logger = structlog.get_logger("audit_core.outbound")


def _dependency_name(host: str) -> str:
    """Which service an outbound call went to, from its host: enough to read
    the log per dependency without knowing the deployment's host names."""
    lowered = host.lower()
    if "security" in lowered:
        return "SECURITY"
    if "rule" in lowered:
        return "RULE_ENGINE"
    if "amazonaws" in lowered or "r2.cloudflarestorage" in lowered or "storage" in lowered:
        return "STORAGE"
    if lowered.startswith(("verigence-di", "di.")) or "-di-" in lowered:
        return "DI"
    return host


def _log_outbound(request: httpx.Request, *, started: float, response: httpx.Response | None, error: BaseException | None) -> None:
    """One line per outbound API call (decision 2026-09-30): where it went, how
    long it took, what came back. Path only: a query string can carry a
    presigned signature or an identifier; headers are never logged."""
    duration_ms = (time.perf_counter() - started) * 1000.0
    from audit_core.observability import add_timing

    add_timing("outbound_ms", duration_ms, count_key="outbound_calls")
    fields = {
        "dependency": _dependency_name(request.url.host),
        "method": request.method,
        "host": request.url.host,
        "path": request.url.path,
        "duration_ms": round(duration_ms, 1),
    }
    if error is not None:
        _outbound_logger.warning("outbound_request_failed", error_type=type(error).__name__, **fields)
    elif response.status_code >= 400:
        _outbound_logger.warning("outbound_request", status_code=response.status_code, **fields)
    else:
        _outbound_logger.info("outbound_request", status_code=response.status_code, **fields)


def install_correlation_propagation() -> None:
    """Send the current X-Correlation-ID on every outbound httpx call (DI, Security, Rule Engine,
    ...), whether or not OTLP export is configured, so one id follows a request or P2 work item
    across services; and log every outbound call once with its response time."""
    global _PROPAGATION_INSTALLED
    if _PROPAGATION_INSTALLED:
        return
    sync_send = httpx.Client.send
    async_send = httpx.AsyncClient.send

    def send(self: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        _with_correlation(request)
        started = time.perf_counter()
        try:
            response = sync_send(self, request, *args, **kwargs)
        except BaseException as exc:
            _log_outbound(request, started=started, response=None, error=exc)
            raise
        _log_outbound(request, started=started, response=response, error=None)
        return response

    async def asend(self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
        _with_correlation(request)
        started = time.perf_counter()
        try:
            response = await async_send(self, request, *args, **kwargs)
        except BaseException as exc:
            _log_outbound(request, started=started, response=None, error=exc)
            raise
        _log_outbound(request, started=started, response=response, error=None)
        return response

    httpx.Client.send = send  # type: ignore[method-assign]
    httpx.AsyncClient.send = asend  # type: ignore[method-assign]
    _PROPAGATION_INSTALLED = True


def configure_otlp(app: FastAPI, settings: Settings) -> bool:
    """Configure non-blocking, fail-open Phase-1 OpenTelemetry export."""
    global _OTEL_LOGGER

    if not settings.observability_enabled:
        return False
    if not _otlp_endpoints_configured():
        _bootstrap_warning("missing_otlp_endpoint_configuration")
        return False

    try:
        resource = Resource.create(
            {
                "service.namespace": "verigence",
                "service.name": settings.service_name,
                "service.version": _service_version(),
                "deployment.environment.name": settings.environment,
            }
        )

        export_timeout_ms = int(settings.observability_export_timeout_seconds * 1000)

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(timeout=settings.observability_export_timeout_seconds),
                max_queue_size=settings.observability_max_queue_size,
                max_export_batch_size=settings.observability_max_export_batch_size,
                schedule_delay_millis=settings.observability_batch_delay_ms,
                export_timeout_millis=export_timeout_ms,
            )
        )

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(timeout=settings.observability_export_timeout_seconds),
            export_interval_millis=settings.observability_metric_export_interval_ms,
            export_timeout_millis=export_timeout_ms,
        )
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(timeout=settings.observability_export_timeout_seconds),
                max_queue_size=settings.observability_max_queue_size,
                max_export_batch_size=settings.observability_max_export_batch_size,
                schedule_delay_millis=settings.observability_batch_delay_ms,
                export_timeout_millis=export_timeout_ms,
            )
        )

        trace.set_tracer_provider(tracer_provider)
        metrics.set_meter_provider(meter_provider)
        set_logger_provider(logger_provider)
        _OTEL_LOGGER = logger_provider.get_logger("audit_core")

        configure_otel_telemetry(
            meter_provider.get_meter("audit_core"),
            tracer_provider.get_tracer("audit_core"),
        )

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            excluded_urls="/health",
        )
        HTTPXClientInstrumentor().instrument(
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            request_hook=_httpx_request_hook,
            async_request_hook=_httpx_async_request_hook,
        )
        SQLAlchemyInstrumentor().instrument(tracer_provider=tracer_provider)
        return True
    except Exception as exc:  # pragma: no cover - defensive third-party boundary
        _OTEL_LOGGER = None
        _bootstrap_warning("initialization_failed", type(exc).__name__)
        return False
