from dataclasses import dataclass

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWTError


class SecurityTokenError(RuntimeError):
    """Raised when a Security token cannot be trusted."""


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    permissions: tuple[str, ...]


class SecurityTokenValidator:
    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_client = jwks_client or PyJWKClient(jwks_url)

    def validate(self, token: str) -> Principal:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=[signing_key.algorithm_name],
                issuer=self._issuer,
                audience=self._audience,
                options={
                    "require": ["exp", "iss", "aud", "sub", "tenant_id", "permissions"]
                },
            )
        except (PyJWTError, ValueError, TypeError) as exc:
            raise SecurityTokenError("Invalid Security token") from exc

        subject = claims.get("sub")
        tenant_id = claims.get("tenant_id")
        permissions = claims.get("permissions")
        if (
            not isinstance(subject, str)
            or not subject
            or not isinstance(tenant_id, str)
            or not tenant_id
            or not isinstance(permissions, list)
            or any(not isinstance(permission, str) for permission in permissions)
        ):
            raise SecurityTokenError("Invalid Security token claims")

        return Principal(
            subject=subject,
            tenant_id=tenant_id,
            permissions=tuple(permissions),
        )
