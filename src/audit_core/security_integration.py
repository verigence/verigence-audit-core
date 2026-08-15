from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import httpx

TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
ACCESS_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:access_token"


class SecurityTokenError(RuntimeError):
    """Security did not issue the requested downstream access token."""


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

    def __enter__(self) -> SecurityOAuthClient:
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
        try:
            response = self._client.post("/oauth/token", data=form)
        except httpx.HTTPError as exc:
            raise SecurityTokenError("Security token endpoint is unavailable") from exc

        if response.status_code != 200:
            error = _safe_oauth_error(response)
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
