import os
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated
from uuid import uuid4

import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection, Engine, create_engine, text

from audit_core.authorization import AuthorizationError
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

# ---------------------------------------------------------------------------
# Module-level singleton for the Security admin client.
# Set by lifespan in main.py; never None once the app is running.
# ---------------------------------------------------------------------------
_security_admin_client: SecurityAdminClient | None = None


def set_security_admin_client(client: SecurityAdminClient) -> None:
    """Called once by lifespan on startup."""
    global _security_admin_client
    _security_admin_client = client


def clear_security_admin_client() -> None:
    """Called by lifespan on shutdown."""
    global _security_admin_client
    _security_admin_client = None


def get_security_admin_client() -> SecurityAdminClient:
    if _security_admin_client is None:
        raise RuntimeError("SecurityAdminClient is not initialised — lifespan not run")
    return _security_admin_client


@dataclass(frozen=True)
class HumanAdminRequest:
    user_id: str
    bearer_token: str
    admin_context: SecurityAdminContext


@lru_cache
def _engine() -> Engine:
    """Sync SQLAlchemy engine for all plain-def route handlers.

    Uses the pooled Neon endpoint for application runtime.
    Alembic migrations use DATABASE_URL_DIRECT (non-pooled) — set in railway.toml
    pre-deploy command so it never touches the pooler.
    """
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")

    # Normalise postgres:// -> postgresql+psycopg://
    for prefix, replacement in (
        ("postgresql+asyncpg://", "postgresql+psycopg://"),
        ("postgresql://", "postgresql+psycopg://"),
        ("postgres://", "postgresql+psycopg://"),
    ):
        if database_url.startswith(prefix):
            database_url = replacement + database_url[len(prefix):]
            break

    return create_engine(
        database_url,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        pool_recycle=300,        # Neon drops idle connections
        pool_pre_ping=True,      # survives autosuspend / cold start
        connect_args={
            "prepare_threshold": None,   # psycopg v3: disable server-side prepared stmts
            "options": (
                "-c statement_timeout=8000 "
                "-c idle_in_transaction_session_timeout=10000 "
                "-c jit=off "
                "-c application_name=verigence-audit-core "
                "-c search_path=auditcore,public"
            ),
        },
    )


def get_engine() -> Engine:
    return _engine()


def get_connection() -> Iterator[Connection]:
    with get_engine().begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        yield connection


def _check_db_rtt() -> None:
    """Log DB round-trip time at startup. Warn if > 5 ms (cross-region signal)."""
    try:
        engine = _engine()
        with engine.connect() as conn:
            t0 = time.perf_counter()
            conn.execute(text("SELECT 1"))
            rtt_ms = (time.perf_counter() - t0) * 1000
        logger.info("db_rtt_ms", ms=round(rtt_ms, 2))
        if rtt_ms > 5:
            logger.warning(
                "db_rtt_high",
                ms=round(rtt_ms, 2),
                hint="likely cross-region — fix before optimising anything else",
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("db_rtt_check_failed", reason=str(exc))


def get_bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if credentials is None:
        raise SecurityTokenError("Missing Security token")
    return credentials.credentials


def _token_validator() -> SecurityTokenValidator:
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
        return _token_validator().validate_human(bearer_token)
    except SecurityTokenError as exc:
        logger.warning("human_auth_failed", reason=str(exc))
        raise


async def get_human_admin_request(
    bearer_token: Annotated[str, Depends(get_bearer_token)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
) -> HumanAdminRequest:
    """Async dependency: uses the lifespan-created SecurityAdminClient singleton.

    No per-request client creation; no TLS re-handshake; no event-loop block.
    """
    client = get_security_admin_client()
    try:
        context = await client.get_admin_context(human_bearer_token=bearer_token)
    except SecurityAdminError as exc:
        logger.warning("security_admin_context_failed", reason=str(exc))
        raise SecurityTokenError("Security administrative context is unavailable") from exc
    if context.user_id != human_principal.subject:
        raise SecurityTokenError("Security administrative USER does not match authenticated USER")
    return HumanAdminRequest(
        user_id=human_principal.subject,
        bearer_token=bearer_token,
        admin_context=context,
    )


async def require_super_admin_request(
    request: Annotated[HumanAdminRequest, Depends(get_human_admin_request)],
) -> HumanAdminRequest:
    if not request.admin_context.is_super_admin:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    return request
