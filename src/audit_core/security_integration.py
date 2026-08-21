from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Self

import httpx
import structlog

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"

logger = structlog.get_logger(__name__)


class SecurityTokenError(RuntimeError):
    """Security did not issue the requested downstream access token."""


class SecurityAdminError(RuntimeError):
    """Security could not provide trustworthy human administrative context."""


@dataclass(frozen=True)
class SecurityAdminScope:
    role_key: str
    scope_type: str
    scope_id: str | None


@dataclass(frozen=True)
class SecurityAdminContext:
    user_id: str
    is_super_admin: bool
    admin_scopes: tuple[SecurityAdminScope, ...]


class SecurityAdminClient:
    """Human-admin client that always forwards the initiating Security bearer token."""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Security base URL is required")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def get_admin_context(self, *, human_bearer_token: str) -> SecurityAdminContext:
        payload = self._request_json(
            "GET",
            "/security/v1/platform/admin-context",
            human_bearer_token=human_bearer_token,
        )
        user_id = payload.get("userId")
        is_super_admin = payload.get("isSuperAdmin")
        raw_scopes = payload.get("adminScopes")
        if (
            not isinstance(user_id, str)
            or not user_id
            or not isinstance(is_super_admin, bool)
            or not isinstance(raw_scopes, list)
        ):
            raise SecurityAdminError("Security admin-context response has invalid shape")

        scopes: list[SecurityAdminScope] = []
        for raw in raw_scopes:
            if not isinstance(raw, dict):
                raise SecurityAdminError("Security admin-context scope has invalid shape")
            role_key = raw.get("roleKey")
            scope_type = raw.get("scopeType")
            scope_id = raw.get("scopeId")
            if (
                not isinstance(role_key, str)
                or not role_key
                or not isinstance(scope_type, str)
                or not scope_type
                or (scope_id is not None and not isinstance(scope_id, str))
            ):
                raise SecurityAdminError("Security admin-context scope has invalid shape")
            scopes.append(
                SecurityAdminScope(
                    role_key=role_key,
                    scope_type=scope_type,
                    scope_id=scope_id,
                )
            )
        return SecurityAdminContext(
            user_id=user_id,
            is_super_admin=is_super_admin,
            admin_scopes=tuple(scopes),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        human_bearer_token: str,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        if not human_bearer_token:
            raise ValueError("human_bearer_token is required")
        request_headers = {"Authorization": f"Bearer {human_bearer_token}"}
        if headers:
            request_headers.update(headers)
        try:
            response = self._client.request(
                method,
                path,
                headers=request_headers,
                json=json_body,
            )
        except httpx.HTTPError as exc:
            logger.warning("security_admin_call_failed", reason="endpoint_unavailable", path=path)
            raise SecurityAdminError("Security administrative endpoint is unavailable") from exc
        if response.status_code < 200 or response.status_code >= 300:
            logger.warning(
                "security_admin_call_failed",
                http_status=response.status_code,
                path=path,
            )
            raise SecurityAdminError(
                f"Security administrative request failed with HTTP {response.status_code}"
            )
        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SecurityAdminError("Security administrative response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SecurityAdminError("Security administrative response has invalid shape")
        return payload


class SecurityOAuthClient:
    """Minimal confidential-client wrapper for Security's internal OAuth endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 5.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip() or not client_id.strip() or not client_secret:
            raise ValueError("Security OAuth base URL and client credentials are required")
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=(client_id, client_secret),
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def get_service_token(self, *, tenant_id: str, permissions: Iterable[str]) -> str:
        if not tenant_id.strip():
            raise ValueError("tenant_id is required")
        return self._request_token(
            {
                "grant_type": "client_credentials",
                "tenant_id": tenant_id,
                "scope": _scope(permissions),
            }
        )

    def exchange_user_token(
        self,
        *,
        subject_token: str,
        permissions: Iterable[str],
    ) -> str:
        if not subject_token:
            raise ValueError("subject_token is required")
        return self._request_token(
            {
                "grant_type": TOKEN_EXCHANGE_GRANT,
                "subject_token": subject_token,
                "subject_token_type": ACCESS_TOKEN_TYPE,
                "scope": _scope(permissions),
            }
        )

    def _request_token(self, form: dict[str, str]) -> str:
        logger.debug(
            "security_token_exchange_start",
            grant_type=form.get("grant_type"),
            scope=form.get("scope"),
        )
        try:
            response = self._client.post("/oauth/token", data=form)
        except httpx.HTTPError as exc:
            logger.warning("security_token_exchange_failed", reason="endpoint_unavailable")
            raise SecurityTokenError("Security token endpoint is unavailable") from exc

        if response.status_code != 200:
            error = _safe_oauth_error(response)
            logger.warning(
                "security_token_exchange_failed",
                http_status=response.status_code,
                error=error,
            )
            raise SecurityTokenError(
                f"Security token request denied with HTTP {response.status_code}: {error}"
            )

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SecurityTokenError("Security token response is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise SecurityTokenError("Security token response has invalid shape")

        access_token = payload.get("access_token")
        token_type = payload.get("token_type")
        if not isinstance(access_token, str) or not access_token:
            raise SecurityTokenError("Security token response has no access_token")
        if token_type != "Bearer":
            raise SecurityTokenError("Security token response has invalid token_type")
        return access_token


def _scope(permissions: Iterable[str]) -> str:
    values = sorted({permission.strip() for permission in permissions if permission.strip()})
    if not values:
        raise ValueError("at least one downstream permission is required")
    if any(" " in value for value in values):
        raise ValueError("permission values must not contain spaces")
    return " ".join(values)


def _safe_oauth_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "request_denied"
    if not isinstance(payload, dict):
        return "request_denied"
    value = payload.get("error")
    return value if isinstance(value, str) and value else "request_denied"
