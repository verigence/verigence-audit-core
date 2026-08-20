from dataclasses import dataclass

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from audit_core.authorization import AuthorizationError
from audit_core.observability import CORRELATION_HEADER, get_correlation_id
from audit_core.security import SecurityTokenError

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AuditCoreError(RuntimeError):
    error_code: str
    status_code: int
    title: str
    detail: str

    def __str__(self) -> str:
        return self.title


class NotFoundError(AuditCoreError):
    def __init__(self, *, error_code: str, title: str, detail: str) -> None:
        super().__init__(error_code, 404, title, detail)


class ConflictError(AuditCoreError):
    def __init__(self, *, error_code: str, title: str, detail: str) -> None:
        super().__init__(error_code, 409, title, detail)


def _problem(
    request: Request,
    *,
    error_code: str,
    status_code: int,
    title: str,
    detail: str,
) -> JSONResponse:
    correlation_id = get_correlation_id(request)
    logger.warning(
        "api_error",
        correlation_id=correlation_id,
        error_code=error_code,
        status_code=status_code,
        title=title,
    )
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers={CORRELATION_HEADER: correlation_id},
        content={
            "type": f"urn:verigence:audit-core:error:{error_code}",
            "title": title,
            "status": status_code,
            "detail": detail,
            "errorCode": error_code,
            "correlationId": correlation_id,
        },
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(
            request,
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail="One or more request fields are invalid.",
        )

    @app.exception_handler(SecurityTokenError)
    async def authentication_error(request: Request, exc: SecurityTokenError) -> JSONResponse:
        return _problem(
            request,
            error_code="VAC-AUTH-001",
            status_code=401,
            title="Authentication required",
            detail="A valid Security access token is required.",
        )

    @app.exception_handler(AuthorizationError)
    async def authorization_error(request: Request, exc: AuthorizationError) -> JSONResponse:
        return _problem(
            request,
            error_code=exc.error_code,
            status_code=exc.status_code,
            title=exc.title,
            detail=exc.title,
        )

    @app.exception_handler(AuditCoreError)
    async def audit_core_error(request: Request, exc: AuditCoreError) -> JSONResponse:
        return _problem(
            request,
            error_code=exc.error_code,
            status_code=exc.status_code,
            title=exc.title,
            detail=exc.detail,
        )

    @app.exception_handler(Exception)
    async def system_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "system_error",
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
        )
        return _problem(
            request,
            error_code="VAC-SYS-001",
            status_code=500,
            title="Internal error",
            detail="An unexpected Audit Core error occurred.",
        )
