from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Any

import httpx
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from audit_core.security import HumanPrincipal, SecurityTokenError, SecurityTokenValidator
from audit_core.security_integration import SecurityOAuthClient
from audit_core.verigence_attendance.settings import get_settings

_bearer = HTTPBearer(auto_error=False)


class AttendanceAuthorizationError(RuntimeError):
    pass


class AttendanceDependencyError(RuntimeError):
    pass


@lru_cache
def _validator() -> SecurityTokenValidator:
    settings = get_settings()
    return SecurityTokenValidator(
        jwks_url=settings.security_jwks_url,
        issuer=settings.security_issuer,
        audience=settings.security_audience,
    )


def bearer_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise SecurityTokenError("Missing Security human token")
    token = credentials.credentials.strip()
    if not token:
        raise SecurityTokenError("Missing Security human token")
    return token


def human_principal(token: Annotated[str, Depends(bearer_token)]) -> HumanPrincipal:
    return _validator().validate_human(token)


class AttendanceSecurityClient:
    """Attendance-only consumer of the existing Security authorization contract."""

    def __init__(self) -> None:
        settings = get_settings()
        self._oauth = SecurityOAuthClient(
            base_url=settings.security_base_url,
            client_id=settings.security_client_id,
            client_secret=settings.security_client_secret,
            timeout_seconds=4.0,
        )
        self._client = httpx.Client(
            base_url=settings.security_base_url.rstrip("/"),
            timeout=4.0,
        )

    def close(self) -> None:
        self._oauth.close()
        self._client.close()

    def require(self, *, user_id: str, permission_key: str) -> dict[str, Any]:
        try:
            service_token = self._oauth.get_service_token(audience="security")
            response = self._client.post(
                "/security/v1/authorization/check",
                headers={"Authorization": f"Bearer {service_token}"},
                json={
                    "userId": user_id,
                    "tenantId": None,
                    "permissionKey": permission_key,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            raise AttendanceDependencyError(
                "Security authorization is temporarily unavailable"
            ) from exc
        if not isinstance(payload, dict) or payload.get("allowed") is not True:
            reason = payload.get("reasonCode") if isinstance(payload, dict) else None
            raise AttendanceAuthorizationError(str(reason or "PERMISSION_DENIED"))
        return {str(key): value for key, value in payload.items()}

    def allowed(self, *, user_id: str, permission_key: str) -> bool:
        try:
            self.require(user_id=user_id, permission_key=permission_key)
            return True
        except AttendanceAuthorizationError:
            return False


@lru_cache
def security_client() -> AttendanceSecurityClient:
    return AttendanceSecurityClient()
