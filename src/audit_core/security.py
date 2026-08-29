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


@dataclass(frozen=True)
class ServiceIntegrationPrincipal:
    subject: str


# A Project Administration page can legitimately make several protected Audit Core
# requests. They must all validate the JWT, but they must not all fetch JWKS. Keep
# the public key set warm for five minutes; PyJWKClient still refreshes when an
# unknown signing kid is observed, which preserves normal Security key rotation.
_JWKS_CACHE_LIFESPAN_SECONDS = 300

# Never inherit PyJWT's much larger network timeout for a signing-key refresh. A
# healthy Security JWKS endpoint is a very small read; five seconds is already a
# generous dependency budget and matches the rest of Audit Core's Security calls.
_JWKS_REQUEST_TIMEOUT_SECONDS = 5.0


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
        self._jwks_client = jwks_client or PyJWKClient(
            jwks_url,
            cache_keys=True,
            cache_jwk_set=True,
            lifespan=_JWKS_CACHE_LIFESPAN_SECONDS,
            timeout=_JWKS_REQUEST_TIMEOUT_SECONDS,
        )

    def warm(self) -> None:
        """Fetch and cache the current Security JWK set before the first human request."""
        self._jwks_client.get_jwk_set(refresh=False)

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

    def validate_service_integration(self, token: str) -> ServiceIntegrationPrincipal:
        claims = self._decode(
            token,
            required_claims=["exp", "iss", "aud", "sub", "iat", "jti", "actor_type"],
        )
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject.strip():
            raise SecurityTokenError("Invalid Security ServiceIntegration token claims")
        if claims.get("actor_type") != "SERVICE_INTEGRATION":
            raise SecurityTokenError("Security token is not a ServiceIntegration token")

        # Security service tokens are audience-bound machine identities only. They
        # deliberately carry no Tenant/USER authority; accepting such claims here
        # would blur the internal callback trust boundary.
        forbidden_authority_claims = {
            "tenant_id",
            "access_session_id",
            "permissions",
            "roles",
            "device_id",
            "location_id",
            "act",
        }
        if forbidden_authority_claims.intersection(claims):
            raise SecurityTokenError(
                "Security ServiceIntegration token carries unsupported authority claims"
            )
        return ServiceIntegrationPrincipal(subject=subject.strip())

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