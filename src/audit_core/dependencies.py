import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection, Engine, create_engine, text

from audit_core.authorization import AuthorizationError
from audit_core.errors import DependencyUnavailableError
from audit_core.otel import attach_trusted_user_id
from audit_core.security import (
    HumanPrincipal,
    Principal,
    SecurityTokenError,
    SecurityTokenValidator,
)
from audit_core.security_integration import (
    SecurityAdminClient,
    SecurityAdminContext,
    SecurityAdminError,
)

logger = structlog.get_logger(__name__)

_bearer = HTTPBearer(auto_error=False)

# These GETs expose read-only reference/master data. They require a valid human
# token, but do not require a live Security SuperAdmin round-trip merely because
# they are surfaced from Project Administration.
_LIGHTWEIGHT_AUTHENTICATED_READS = (
    re.compile(r"^/v1/project-reference-data/?$"),
    re.compile(r"^/v1/tenants/[^/]+/project-masters/?$"),
    re.compile(r"^/v1/tenants/[^/]+/project-masters/[^/]+/[^/]+/versions/?$"),
    re.compile(r"^/v1/tenants/[^/]+/project-masters/DI/[^/]+/template/?$"),
)


@dataclass(frozen=True)
class HumanAdminRequest:
    user_id: str
    bearer_token: str
    admin_context: SecurityAdminContext


def _postgresql_url(database_url: str) -> bool:
    normalized = database_url.lower()
    return normalized.startswith(("postgresql://", "postgres://", "postgresql+"))


@lru_cache
def _engine() -> Engine:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    # Keep DB resilience central and small: one pool, stale-connection detection,
    # and bounded waits. API handlers do not implement their own retry loops.
    engine_options: dict[str, object] = {"pool_pre_ping": True}
    if _postgresql_url(database_url):
        engine_options.update(
            pool_timeout=5,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=10000",
            },
        )
    return create_engine(database_url, **engine_options)


def get_engine() -> Engine:
    return _engine()


def get_connection() -> Iterator[Connection]:
    with get_engine().begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        yield connection


def get_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if credentials is None:
        raise SecurityTokenError("Missing Security token")
    return credentials.credentials


@lru_cache
def _token_validator() -> SecurityTokenValidator:
    """Reuse one validator/JWKS client per Audit Core process.

    Every request is still cryptographically validated. Reusing the validator lets
    the JWKS client keep its server-side signing-key cache instead of fetching the
    same public keys for each API call.
    """

    jwks_url = os.environ.get("SECURITY_JWKS_URL", "").strip()
    issuer = os.environ.get("SECURITY_ISSUER", "").strip()
    audience = os.environ.get("SECURITY_AUDIENCE", "").strip()
    if not jwks_url or not issuer or not audience:
        raise RuntimeError("Security JWT verification is not configured")
    return SecurityTokenValidator(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=audience,
    )


def get_principal(
    bearer_token: Annotated[str, Depends(get_bearer_token)],
) -> Principal:
    try:
        return _token_validator().validate(bearer_token)
    except SecurityTokenError as exc:
        logger.warning("auth_failed", reason=str(exc))
        raise


def get_human_principal(
    bearer_token: Annotated[str, Depends(get_bearer_token)],
) -> HumanPrincipal:
    try:
        principal = _token_validator().validate_human(bearer_token)
        attach_trusted_user_id(principal.subject)
        return principal
    except SecurityTokenError as exc:
        logger.warning("human_auth_failed", reason=str(exc))
        raise


def _security_admin_context(
    *,
    bearer_token: str,
    human_principal: HumanPrincipal,
) -> HumanAdminRequest:
    security_base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    if not security_base_url:
        logger.error("security_admin_context_failed", reason="security_base_url_missing")
        raise DependencyUnavailableError(
            detail="Project administration is temporarily unavailable. Please try again."
        )

    last_error: SecurityAdminError | None = None
    for attempt in range(2):
        try:
            with SecurityAdminClient(base_url=security_base_url) as client:
                context = client.get_admin_context(human_bearer_token=bearer_token)
            break
        except SecurityAdminError as exc:
            last_error = exc
            logger.warning(
                "security_admin_context_failed",
                reason=str(exc),
                downstream_http_status=exc.http_status,
                attempt=attempt + 1,
            )
            if attempt == 0:
                time.sleep(0.15)
    else:
        assert last_error is not None
        raise DependencyUnavailableError(
            detail="Project administration is temporarily unavailable. Please try again."
        ) from last_error

    if context.user_id != human_principal.subject:
        raise SecurityTokenError("Security administrative USER does not match authenticated USER")
    return HumanAdminRequest(
        user_id=human_principal.subject,
        bearer_token=bearer_token,
        admin_context=context,
    )


def get_human_admin_request(
    bearer_token: Annotated[str, Depends(get_bearer_token)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
) -> HumanAdminRequest:
    return _security_admin_context(
        bearer_token=bearer_token,
        human_principal=human_principal,
    )


def _is_lightweight_authenticated_read(request: Request) -> bool:
    if request.method.upper() != "GET":
        return False
    path = request.url.path
    return any(pattern.fullmatch(path) for pattern in _LIGHTWEIGHT_AUTHENTICATED_READS)


def require_super_admin_request(
    http_request: Request,
    bearer_token: Annotated[str, Depends(get_bearer_token)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
) -> HumanAdminRequest:
    if _is_lightweight_authenticated_read(http_request):
        # The route signature historically expects HumanAdminRequest so preserve the
        # shape, while explicitly marking that no administrative authority was used.
        return HumanAdminRequest(
            user_id=human_principal.subject,
            bearer_token=bearer_token,
            admin_context=SecurityAdminContext(
                user_id=human_principal.subject,
                is_super_admin=False,
                admin_scopes=(),
            ),
        )

    request = _security_admin_context(
        bearer_token=bearer_token,
        human_principal=human_principal,
    )
    if not request.admin_context.is_super_admin:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    return request
