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


@dataclass(frozen=True)
class HumanPrincipal:
    subject: str


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
        claims = self._decode(
            token,
            required_claims=["exp", "iss", "aud", "sub", "tenant_id", "permissions"],
        )

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

    def validate_human(self, token: str) -> HumanPrincipal:
        claims = self._decode(
            token,
            required_claims=["exp", "iss", "aud", "sub", "iat", "jti", "actor_type"],
        )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise SecurityTokenError("Invalid Security human token claims")
        if claims.get("actor_type") != "USER":
            raise SecurityTokenError("Security token is not a human USER token")

        forbidden_authority_claims = {
            "tenant_id",
            "permissions",
            "roles",
            "device_id",
            "location_id",
            "act",
        }
        if forbidden_authority_claims.intersection(claims):
            raise SecurityTokenError("Security human token carries unsupported authority claims")
        return HumanPrincipal(subject=subject)

    def _decode(self, token: str, *, required_claims: list[str]) -> dict:
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)
            return jwt.decode(
                token,
                signing_key.key,
                algorithms=[signing_key.algorithm_name],
                issuer=self._issuer,
                audience=self._audience,
                options={"require": required_claims},
            )
        except (PyJWTError, ValueError, TypeError) as exc:
            raise SecurityTokenError("Invalid Security token") from exc
