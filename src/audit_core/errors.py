from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from audit_core.authorization import AuthorizationError
from audit_core.security import SecurityTokenError


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
    correlation_id = request.headers.get("X-Correlation-ID", "unknown")
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
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
        return _problem(
            request,
            error_code="VAC-SYS-001",
            status_code=500,
            title="Internal error",
            detail="An unexpected Audit Core error occurred.",
        )
