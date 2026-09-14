from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any

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

_OTEL_LOGGER: Any | None = None
_LOGGER_PROVIDER: LoggerProvider | None = None
_METER_PROVIDER: MeterProvider | None = None
_TRACER_PROVIDER: TracerProvider | None = None
_EXPORT_ALL_LOGS = False
_EXPORT_ERRORS = False

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
}

_SEVERITY = {
    "debug": SeverityNumber.DEBUG,
    "info": SeverityNumber.INFO,
    "warning": SeverityNumber.WARN,
    "error": SeverityNumber.ERROR,
    "critical": SeverityNumber.FATAL,
    "exception": SeverityNumber.ERROR,
}


def _service_version() -> str:
    return (
        os.getenv("VERIGENCE_GIT_SHA")
        or os.getenv("RAILWAY_GIT_COMMIT_SHA")
        or os.getenv("VERIGENCE_RELEASE")
        or "unknown"
    )


def _resource(settings: Settings) -> Resource:
    return Resource.create(
        {
            "service.namespace": "verigence",
            "service.name": settings.service_name,
            "service.version": _service_version(),
            "deployment.environment.name": settings.environment,
        }
    )


def _signal_endpoint_configured(signal: str) -> bool:
    specific = os.getenv(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT", "").strip()
    generic = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    return bool(specific or generic)


def _bootstrap_warning(
    capability: str,
    reason: str,
    exception_type: str | None = None,
) -> None:
    payload = {
        "severity": "WARNING",
        "event_name": "observability_capability_disabled",
        "service_name": "verigence-audit-core",
        "capability": capability,
        "reason": reason,
    }
    if exception_type:
        payload["exception_type"] = exception_type
    sys.stderr.write(json.dumps(payload, separators=(",", ":")) + "\n")


def _primitive_attribute(value: Any) -> Any | None:
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)) and all(
        isinstance(item, (str, bool, int, float)) for item in value
    ):
        return tuple(value)
    return None


def _is_error_event(event_dict: Mapping[str, Any]) -> bool:
    level = str(event_dict.get("level", "info")).lower()
    return level in {"error", "critical", "exception"} or bool(event_dict.get("error_code"))


def emit_otel_log(event_dict: Mapping[str, Any]) -> None:
    """Queue one controlled structured event for OTLP log/error export."""
    if _OTEL_LOGGER is None:
        return
    if not _EXPORT_ALL_LOGS and not (_EXPORT_ERRORS and _is_error_event(event_dict)):
        return
    try:
        event_name = str(event_dict.get("event", "audit_core_event"))
        level = str(event_dict.get("level", "info")).lower()
        attributes: dict[str, Any] = {}
        for key, value in event_dict.items():
            if key not in _SAFE_LOG_ATTRIBUTES or value is None:
                continue
            safe_value = _primitive_attribute(value)
            if safe_value is not None:
                attributes[key] = safe_value
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
    """Attach an authenticated opaque Verigence user ID to logs and active trace."""
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


def _configure_logs(settings: Settings, resource: Resource) -> tuple[bool, bool]:
    global _OTEL_LOGGER, _LOGGER_PROVIDER, _EXPORT_ALL_LOGS, _EXPORT_ERRORS
    requested_logs = settings.observability_logs_enabled
    requested_errors = settings.observability_errors_enabled
    if not requested_logs and not requested_errors:
        return False, False
    if not _signal_endpoint_configured("logs"):
        if requested_logs:
            _bootstrap_warning("logs", "missing_otlp_endpoint")
        if requested_errors:
            _bootstrap_warning("errors", "missing_otlp_endpoint")
        return False, False
    try:
        export_timeout_ms = int(settings.observability_export_timeout_seconds * 1000)
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(
                OTLPLogExporter(timeout=settings.observability_export_timeout_seconds),
                max_queue_size=settings.observability_max_queue_size,
                max_export_batch_size=settings.observability_max_export_batch_size,
                schedule_delay_millis=settings.observability_batch_delay_ms,
                export_timeout_millis=export_timeout_ms,
            )
        )
        set_logger_provider(provider)
        _LOGGER_PROVIDER = provider
        _OTEL_LOGGER = provider.get_logger("audit_core")
        _EXPORT_ALL_LOGS = requested_logs
        _EXPORT_ERRORS = requested_errors
        return requested_logs, requested_errors
    except Exception as exc:  # pragma: no cover - defensive third-party boundary
        _OTEL_LOGGER = None
        _LOGGER_PROVIDER = None
        _EXPORT_ALL_LOGS = False
        _EXPORT_ERRORS = False
        if requested_logs:
            _bootstrap_warning("logs", "initialization_failed", type(exc).__name__)
        if requested_errors:
            _bootstrap_warning("errors", "initialization_failed", type(exc).__name__)
        return False, False


def _configure_metrics(settings: Settings, resource: Resource) -> Any | None:
    global _METER_PROVIDER
    if not settings.observability_metrics_enabled:
        return None
    if not _signal_endpoint_configured("metrics"):
        _bootstrap_warning("metrics", "missing_otlp_endpoint")
        return None
    try:
        export_timeout_ms = int(settings.observability_export_timeout_seconds * 1000)
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(timeout=settings.observability_export_timeout_seconds),
            export_interval_millis=settings.observability_metric_export_interval_ms,
            export_timeout_millis=export_timeout_ms,
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)
        _METER_PROVIDER = provider
        return provider.get_meter("audit_core")
    except Exception as exc:  # pragma: no cover - defensive third-party boundary
        _METER_PROVIDER = None
        _bootstrap_warning("metrics", "initialization_failed", type(exc).__name__)
        return None


def _configure_traces(app: FastAPI, settings: Settings, resource: Resource) -> Any | None:
    global _TRACER_PROVIDER
    if not settings.observability_traces_enabled:
        return None
    if not _signal_endpoint_configured("traces"):
        _bootstrap_warning("traces", "missing_otlp_endpoint")
        return None
    try:
        export_timeout_ms = int(settings.observability_export_timeout_seconds * 1000)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(
            BatchSpanProcessor(
                OTLPSpanExporter(timeout=settings.observability_export_timeout_seconds),
                max_queue_size=settings.observability_max_queue_size,
                max_export_batch_size=settings.observability_max_export_batch_size,
                schedule_delay_millis=settings.observability_batch_delay_ms,
                export_timeout_millis=export_timeout_ms,
            )
        )
        trace.set_tracer_provider(provider)
        _TRACER_PROVIDER = provider
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="/health")
        HTTPXClientInstrumentor().instrument(
            tracer_provider=provider,
            request_hook=_httpx_request_hook,
            async_request_hook=_httpx_async_request_hook,
        )
        SQLAlchemyInstrumentor().instrument(tracer_provider=provider)
        return provider.get_tracer("audit_core")
    except Exception as exc:  # pragma: no cover - defensive third-party boundary
        _TRACER_PROVIDER = None
        _bootstrap_warning("traces", "initialization_failed", type(exc).__name__)
        return None


def configure_otlp(app: FastAPI, settings: Settings) -> bool:
    """Configure only explicitly enabled OTLP capabilities, independently and fail-open."""
    resource = _resource(settings)
    logs_enabled, errors_enabled = _configure_logs(settings, resource)
    meter = _configure_metrics(settings, resource)
    tracer = _configure_traces(app, settings, resource)

    if meter is not None or tracer is not None:
        configure_otel_telemetry(
            meter if meter is not None else metrics.get_meter("audit_core.noop"),
            tracer if tracer is not None else trace.get_tracer("audit_core.noop"),
        )

    return bool(logs_enabled or errors_enabled or meter is not None or tracer is not None)
