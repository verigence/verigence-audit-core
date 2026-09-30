import re
import time
from contextvars import ContextVar
from functools import lru_cache
from typing import Any
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


# Accept a caller's id only if it is a plain token: it is echoed in headers and every log line.
_CORRELATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


# Where a request's time went: filled by the DB and outbound-HTTP hooks, reported on slow or
# failed requests. The dict is shared with the endpoint's task/thread (context copies keep the
# same object).
_request_timings: ContextVar[dict[str, Any] | None] = ContextVar("audit_core_request_timings", default=None)


def add_timing(key: str, milliseconds: float | None = None, *, count_key: str | None = None) -> None:
    timings = _request_timings.get()
    if timings is None:
        return
    if milliseconds is not None:
        timings[key] = round(timings.get(key, 0.0) + milliseconds, 1)
    if count_key:
        timings[count_key] = timings.get(count_key, 0) + 1


def accepted_correlation_id(value: str | None) -> str:
    return value if value and _CORRELATION_ID.match(value) else str(uuid4())


@lru_cache(maxsize=1)
def _slow_request_threshold_ms() -> float:
    from audit_core.config import load_settings

    return float(load_settings().slow_request_threshold_ms)


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
        correlation_id = accepted_correlation_id(request.headers.get(CORRELATION_HEADER))
        incoming_trace_id = request.headers.get(TRACE_HEADER)
        request.state.correlation_id = correlation_id
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        timings: dict[str, Any] = {}
        timings_token = _request_timings.set(timings)
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
                try:
                    response = await call_next(request)
                except Exception as exc:  # noqa: BLE001 - answered as a logged 500 below
                    # Answer here: re-raising would let the server print the raw traceback, whose
                    # messages can contain request/document values. The handler logs a redacted
                    # summary once.
                    from audit_core.errors import system_error_response

                    response = system_error_response(request, exc)
                status_code = response.status_code
                response.headers[CORRELATION_HEADER] = correlation_id
                response.headers[TRACE_HEADER] = trace_id
                return response
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
                route = getattr(request.scope.get("route"), "path", None) or request.url.path
                if status_code >= 400:
                    record_metric("audit_core.http.errors", labels=labels)
                    # One line per failed request: the error handler already logged api_error
                    # (code, category, detail); this adds the timing to it.
                    getattr(logger, "error" if status_code >= 500 else "info")(
                        "http_request_failed",
                        method=request.method,
                        route=route,
                        status_code=status_code,
                        duration_ms=round(duration_ms, 2),
                        **timings,
                        **business_context,
                    )
                elif duration_ms > _slow_request_threshold_ms():
                    logger.warning(
                        "http_request_slow",
                        method=request.method,
                        route=route,
                        status_code=status_code,
                        duration_ms=round(duration_ms, 2),
                        threshold_ms=_slow_request_threshold_ms(),
                        **timings,
                        **business_context,
                    )
                _request_timings.reset(timings_token)


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
