from dataclasses import dataclass

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from audit_core.authorization import AuthorizationError
from audit_core.observability import CORRELATION_HEADER, get_correlation_id
from audit_core.security import SecurityTokenError

logger = structlog.get_logger(__name__)


@dataclass
class AuditCoreError(RuntimeError):
    """Stable public API error.

    Exceptions must remain mutable because Python/contextlib assigns traceback state
    while an exception crosses FastAPI yield dependencies. A frozen dataclass turns
    that normal traceback propagation into FrozenInstanceError and masks the original
    problem response.
    """

    error_code: str
    status_code: int
    title: str
    detail: str

    def __str__(self) -> str:
        return self.title


class ValidationError(AuditCoreError):
    def __init__(self, *, detail: str) -> None:
        super().__init__("VAC-VAL-001", 400, "Validation failed", detail)


class BusinessValidationError(AuditCoreError):
    def __init__(self, *, detail: str) -> None:
        super().__init__("VAC-VAL-002", 422, "Business validation failed", detail)


class NotFoundError(AuditCoreError):
    def __init__(self, *, error_code: str, title: str, detail: str) -> None:
        super().__init__(error_code, 404, title, detail)


class ConflictError(AuditCoreError):
    def __init__(self, *, error_code: str, title: str, detail: str) -> None:
        super().__init__(error_code, 409, title, detail)


class DependencyUnavailableError(AuditCoreError):
    def __init__(self, *, detail: str) -> None:
        super().__init__(
            "VAC-SYS-002",
            503,
            "Service temporarily unavailable",
            detail,
        )


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
        # Exception messages can contain bearer tokens, passwords, document values or other
        # sensitive input. Log only the safe exception classification and correlation context.
        logger.error(
            "system_error",
            exc_type=type(exc).__name__,
        )
        return _problem(
            request,
            error_code="VAC-SYS-001",
            status_code=500,
            title="Internal error",
            detail="An unexpected Audit Core error occurred.",
        )
