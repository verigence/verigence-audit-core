import time
from uuid import uuid4

import structlog
from fastapi import FastAPI, Request, Response

from audit_core.telemetry import record_metric, trace_span

CORRELATION_HEADER = "X-Correlation-ID"
TRACE_HEADER = "X-Trace-ID"
logger = structlog.get_logger(__name__)


def get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or request.headers.get(
        CORRELATION_HEADER,
        "unknown",
    )


def install_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_and_request_log(request: Request, call_next) -> Response:
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
            try:
                response = await call_next(request)
                status_code = response.status_code
                response.headers[CORRELATION_HEADER] = correlation_id
                response.headers[TRACE_HEADER] = trace_id
                return response
            except Exception:
                logger.exception(
                    "unhandled_exception",
                    method=request.method,
                    path=request.url.path,
                )
                raise
            finally:
                duration_ms = (time.perf_counter() - started) * 1000.0
                status_class = f"{status_code // 100}xx"
                record_metric(
                    "audit_core.http.requests",
                    labels={
                        "method": request.method,
                        "status_class": status_class,
                    },
                )
                record_metric(
                    "audit_core.http.duration_ms",
                    duration_ms,
                    kind="histogram",
                    labels={
                        "method": request.method,
                        "status_class": status_class,
                    },
                )
                if status_code >= 400:
                    record_metric(
                        "audit_core.http.errors",
                        labels={
                            "method": request.method,
                            "status_class": status_class,
                        },
                    )
                logger.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    status_code=status_code,
                    duration_ms=round(duration_ms, 2),
                    trace_id=trace_id,
                    span_id=span_id,
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
    logger.info(
        "dependency_call",
        correlation_id=correlation_id,
        dependency=dependency,
        operation=operation,
        result=result,
        duration_ms=duration_ms,
    )
