from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from audit_core.security import SecurityKeysUnavailableError, SecurityTokenError
from audit_core.verigence_attendance.api import router
from audit_core.verigence_attendance.db import attendance_engine
from audit_core.verigence_attendance.errors import AttendanceRuleError
from audit_core.verigence_attendance.security import (
    AttendanceAuthorizationError,
    AttendanceDependencyError,
)
from audit_core.verigence_attendance.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Verigence Employee Attendance",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
        )

    @app.exception_handler(SecurityTokenError)
    async def token_error(_: Request, exc: SecurityTokenError) -> JSONResponse:
        return JSONResponse(status_code=401, content={"code": "AUTHENTICATION_FAILED", "detail": str(exc)})

    @app.exception_handler(SecurityKeysUnavailableError)
    async def keys_error(_: Request, exc: SecurityKeysUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"code": "SECURITY_UNAVAILABLE", "detail": str(exc)})

    @app.exception_handler(AttendanceAuthorizationError)
    async def authz_error(_: Request, exc: AttendanceAuthorizationError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"code": "PERMISSION_DENIED", "detail": str(exc)})

    @app.exception_handler(AttendanceDependencyError)
    async def dependency_error(_: Request, exc: AttendanceDependencyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"code": "DEPENDENCY_UNAVAILABLE", "detail": str(exc)})

    @app.exception_handler(AttendanceRuleError)
    async def rule_error(_: Request, exc: AttendanceRuleError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"code": exc.code, "detail": exc.detail})

    app.include_router(router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "employee-attendance"}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        with attendance_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready", "service": "employee-attendance"}

    return app


app = create_app()
