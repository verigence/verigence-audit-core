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
    """Security could not complete a human administrative request."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


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


@dataclass(frozen=True)
class SecurityGlobalUser:
    user_id: str
    display_name: str
    primary_email: str | None
    status: str


@dataclass(frozen=True)
class SecurityOperatingRoleMutation:
    tenant_id: str
    user_id: str
    changed: bool
    assignment_id: str | None
    role_key: str | None


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

    def list_global_users(
        self,
        *,
        human_bearer_token: str,
        search: str | None,
        limit: int,
    ) -> tuple[SecurityGlobalUser, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("Security USER directory limit must be between 1 and 200")
        params: dict[str, str | int] = {
            "userStatus": "ACTIVE",
            "limit": limit,
            "offset": 0,
        }
        if search:
            params["search"] = search
        payload = self._request_json_list(
            "GET",
            "/security/v1/platform/users",
            human_bearer_token=human_bearer_token,
            params=params,
        )
        users: list[SecurityGlobalUser] = []
        for raw in payload:
            if not isinstance(raw, dict):
                raise SecurityAdminError("Security USER directory response has invalid shape")
            user_id = raw.get("userId")
            display_name = raw.get("displayName")
            primary_email = raw.get("primaryEmail")
            status = raw.get("status")
            if (
                not isinstance(user_id, str)
                or not user_id
                or not isinstance(display_name, str)
                or not display_name
                or (primary_email is not None and not isinstance(primary_email, str))
                or not isinstance(status, str)
                or not status
            ):
                raise SecurityAdminError("Security USER directory response has invalid shape")
            users.append(
                SecurityGlobalUser(
                    user_id=user_id,
                    display_name=display_name,
                    primary_email=primary_email,
                    status=status,
                )
            )
        return tuple(users)

    def set_operating_role(
        self,
        *,
        human_bearer_token: str,
        tenant_id: str,
        user_id: str,
        role_key: str,
    ) -> SecurityOperatingRoleMutation:
        payload = self._request_json(
            "PUT",
            f"/security/v1/tenants/{tenant_id}/users/{user_id}/operating-role",
            human_bearer_token=human_bearer_token,
            json_body={"roleKey": role_key},
        )
        return self._operating_role_mutation(payload)

    def remove_operating_role(
        self,
        *,
        human_bearer_token: str,
        tenant_id: str,
        user_id: str,
    ) -> SecurityOperatingRoleMutation:
        payload = self._request_json(
            "DELETE",
            f"/security/v1/tenants/{tenant_id}/users/{user_id}/operating-role",
            human_bearer_token=human_bearer_token,
        )
        return self._operating_role_mutation(payload)

    @staticmethod
    def _operating_role_mutation(payload: dict[str, Any]) -> SecurityOperatingRoleMutation:
        tenant_id = payload.get("tenantId")
        user_id = payload.get("userId")
        changed = payload.get("changed")
        assignment_id = payload.get("assignmentId")
        role_key = payload.get("roleKey")
        if (
            not isinstance(tenant_id, str)
            or not tenant_id
            or not isinstance(user_id, str)
            or not user_id
            or not isinstance(changed, bool)
            or (assignment_id is not None and not isinstance(assignment_id, str))
            or (role_key is not None and not isinstance(role_key, str))
        ):
            raise SecurityAdminError("Security operating-role response has invalid shape")
        return SecurityOperatingRoleMutation(
            tenant_id=tenant_id,
            user_id=user_id,
            changed=changed,
            assignment_id=assignment_id,
            role_key=role_key,
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        human_bearer_token: str,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        payload = self._request_payload(
            method,
            path,
            human_bearer_token=human_bearer_token,
            headers=headers,
            json_body=json_body,
            params=params,
        )
        if not isinstance(payload, dict):
            raise SecurityAdminError("Security administrative response has invalid shape")
        return payload

    def _request_json_list(
        self,
        method: str,
        path: str,
        *,
        human_bearer_token: str,
        params: dict[str, str | int] | None = None,
    ) -> list[Any]:
        payload = self._request_payload(
            method,
            path,
            human_bearer_token=human_bearer_token,
            params=params,
        )
        if not isinstance(payload, list):
            raise SecurityAdminError("Security administrative response has invalid shape")
        return payload

    def _request_payload(
        self,
        method: str,
        path: str,
        *,
        human_bearer_token: str,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> Any:
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
                params=params,
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
                f"Security administrative request failed with HTTP {response.status_code}",
                http_status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise SecurityAdminError("Security administrative response is not valid JSON") from exc


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
