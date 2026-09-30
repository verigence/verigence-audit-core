from dataclasses import dataclass

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from audit_core import security_integration
from audit_core.authorization import AuthorizationError
from audit_core.di_capture_v2_client import DiCaptureV2Error
from audit_core.di_client import DiClientError
from audit_core.logging_config import exception_summary, redact_text
from audit_core.observability import (
    CORRELATION_HEADER,
    get_correlation_id,
    request_business_context,
)
from audit_core.otel import attach_business_context
from audit_core.security import SecurityKeysUnavailableError, SecurityTokenError
from audit_core.security_integration import (
    SecurityAdminError,
    SecurityTokenUnavailableError,
)

logger = structlog.get_logger(__name__)

# Every problem response and api_error log carries one of these, so a reader (and alerting) can
# tell "the user must change something" from "the platform is broken".
#   VALIDATION  request shape/field problems              400/422   log INFO
#   BUSINESS    domain rule / not found / conflict        404/409/422...  log INFO
#   SECURITY    authentication / permission / scope       401/403   log WARNING
#   DEPENDENCY  DI / Security / storage / rule engine     502/503/504  log ERROR
#   TECHNICAL   unexpected Audit Core failure             500       log ERROR (+ stack)
_LOG_LEVEL_BY_CATEGORY = {
    "VALIDATION": "info",
    "BUSINESS": "info",
    "SECURITY": "warning",
    "DEPENDENCY": "error",
    "TECHNICAL": "error",
}

# Generic codes for plain HTTPException / framework errors that carry no catalogue code.
_GENERIC_BY_STATUS: dict[int, tuple[str, str]] = {
    400: ("VAC-VAL-001", "Validation failed"),
    401: ("VAC-AUTH-001", "Authentication required"),
    403: ("VAC-AUTH-002", "Permission denied"),
    404: ("VAC-NF-000", "Not found"),
    405: ("VAC-VAL-010", "Unsupported request"),
    409: ("VAC-CONFLICT-000", "Conflict"),
    413: ("VAC-VAL-010", "Unsupported request"),
    415: ("VAC-VAL-010", "Unsupported request"),
    422: ("VAC-VAL-002", "Business validation failed"),
    429: ("VAC-SYS-003", "Too many requests"),
    502: ("VAC-SYS-002", "Dependency unavailable"),
    503: ("VAC-SYS-002", "Service temporarily unavailable"),
    504: ("VAC-SYS-002", "Service temporarily unavailable"),
}


def error_category(status_code: int, error_code: str = "") -> str:
    if status_code in (401, 403):
        return "SECURITY"
    if status_code in (502, 503, 504):
        return "DEPENDENCY"
    if status_code >= 500:
        return "TECHNICAL"
    if status_code == 400 or error_code.startswith("VAC-VAL-001"):
        return "VALIDATION"
    return "BUSINESS"


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


def security_admin_failure(exc: SecurityAdminError, *, action: str) -> AuditCoreError:
    """Translate a Security administrative failure by what Security answered: the caller's
    authentication/permission (401/403) or a business refusal (4xx) is not an outage."""
    status = exc.http_status
    if status == 401:
        return AuditCoreError("VAC-AUTH-001", 401, "Authentication required",
                              "Authentication is required for this administrative operation.")
    if status == 403:
        return AuditCoreError("VAC-AUTH-002", 403, "Permission denied",
                              f"Security did not allow you to {action}.")
    if status == 404:
        return NotFoundError(error_code="VAC-NF-000", title="Not found",
                             detail=f"Security could not find what is needed to {action}.")
    if status == 409:
        return ConflictError(error_code="VAC-CONFLICT-000", title="Conflict",
                             detail=f"Security reported a conflict; could not {action}. Refresh and try again.")
    if status is not None and 400 <= status < 500:
        return BusinessValidationError(detail=f"Security rejected the request to {action}.")
    return DependencyUnavailableError(detail=f"Could not {action}: Security is temporarily unavailable. Please try again.")


def _problem(
    request: Request,
    *,
    error_code: str,
    status_code: int,
    title: str,
    detail: str,
    exc: BaseException | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    correlation_id = get_correlation_id(request)
    category = error_category(status_code, error_code)
    retryable = category == "DEPENDENCY" or status_code == 429
    business_context = request_business_context(request)
    if business_context:
        attach_business_context(business_context)
    fields: dict[str, object] = {
        "correlation_id": correlation_id,
        "error_code": error_code,
        "error_category": category,
        "retryable": retryable,
        "status_code": status_code,
        "method": request.method,
        "route": _route_template(request),
        # The client sees the full detail; the log keeps a redacted copy (details can echo input).
        "detail": redact_text(detail),
        **business_context,
    }
    if exc is not None and category in ("TECHNICAL", "DEPENDENCY"):
        fields.update(exception_summary(exc))
    getattr(logger, _LOG_LEVEL_BY_CATEGORY[category])("api_error", **fields)
    request.state.problem_logged = True
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers={**(headers or {}), CORRELATION_HEADER: correlation_id},
        content={
            "type": f"urn:verigence:audit-core:error:{error_code}",
            "title": title,
            "status": status_code,
            "detail": detail,
            "errorCode": error_code,
            "errorCategory": category,
            "retryable": retryable,
            "correlationId": correlation_id,
        },
    )


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", "") or request.url.path)


