import logging
from uuid import uuid4

from fastapi import FastAPI, Request, Response

CORRELATION_HEADER = "X-Correlation-ID"
logger = logging.getLogger("audit_core")


def get_correlation_id(request: Request) -> str:
    return getattr(request.state, "correlation_id", None) or request.headers.get(
        CORRELATION_HEADER,
        "unknown",
    )


def install_observability(app: FastAPI) -> None:
    @app.middleware("http")
    async def correlation_and_request_log(request: Request, call_next) -> Response:
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid4())
        request.state.correlation_id = correlation_id
        response = await call_next(request)
        response.headers[CORRELATION_HEADER] = correlation_id
        logger.info(
            "request_complete",
            extra={
                "correlation_id": correlation_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
            },
        )
        return response


def log_dependency(*, correlation_id: str, dependency: str, operation: str, result: str) -> None:
    logger.info(
        "dependency_call",
        extra={
            "correlation_id": correlation_id,
            "dependency": dependency,
            "operation": operation,
            "result": result,
        },
    )
