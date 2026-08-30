import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock
from typing import Annotated

import structlog
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection, Engine, create_engine

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

# These GETs can authenticate locally without an extra /platform/admin-context
# call. Cross-Tenant Project Administration (`GET /v1/projects`) is deliberately
# excluded: it requires the real SuperAdmin attestation before its one SQL directory
# read. Reference/master reads remain authenticated-human reads per the runtime design.
_LIGHTWEIGHT_AUTHENTICATED_READS = (
    re.compile(r"^/v1/project-reference-data/?$"),
    re.compile(r"^/v1/tenants/[^/]+/project-masters/?$"),
    re.compile(r"^/v1/tenants/[^/]+/mahindra-masters/imports/?$"),
    re.compile(r"^/v1/tenants/[^/]+/mahindra-masters/imports/[^/]+/validation-report/?$"),
    re.compile(r"^/v1/tenants/[^/]+/project-masters/[^/]+/[^/]+/versions/?$"),
    re.compile(r"^/v1/tenants/[^/]+/project-masters/DI/[^/]+/template/?$"),
)

# Administrative reads/writes that really do need SuperAdmin keep that policy,
# but page bursts must not call Security admin-context over and over. Cache only
# the Security-owned attestation in Audit Core process memory for a short window.
# Human JWT validation itself still runs on every request.
_ADMIN_CONTEXT_TTL_SECONDS = 30.0
_admin_context_cache: dict[str, tuple[float, SecurityAdminContext]] = {}
_admin_context_cache_lock = Lock()


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

    # Runtime API connections always operate as the constrained database role. Set
    # that role when a physical PostgreSQL connection is created instead of issuing
    # SET LOCAL ROLE on every HTTP request. Railway/PostgreSQL can terminate pooled
    # connections during a restart/failover, so pre-ping each checkout and let
    # SQLAlchemy discard a dead connection before a user request receives it.
    engine_options: dict[str, object] = {"pool_pre_ping": True}
    if _postgresql_url(database_url):
        engine_options.update(
            pool_timeout=5,
            pool_recycle=600,
            pool_use_lifo=True,
            connect_args={
                "connect_timeout": 5,
                "keepalives": 1,
                "keepalives_idle": 30,
                "keepalives_interval": 10,
                "keepalives_count": 3,
                "options": "-c statement_timeout=10000 -c role=audit_core_runtime",
            },
        )
    return create_engine(database_url, **engine_options)


def get_engine() -> Engine:
    return _engine()


def get_connection() -> Iterator[Connection]:
    with get_engine().begin() as connection:
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


def _cached_admin_context(user_id: str) -> SecurityAdminContext | None:
    now = time.monotonic()
    with _admin_context_cache_lock:
        cached = _admin_context_cache.get(user_id)
        if cached is None:
            return None
        expires_at, context = cached
        if expires_at <= now:
            _admin_context_cache.pop(user_id, None)
            return None
        return context


def _remember_admin_context(context: SecurityAdminContext) -> None:
    with _admin_context_cache_lock:
        _admin_context_cache[context.user_id] = (
            time.monotonic() + _ADMIN_CONTEXT_TTL_SECONDS,
            context,
        )


def _security_admin_context(
    *,
    bearer_token: str,
    human_principal: HumanPrincipal,
) -> HumanAdminRequest:
    cached = _cached_admin_context(human_principal.subject)
    if cached is not None:
        return HumanAdminRequest(
            user_id=human_principal.subject,
            bearer_token=bearer_token,
            admin_context=cached,
        )

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
    _remember_admin_context(context)
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


def _has_tenant_admin_scope(context: SecurityAdminContext, tenant_id: str) -> bool:
    return any(
        scope.role_key == "TenantAdmin"
        and scope.scope_type == "TENANT"
        and scope.scope_id == tenant_id
        for scope in context.admin_scopes
    )


def require_project_admin_request(
    tenant_id: str,
    http_request: Request,
    bearer_token: Annotated[str, Depends(get_bearer_token)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
) -> HumanAdminRequest:
    """Authorize UC02 administration for SuperAdmin or matching TenantAdmin.

    Lightweight authenticated GETs retain their existing fast path. Mutations and
    non-lightweight reads use Security's live/cached admin-context; authority is
    never taken from the human JWT itself.
    """
    if _is_lightweight_authenticated_read(http_request):
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
    if request.admin_context.is_super_admin or _has_tenant_admin_scope(
        request.admin_context, tenant_id
    ):
        return request
    raise AuthorizationError(
        error_code="VAC-AUTH-002",
        status_code=403,
        title="Permission denied",
    )
