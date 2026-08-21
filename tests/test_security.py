from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from audit_core.security import (
    HumanPrincipal,
    Principal,
    SecurityTokenError,
    SecurityTokenValidator,
)

ISSUER = "https://security.verigence.test"
AUDIENCE = "verigence-platform"


class _SigningKey:
    algorithm_name = "RS256"

    def __init__(self, key) -> None:
        self.key = key


class _StaticJwksClient:
    def __init__(self, public_key) -> None:
        self._signing_key = _SigningKey(public_key)

    def get_signing_key_from_jwt(self, token: str):
        return self._signing_key


def _private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _token(private_key, **overrides) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "tenant_id": "tenant-123",
        "permissions": ["audit.journey.read"],
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _human_token(private_key, **overrides) -> str:
    now = datetime.now(UTC)
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-123",
        "actor_type": "USER",
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "jti": str(uuid4()),
    }
    claims.update(overrides)
    return jwt.encode(claims, private_key, algorithm="RS256")


def _validator(private_key) -> SecurityTokenValidator:
    return SecurityTokenValidator(
        jwks_url="https://security.verigence.test/.well-known/jwks.json",
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_client=_StaticJwksClient(private_key.public_key()),
    )


def test_valid_security_token_returns_principal() -> None:
    private_key = _private_key()

    principal = _validator(private_key).validate(_token(private_key))

    assert principal == Principal(
        subject="user-123",
        tenant_id="tenant-123",
        permissions=("audit.journey.read",),
    )


def test_valid_security_human_token_returns_global_user_only() -> None:
    private_key = _private_key()

    principal = _validator(private_key).validate_human(_human_token(private_key))

    assert principal == HumanPrincipal(subject="user-123")


@pytest.mark.parametrize(
    ("claim", "value"),
    [("iss", "https://wrong-issuer.test"), ("aud", "wrong-audience")],
)
def test_security_token_rejects_wrong_issuer_or_audience(claim: str, value: str) -> None:
    private_key = _private_key()

    with pytest.raises(SecurityTokenError):
        _validator(private_key).validate(_token(private_key, **{claim: value}))


def test_security_token_rejects_invalid_signature() -> None:
    trusted_key = _private_key()
    untrusted_key = _private_key()

    with pytest.raises(SecurityTokenError):
        _validator(trusted_key).validate(_token(untrusted_key))


def test_security_token_rejects_expired_token() -> None:
    private_key = _private_key()
    expired_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(SecurityTokenError):
        _validator(private_key).validate(_token(private_key, exp=expired_at))


def test_human_validation_rejects_service_integration_actor() -> None:
    private_key = _private_key()

    with pytest.raises(SecurityTokenError, match="not a human USER token"):
        _validator(private_key).validate_human(
            _human_token(private_key, actor_type="SERVICE_INTEGRATION")
        )


def test_human_validation_rejects_embedded_tenant_authority() -> None:
    private_key = _private_key()

    with pytest.raises(SecurityTokenError, match="unsupported authority claims"):
        _validator(private_key).validate_human(
            _human_token(
                private_key,
                tenant_id="tenant-123",
                permissions=["audit.project.write"],
            )
        )
