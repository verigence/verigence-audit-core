from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

import httpx
import structlog

logger = structlog.get_logger(__name__)


class SecurityTokenError(RuntimeError):
    """Security did not issue the requested ServiceIntegration access token."""


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
class SecurityTenant:
    tenant_id: str
    tenant_code: str
    tenant_name: str
    status: str


@dataclass(frozen=True)
class SecurityOperatingRoleMutation:
    tenant_id: str
    user_id: str
    changed: bool
    assignment_id: str | None
    role_key: str | None


class SecurityAdminClient:
    """Human-admin async client that always forwards the initiating Security bearer token.

    Must be created once (e.g. in lifespan) and reused across requests.
    Use aclose() or the async context manager to release the connection pool.
    """

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not base_url.strip():
            raise ValueError("Security base URL is required")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=2.0,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=2.0,
            ),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    async def get_admin_context(self, *, human_bearer_token: str) -> SecurityAdminContext:
        payload = await self._request_json(
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

    async def create_tenant(
        self,
        *,
        human_bearer_token: str,
        tenant_name: str,
        idempotency_key: str,
    ) -> SecurityTenant:
        payload = await self._request_json(
            "POST",
            "/security/v1/platform/tenants",
            human_bearer_token=human_bearer_token,
            headers={"Idempotency-Key": idempotency_key},
            json_body={"tenantName": tenant_name},
        )
        tenant_id = payload.get("tenantId")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise SecurityAdminError("Security Tenant create response has invalid shape")
        return self._tenant_from_payload(payload, requested_tenant_id=tenant_id)

    async def get_tenant(
        self,
        *,
        human_bearer_token: str,
        tenant_id: str,
    ) -> SecurityTenant:
        payload = await self._request_json(
            "GET",
            f"/security/v1/platform/tenants/{tenant_id}",
            human_bearer_token=human_bearer_token,
        )
        return self._tenant_from_payload(payload, requested_tenant_id=tenant_id)

    async def activate_tenant(
        self,
        *,
        human_bearer_token: str,
        tenant_id: str,
    ) -> SecurityTenant:
        payload = await self._request_json(
            "POST",
            f"/security/v1/platform/tenants/{tenant_id}/activate",
            human_bearer_token=human_bearer_token,
        )
        return self._tenant_from_payload(payload, requested_tenant_id=tenant_id)

    @staticmethod
    def _tenant_from_payload(
        payload: dict[str, Any],
        *,
        requested_tenant_id: str,
    ) -> SecurityTenant:
        response_tenant_id = payload.get("tenantId")
        tenant_code = payload.get("tenantCode")
        tenant_name = payload.get("tenantName")
        tenant_status = payload.get("status")
        if (
            not isinstance(response_tenant_id, str)
            or not response_tenant_id
            or not isinstance(tenant_code, str)
            or not tenant_code
            or not isinstance(tenant_name, str)
            or not tenant_name
            or not isinstance(tenant_status, str)
            or not tenant_status
        ):
            raise SecurityAdminError("Security Tenant response has invalid shape")
        if response_tenant_id != requested_tenant_id:
            raise SecurityAdminError("Security Tenant response does not match requested Tenant")
        return SecurityTenant(
            tenant_id=response_tenant_id,
            tenant_code=tenant_code,
            tenant_name=tenant_name,
            status=tenant_status,
        )

    async def list_global_users(
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
        payload = await self._request_json_list(
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

    async def set_operating_role(
        self,
        *,
        human_bearer_token: str,
        tenant_id: str,
        user_id: str,
        role_key: str,
    ) -> SecurityOperatingRoleMutation:
        payload = await self._request_json(
            "PUT",
            f"/security/v1/tenants/{tenant_id}/users/{user_id}/operating-role",
            human_bearer_token=human_bearer_token,
            json_body={"roleKey": role_key},
        )
        return self._operating_role_mutation(payload)

    async def remove_operating_role(
        self,
        *,
        human_bearer_token: str,
        tenant_id: str,
        user_id: str,
    ) -> SecurityOperatingRoleMutation:
        payload = await self._request_json(
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

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        human_bearer_token: str,
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        payload = await self._request_payload(
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

    async def _request_json_list(
        self,
        method: str,
        path: str,
        *,
        human_bearer_token: str,
        params: dict[str, str | int] | None = None,
    ) -> list[Any]:
        payload = await self._request_payload(
            method,
            path,
            human_bearer_token=human_bearer_token,
            params=params,
        )
        if not isinstance(payload, list):
            raise SecurityAdminError("Security administrative response has invalid shape")
        return payload

    async def _request_payload(
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
            response = await self._client.request(
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
    """Security v2 ServiceIntegration token client.

    Holds both a sync and async connection pool so it can serve plain-def
    (threadpool) handlers via get_service_token_sync() and async handlers
    via get_service_token().

    Must be created once (e.g. in lifespan) and reused across requests.
    Use aclose() at shutdown to drain both pools.
    """

    def __init__(
        self,
        *,
        base_url: str,
        client_id: str,
        client_secret: str,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sync_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not base_url.strip() or not client_id.strip() or not client_secret:
            raise ValueError("Security service-token base URL and client credentials are required")
        _timeout = httpx.Timeout(
            connect=2.0,
            read=timeout_seconds,
            write=timeout_seconds,
            pool=2.0,
        )
        _base = base_url.rstrip("/")
        _auth = (client_id, client_secret)
        # Async pool — used by async def callers
        self._async_client = httpx.AsyncClient(
            base_url=_base,
            auth=_auth,
            timeout=_timeout,
            transport=transport,
        )
        # Sync pool — used by plain def handlers running in FastAPI's threadpool
        self._sync_client = httpx.Client(
            base_url=_base,
            auth=_auth,
            timeout=_timeout,
            transport=sync_transport,
        )

    def close(self) -> None:
        """Close the sync connection pool."""
        self._sync_client.close()

    async def aclose(self) -> None:
        """Close both connection pools. Call from lifespan shutdown."""
        self._sync_client.close()
        await self._async_client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.aclose()

    def get_service_token(self, *, audience: str) -> str:
        """Synchronous token mint — call from plain def (threadpool) handlers."""
        requested_audience = audience.strip()
        if not requested_audience:
            raise ValueError("audience is required")
        logger.debug("security_service_token_start", audience=requested_audience)
        try:
            response = self._sync_client.post(
                "/security/v1/service/token",
                data={"audience": requested_audience},
            )
        except httpx.HTTPError as exc:
            logger.warning("security_service_token_failed", reason="endpoint_unavailable")
            raise SecurityTokenError("Security service-token endpoint is unavailable") from exc
        return _parse_service_token_response(response, requested_audience)

    async def get_service_token_async(self, *, audience: str) -> str:
        """Asynchronous token mint — call from async def handlers."""
        requested_audience = audience.strip()
        if not requested_audience:
            raise ValueError("audience is required")
        logger.debug("security_service_token_start", audience=requested_audience)
        try:
            response = await self._async_client.post(
                "/security/v1/service/token",
                data={"audience": requested_audience},
            )
        except httpx.HTTPError as exc:
            logger.warning("security_service_token_failed", reason="endpoint_unavailable")
            raise SecurityTokenError("Security service-token endpoint is unavailable") from exc
        return _parse_service_token_response(response, requested_audience)


def _parse_service_token_response(response: httpx.Response, requested_audience: str) -> str:
    if response.status_code != 200:
        error = _safe_service_error(response)
        logger.warning(
            "security_service_token_failed",
            http_status=response.status_code,
            error=error,
            audience=requested_audience,
        )
        raise SecurityTokenError(
            f"Security service-token request denied with HTTP {response.status_code}: {error}"
        )
    try:
        payload: Any = response.json()
    except ValueError as exc:
        raise SecurityTokenError("Security service-token response is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise SecurityTokenError("Security service-token response has invalid shape")
    access_token = payload.get("accessToken")
    token_type = payload.get("tokenType")
    returned_audience = payload.get("audience")
    if not isinstance(access_token, str) or not access_token:
        raise SecurityTokenError("Security service-token response has no accessToken")
    if token_type != "Bearer":
        raise SecurityTokenError("Security service-token response has invalid tokenType")
    if returned_audience != requested_audience:
        raise SecurityTokenError("Security service-token response audience mismatch")
    return access_token


def _safe_service_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "request_denied"
    if not isinstance(payload, dict):
        return "request_denied"
    value = payload.get("code") or payload.get("detail") or payload.get("error")
    return str(value) if value else "request_denied"