def system_error_response(request: Request, exc: BaseException) -> JSONResponse:
    """500 for anything unexpected. Exception messages can contain bearer tokens, passwords or
    document values, so the log carries the redacted summary (type, frames, SQLSTATE) only."""
    return _problem(
        request,
        error_code="VAC-SYS-001",
        status_code=500,
        title="Internal error",
        detail="An unexpected Audit Core error occurred.",
        exc=exc,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Field locations and messages (never the submitted values) tell the caller what to fix.
        field_errors = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg')} ({error.get('type')})"
            for error in exc.errors()
        )
        detail = "One or more request fields are invalid."
        if field_errors:
            detail = f"{detail} {field_errors}"
        return _problem(
            request,
            error_code="VAC-VAL-001",
            status_code=400,
            title="Validation failed",
            detail=detail,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        # Plain HTTPException (and Starlette's own 404/405) get the same problem shape, a
        # catalogue code by status and the correlation id.
        error_code, title = _GENERIC_BY_STATUS.get(
            exc.status_code,
            ("VAC-SYS-001", "Internal error") if exc.status_code >= 500 else ("VAC-VAL-002", "Request rejected"),
        )
        detail = exc.detail if isinstance(exc.detail, str) else title
        return _problem(
            request,
            error_code=error_code,
            status_code=exc.status_code,
            title=title,
            detail=detail,
            headers=dict(exc.headers or {}),
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

    @app.exception_handler(SecurityKeysUnavailableError)
    async def signing_keys_unavailable(request: Request, exc: SecurityKeysUnavailableError) -> JSONResponse:
        return _problem(
            request,
            error_code="VAC-SYS-002",
            status_code=503,
            title="Service temporarily unavailable",
            detail="Sign-in verification is temporarily unavailable. Please try again.",
            exc=exc,
        )

    @app.exception_handler(SecurityAdminError)
    async def security_admin_error(request: Request, exc: SecurityAdminError) -> JSONResponse:
        failure = security_admin_failure(exc, action="complete this administrative action")
        return _problem(
            request,
            error_code=failure.error_code,
            status_code=failure.status_code,
            title=failure.title,
            detail=failure.detail,
            exc=exc,
        )

    @app.exception_handler(SecurityTokenUnavailableError)
    async def service_token_unavailable(
        request: Request, exc: SecurityTokenUnavailableError
    ) -> JSONResponse:
        return _problem(
            request,
            error_code="VAC-SYS-002",
            status_code=503,
            title="Service temporarily unavailable",
            detail="Document processing authorization is temporarily unavailable. Please try again.",
            exc=exc,
        )

    @app.exception_handler(security_integration.SecurityTokenError)
    async def service_token_denied(
        request: Request, exc: security_integration.SecurityTokenError
    ) -> JSONResponse:
        # Security refused Audit Core's own service credential: a platform configuration fault,
        # not the caller's authentication.
        return _problem(
            request,
            error_code="VAC-SYS-002",
            status_code=503,
            title="Service temporarily unavailable",
            detail="Audit Core could not obtain service authorization. Please try again later.",
            exc=exc,
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
            exc=exc,
        )

    @app.exception_handler(DiClientError)
    async def di_error(request: Request, exc: DiClientError) -> JSONResponse:
        if exc.retryable or exc.status_code >= 500:
            return _problem(
                request,
                error_code="VAC-DI-001",
                status_code=503,
                title="Document intelligence unavailable",
                detail="Document processing is temporarily unavailable. Please try again.",
                exc=exc,
            )
        return _problem(
            request,
            error_code="VAC-DI-002",
            status_code=422,
            title="Document rejected",
            detail="The document service rejected this request.",
        )

    @app.exception_handler(DiCaptureV2Error)
    async def di_capture_error(request: Request, exc: DiCaptureV2Error) -> JSONResponse:
        if exc.status_code >= 500 or exc.status_code in (408, 425, 429):
            return _problem(
                request,
                error_code="VAC-DI-001",
                status_code=503,
                title="Document intelligence unavailable",
                detail="Document processing is temporarily unavailable. Please try again.",
                exc=exc,
            )
        return _problem(
            request,
            error_code="VAC-DI-002",
            status_code=422,
            title="Document rejected",
            detail="The document service rejected this request.",
        )

    @app.exception_handler(LookupError)
    async def lookup_error(request: Request, exc: LookupError) -> JSONResponse:
        # Services raise LookupError for "not found / no longer available" records. KeyError and
        # IndexError are LookupErrors too, but those are programming errors.
        if isinstance(exc, (KeyError, IndexError)):
            return system_error_response(request, exc)
        return _problem(
            request,
            error_code="VAC-NF-000",
            status_code=404,
            title="Not found",
            detail=str(exc)[:200] or "The requested record was not found.",
        )

    @app.exception_handler(IntegrityError)
    async def integrity_error(request: Request, exc: IntegrityError) -> JSONResponse:
        pgcode = str(getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", "") or "")
        if pgcode in ("23505", "23503"):
            # Unique / foreign-key violation: two requests raced, or the record was changed.
            return _problem(
                request,
                error_code="VAC-CONFLICT-000",
                status_code=409,
                title="Conflict",
                detail="This change conflicts with an existing or related record. Refresh and try again.",
            )
        return system_error_response(request, exc)

    @app.exception_handler(Exception)
    async def system_error(request: Request, exc: Exception) -> JSONResponse:
        return system_error_response(request, exc)
