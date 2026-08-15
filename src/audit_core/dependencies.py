import os
from collections.abc import Iterator
from functools import lru_cache
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import Connection, Engine, create_engine

from audit_core.security import Principal, SecurityTokenError, SecurityTokenValidator

_bearer = HTTPBearer(auto_error=False)


@lru_cache
def _engine() -> Engine:
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is required")
    return create_engine(database_url)


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


def get_principal(
    bearer_token: Annotated[str, Depends(get_bearer_token)],
) -> Principal:
    jwks_url = os.environ.get("SECURITY_JWKS_URL", "").strip()
    issuer = os.environ.get("SECURITY_ISSUER", "").strip()
    audience = os.environ.get("SECURITY_AUDIENCE", "").strip()
    if not jwks_url or not issuer or not audience:
        raise RuntimeError("Security JWT verification is not configured")

    return SecurityTokenValidator(
        jwks_url=jwks_url,
        issuer=issuer,
        audience=audience,
    ).validate(bearer_token)
