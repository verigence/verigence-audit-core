from __future__ import annotations

import base64
import json
import os
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Self

import httpx
import structlog

from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError

logger = structlog.get_logger(__name__)

# Reuse the Security ServiceIntegration token until shortly before its JWT expiry.
# If a non-JWT test/dummy token is returned, preserve the previous short fallback.
_SERVICE_TOKEN_EXPIRY_SAFETY_SECONDS = 300.0
_SERVICE_TOKEN_FALLBACK_REUSE_SECONDS = 60.0

# Reuse only a successful identical ALLOW for a short process-local window. DENY
# and errors are deliberately never cached. Human JWT validation still happens on
# every request.
_AUTHORIZATION_ALLOW_REUSE_SECONDS = 60.0


def _service_token_reuse_seconds(token: str) -> float:
    parts = token.split(".")
    if len(parts) != 3:
        return _SERVICE_TOKEN_FALLBACK_REUSE_SECONDS
    try:
        payload_segment = parts[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + padding).decode("utf-8")
        )
    except (ValueError, TypeError):
        return _SERVICE_TOKEN_FALLBACK_REUSE_SECONDS
    if not isinstance(payload, dict):
        return _SERVICE_TOKEN_FALLBACK_REUSE_SECONDS
    expires_at = payload.get("exp")
    if isinstance(expires_at, bool) or not isinstance(expires_at, (int, float)):
        return _SERVICE_TOKEN_FALLBACK_REUSE_SECONDS
    return max(
        0.0,
        float(expires_at) - time.time() - _SERVICE_TOKEN_EXPIRY_SAFETY_SECONDS,
    )


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
    """Security v2 authorization client for protected human business requests.

    The browser-provided Security human JWT proves the USER identity at Audit Core.
    Audit Core then uses its own ServiceIntegration identity to ask Security for the
    current Tenant/permission decision. No Tenant permission claims are copied from
    the human token.

    Backend credentials are reused and a successful identical authorization ALLOW may
    be reused briefly to coalesce one UI request burst. DENY/error responses are never
    cached and browser state never becomes authoritative.
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
        self._service_token: str | None = None
        self._service_token_reuse_until = 0.0
        self._service_token_lock = threading.Lock()
        self._allow_cache: dict[
            tuple[str, str, str],
            tuple[float, SecurityAuthorizationDecision],
        ] = {}
        self._allow_cache_lock = threading.Lock()
        self._decision_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._decision_locks_lock = threading.Lock()

    def close(self) -> None:
        self._oauth.close()
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def warm_service_token(self) -> None:
        """Issue and cache the backend Security token before the first work request."""
        self._service_token_for_security()

    def _service_token_for_security(self) -> str:
        now = time.monotonic()
        if self._service_token and now < self._service_token_reuse_until:
            return self._service_token

        with self._service_token_lock:
            now = time.monotonic()
            if self._service_token and now < self._service_token_reuse_until:
                return self._service_token
            try:
                token = self._oauth.get_service_token(audience="security")
            except SecurityTokenError as exc:
                raise SecurityAuthorizationError(
                    "Security ServiceIntegration token could not be issued"
                ) from exc
            self._service_token = token
            self._service_token_reuse_until = now + _service_token_reuse_seconds(token)
            return token

    def _cached_allow(
        self,
        key: tuple[str, str, str],
    ) -> SecurityAuthorizationDecision | None:
        now = time.monotonic()
        with self._allow_cache_lock:
            cached = self._allow_cache.get(key)
            if cached is None:
                return None
            expires_at, decision = cached
            if expires_at <= now:
                self._allow_cache.pop(key, None)
                return None
            return decision

    def _remember_allow(
        self,
        key: tuple[str, str, str],
        decision: SecurityAuthorizationDecision,
    ) -> None:
        if not decision.allowed:
            return
        with self._allow_cache_lock:
            self._allow_cache[key] = (
                time.monotonic() + _AUTHORIZATION_ALLOW_REUSE_SECONDS,
                decision,
            )

    def _decision_lock(self, key: tuple[str, str, str]) -> threading.Lock:
        with self._decision_locks_lock:
            lock = self._decision_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._decision_locks[key] = lock
            return lock

    def check_user_permission(
        self,
        *,
        user_id: str,
        tenant_id: str,
        permission_key: str,
    ) -> SecurityAuthorizationDecision:
        if not user_id or not tenant_id or not permission_key:
            raise ValueError("user_id, tenant_id and permission_key are required")

        key = (user_id, tenant_id, permission_key)
        cached = self._cached_allow(key)
        if cached is not None:
            return cached

        # Concurrent requests for the same USER/Tenant/permission share one live
        # Security decision. Different authorization keys can proceed independently.
        with self._decision_lock(key):
            cached = self._cached_allow(key)
            if cached is not None:
                return cached
            decision = self._request_permission_decision(
                user_id=user_id,
                tenant_id=tenant_id,
                permission_key=permission_key,
            )
            self._remember_allow(key, decision)
            return decision

    def _request_permission_decision(
        self,
        *,
        user_id: str,
        tenant_id: str,
        permission_key: str,
    ) -> SecurityAuthorizationDecision:
        service_token = self._service_token_for_security()

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


@lru_cache
def _shared_security_authorization_client() -> SecurityAuthorizationClient:
    base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not base_url or not client_id or not client_secret:
        raise RuntimeError("Security ServiceIntegration authorization is not configured")
    return SecurityAuthorizationClient(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
    )


def get_security_authorization_client() -> SecurityAuthorizationClient:
    """Reuse backend HTTP/OAuth state across requests.

    This preserves HTTP connection pooling, ServiceIntegration token reuse and the
    short successful-ALLOW coalescing window. It never caches DENY/error decisions.
    """

    return _shared_security_authorization_client()
