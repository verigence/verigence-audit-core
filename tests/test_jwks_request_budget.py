from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWKClient as RealPyJWKClient

from audit_core import security


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def test_repeated_human_validation_fetches_jwks_once(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_numbers = private_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "kty": "RSA",
                "kid": "project-screen-key",
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_uint(public_numbers.n),
                "e": _b64url_uint(public_numbers.e),
            }
        ]
    }
    observed_kwargs: dict[str, Any] = {}
    clients: list[CountingPyJWKClient] = []

    class CountingPyJWKClient(RealPyJWKClient):
        def __init__(self, uri: str, **kwargs: Any) -> None:
            observed_kwargs.update(kwargs)
            super().__init__(uri, **kwargs)
            self.fetch_count = 0
            clients.append(self)

        def fetch_data(self) -> dict[str, Any]:
            self.fetch_count += 1
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(jwks)
            return jwks

    monkeypatch.setattr(security, "PyJWKClient", CountingPyJWKClient)
    validator = security.SecurityTokenValidator(
        jwks_url="https://security.test/.well-known/jwks.json",
        issuer="verigence-security",
        audience="verigence-platform",
    )
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "iss": "verigence-security",
            "aud": "verigence-platform",
            "sub": "user-123",
            "actor_type": "USER",
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "jti": "project-screen-token",
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "project-screen-key"},
    )

    for _ in range(5):
        assert validator.validate_human(token).subject == "user-123"

    assert len(clients) == 1
    assert clients[0].fetch_count == 1
    assert observed_kwargs["cache_keys"] is True
    assert observed_kwargs["cache_jwk_set"] is True
    assert observed_kwargs["lifespan"] == security._JWKS_CACHE_LIFESPAN_SECONDS
    assert observed_kwargs["timeout"] == security._JWKS_REQUEST_TIMEOUT_SECONDS
    assert security._JWKS_REQUEST_TIMEOUT_SECONDS <= 5.0
