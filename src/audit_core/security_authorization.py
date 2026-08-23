from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Self

import httpx
import structlog

from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError

logger = structlog.get_logger(__name__)


class SecurityAuthorizationError(RuntimeError):
    """Security could not return a trustworthy functional authorization decision."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class SecurityAuthorizationDecision:
    allowed: bool
    reason_code: str
    user_id: str
    tenant_id: str
    permission_key: str
    role_key: str | None


class SecurityAuthorizationClient:
    """Live Security v2 authorization client for protected human business requests.

    The browser-provided Security human JWT proves the USER identity at Audit Core.
    Audit Core then uses its own ServiceIntegration identity to ask Security for the
    current Tenant/permission decision. No Tenant permission claims are copied from
    the human token and no authorization projection is cached locally.
    """

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
            raise ValueError("Security authorization client configuration is required")
        normalized = base_url.rstrip("/")
        self._oauth = SecurityOAuthClient(
            base_url=normalized,
            client_id=client_id,
            client_secret=client_secret,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        self._client = httpx.Client(
            base_url=normalized,
            timeout=timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._oauth.close()
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def check_user_permission(
        self,
        *,
        user_id: str,
        tenant_id: str,
        permission_key: str,
    ) -> SecurityAuthorizationDecision:
        if not user_id or not tenant_id or not permission_key:
            raise ValueError("user_id, tenant_id and permission_key are required")

        try:
            service_token = self._oauth.get_service_token(audience="security")
        except SecurityTokenError as exc:
            raise SecurityAuthorizationError(
                "Security ServiceIntegration token could not be issued"
            ) from exc

        try:
            response = self._client.post(
                "/security/v1/authorization/check",
                headers={"Authorization": f"Bearer {service_token}"},
                json={
                    "userId": user_id,
                    "tenantId": tenant_id,
                    "permissionKey": permission_key,
                },
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "security_authorization_failed",
                reason="endpoint_unavailable",
                permission_key=permission_key,
            )
            raise SecurityAuthorizationError(
                "Security authorization endpoint is unavailable"
            ) from exc

        if response.status_code != 200:
            logger.warning(
                "security_authorization_failed",
                http_status=response.status_code,
                permission_key=permission_key,
            )
            raise SecurityAuthorizationError(
                f"Security authorization request failed with HTTP {response.status_code}",
                http_status=response.status_code,
            )

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise SecurityAuthorizationError(
                "Security authorization response is not valid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise SecurityAuthorizationError(
                "Security authorization response has invalid shape"
            )

        allowed = payload.get("allowed")
        reason_code = payload.get("reasonCode")
        response_user_id = payload.get("userId")
        response_tenant_id = payload.get("tenantId")
        response_permission_key = payload.get("permissionKey")
        role_key = payload.get("roleKey")
        if (
            not isinstance(allowed, bool)
            or not isinstance(reason_code, str)
            or not reason_code
            or response_user_id != user_id
            or response_tenant_id != tenant_id
            or response_permission_key != permission_key
            or (role_key is not None and not isinstance(role_key, str))
        ):
            raise SecurityAuthorizationError(
                "Security authorization response does not match the requested decision"
            )

        return SecurityAuthorizationDecision(
            allowed=allowed,
            reason_code=reason_code,
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=role_key,
        )


def get_security_authorization_client() -> Iterator[SecurityAuthorizationClient]:
    base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not base_url or not client_id or not client_secret:
        raise RuntimeError("Security ServiceIntegration authorization is not configured")
    with SecurityAuthorizationClient(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
    ) as client:
        yield client
