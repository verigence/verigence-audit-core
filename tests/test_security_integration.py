from __future__ import annotations

import base64
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError

ISSUER = "verigence-security"
CLIENT_ID = "audit-core"
CLIENT_SECRET = "audit-core-secret"


@dataclass
class ControlledSecurity:
    private_key: rsa.RSAPrivateKey
    audiences: list[str] = field(default_factory=list)

    @property
    def public_key(self):
        return self.private_key.public_key()

    def handle(self, request: httpx.Request) -> httpx.Response:
        expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {expected_basic}":
            return httpx.Response(401, json={"code": "MACHINE_CREDENTIAL_INVALID"})
        if request.url.path != "/security/v1/service/token":
            return httpx.Response(404)

        form = {
            key: values[-1]
            for key, values in parse_qs(request.content.decode(), keep_blank_values=True).items()
        }
        audience = form.get("audience", "").strip()
        if not audience:
            return httpx.Response(400, json={"detail": "audience is required"})
        if audience not in {"di", "security", "audit-core"}:
            return httpx.Response(400, json={"detail": "audience is not registered"})
        self.audiences.append(audience)

        now = int(time.time())
        token = jwt.encode(
            {
                "iss": ISSUER,
                "aud": audience,
                "sub": CLIENT_ID,
                "actor_type": "SERVICE_INTEGRATION",
                "iat": now,
                "exp": now + 4 * 60 * 60,
            },
            self.private_key,
            algorithm="RS256",
            headers={"kid": "test"},
        )
        return httpx.Response(
            200,
            json={
                "accessToken": token,
                "tokenType": "Bearer",
                "expiresIn": 4 * 60 * 60,
                "audience": audience,
            },
        )


@pytest.fixture
def controlled_security() -> ControlledSecurity:
    return ControlledSecurity(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def _client(security: ControlledSecurity) -> SecurityOAuthClient:
    return SecurityOAuthClient(
        base_url="https://security.test",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(security.handle),
    )


def _decode(token: str, public_key, *, audience: str) -> dict:
    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        issuer=ISSUER,
        audience=audience,
    )


def test_service_token_uses_canonical_v2_endpoint_and_di_audience(
    controlled_security: ControlledSecurity,
) -> None:
    with _client(controlled_security) as security_client:
        token = security_client.get_service_token(audience="di")

    claims = _decode(token, controlled_security.public_key, audience="di")
    assert claims["sub"] == CLIENT_ID
    assert claims["actor_type"] == "SERVICE_INTEGRATION"
    assert "tenant_id" not in claims
    assert "permissions" not in claims
    assert controlled_security.audiences == ["di"]


def test_service_token_is_audience_bound(
    controlled_security: ControlledSecurity,
) -> None:
    with _client(controlled_security) as security_client:
        token = security_client.get_service_token(audience="di")

    with pytest.raises(jwt.InvalidAudienceError):
        _decode(token, controlled_security.public_key, audience="security")


def test_service_token_rejects_unregistered_audience(
    controlled_security: ControlledSecurity,
) -> None:
    with (
        _client(controlled_security) as security_client,
        pytest.raises(SecurityTokenError, match="audience is not registered"),
    ):
        security_client.get_service_token(audience="unknown-module")


def test_service_token_requires_audience(controlled_security: ControlledSecurity) -> None:
    with _client(controlled_security) as security_client, pytest.raises(ValueError):
        security_client.get_service_token(audience="")


def test_service_token_response_audience_mismatch_fails_closed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "accessToken": "signed-token",
                "tokenType": "Bearer",
                "expiresIn": 14400,
                "audience": "security",
            },
        )

    with (
        SecurityOAuthClient(
            base_url="https://security.test",
            client_id=CLIENT_ID,
            client_secret=CLIENT_SECRET,
            transport=httpx.MockTransport(handler),
        ) as security_client,
        pytest.raises(SecurityTokenError, match="audience mismatch"),
    ):
        security_client.get_service_token(audience="di")
