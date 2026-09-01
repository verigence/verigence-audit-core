import time
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response
from opentelemetry import trace

from audit_core.otel import attach_business_context
from audit_core.telemetry import record_metric, trace_span

CORRELATION_HEADER = "X-Correlation-ID"
TRACE_HEADER = "X-Trace-ID"
logger = structlog.get_logger(__name__)

_BUSINESS_PATH_KEYS = {
    "tenant_id": "tenant_id",
    "tenantId": "tenant_id",
    "project_id": "project_id",
    "projectId": "project_id",
    "journey_id": "journey_id",
    "journeyId": "journey_id",
    "evidence_id": "evidence_id",
    "evidenceId": "evidence_id",
    "document_id": "document_id",
    "documentId": "document_id",
}


def get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or request.headers.get(
        CORRELATION_HEADER,
        "unknown",
    )


def current_correlation_id() -> str | None:
    value = structlog.contextvars.get_contextvars().get("correlation_id")
    return str(value) if value else None


def request_business_context(request: Request) -> dict[str, str]:
    context: dict[str, str] = {}
    for source_key, target_key in _BUSINESS_PATH_KEYS.items():
        value = request.path_params.get(source_key)
        if value is not None:
            context[target_key] = str(value)
    return context


def install_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_and_request_metrics(request: Request, call_next) -> Response:
        structlog.contextvars.clear_contextvars()
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid4())
        incoming_trace_id = request.headers.get(TRACE_HEADER)
        request.state.correlation_id = correlation_id
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        status_code = 500
        started = time.perf_counter()
        with trace_span(
            "audit_core.http_request",
            correlation_id=correlation_id,
            trace_id=incoming_trace_id,
            attributes={"method": request.method, "route": request.url.path},
        ) as (trace_id, span_id):
            request.state.trace_id = trace_id
            request.state.span_id = span_id
            active_span = trace.get_current_span()
            if active_span.is_recording():
                active_span.set_attribute("verigence.correlation_id", correlation_id)
            try:
                response = await call_next(request)
                status_code = response.status_code
                response.headers[CORRELATION_HEADER] = correlation_id
                response.headers[TRACE_HEADER] = trace_id
                return response
            except Exception:
                # Exception messages can contain request/document values; classification is enough.
                logger.error(
                    "unhandled_exception",
                    method=request.method,
                    path=request.url.path,
                )
                raise
            finally:
                business_context = request_business_context(request)
                if business_context:
                    attach_business_context(business_context)
                duration_ms = (time.perf_counter() - started) * 1000.0
                status_class = f"{status_code // 100}xx"
                labels = {
                    "method": request.method,
                    "status_class": status_class,
                }
                record_metric("audit_core.http.requests", labels=labels)
                record_metric(
                    "audit_core.http.duration_ms",
                    duration_ms,
                    kind="histogram",
                    labels=labels,
                )
                if status_code >= 400:
                    record_metric("audit_core.http.errors", labels=labels)
                    logger.warning(
                        "http_request_failed",
                        method=request.method,
                        path=request.url.path,
                        status_code=status_code,
                        duration_ms=round(duration_ms, 2),
                    )
                if status_code < 400:
                    from audit_core.config import load_settings as _load_settings
                    _threshold = _load_settings().slow_request_threshold_ms
                    if duration_ms > _threshold:
                        logger.warning(
                            "http_request_slow",
                            method=request.method,
                            path=request.url.path,
                            status_code=status_code,
                            duration_ms=round(duration_ms, 2),
                            threshold_ms=_threshold,
                        )


def log_dependency(
    *,
    correlation_id: str,
    dependency: str,
    operation: str,
    result: str,
    duration_ms: float | None = None,
) -> None:
    labels = {
        "dependency": dependency,
        "operation": operation,
        "result": result,
    }
    record_metric("audit_core.dependency.calls", labels=labels)
    if duration_ms is not None:
        record_metric(
            "audit_core.dependency.duration_ms",
            duration_ms,
            kind="histogram",
            labels=labels,
        )
    if result.upper() not in {"SUCCESS", "OK"}:
        record_metric("audit_core.dependency.errors", labels=labels)
        logger.warning(
            "dependency_call_failed",
            correlation_id=correlation_id,
            dependency=dependency,
            operation=operation,
            result=result,
            duration_ms=duration_ms,
        )
