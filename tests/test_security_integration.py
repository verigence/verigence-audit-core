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
AUDIENCE = "verigence-platform"
CLIENT_ID = "audit-core"
CLIENT_SECRET = "audit-core-secret"
TENANT_ID = "tenant-1"
INTEGRATION_PERMISSIONS = frozenset({"di.document.upload", "di.document.read"})


@dataclass
class ControlledSecurity:
    private_key: rsa.RSAPrivateKey
    grant_types: list[str] = field(default_factory=list)

    @property
    def public_key(self):
        return self.private_key.public_key()

    def issue_user_token(self, *, permissions: list[str]) -> str:
        return self._issue(
            subject="pc-1",
            tenant_id=TENANT_ID,
            actor_type="USER",
            permissions=permissions,
            roles=["PC"],
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        expected_basic = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if request.headers.get("Authorization") != f"Basic {expected_basic}":
            return httpx.Response(401, json={"error": "invalid_client"})
        if request.url.path != "/oauth/token":
            return httpx.Response(404)

        form = {
            key: values[-1]
            for key, values in parse_qs(request.content.decode(), keep_blank_values=True).items()
        }
        grant_type = form.get("grant_type", "")
        self.grant_types.append(grant_type)
        requested = frozenset(form.get("scope", "").split())
        if not requested:
            return httpx.Response(400, json={"error": "invalid_scope"})

        if grant_type == "client_credentials":
            if not requested.issubset(INTEGRATION_PERMISSIONS):
                return httpx.Response(400, json={"error": "invalid_scope"})
            token = self._issue(
                subject=CLIENT_ID,
                tenant_id=form.get("tenant_id", ""),
                actor_type="SERVICE",
                permissions=sorted(requested),
                roles=[],
            )
        elif grant_type == "urn:ietf:params:oauth:grant-type:token-exchange":
            try:
                claims = jwt.decode(
                    form.get("subject_token", ""),
                    self.public_key,
                    algorithms=["RS256"],
                    issuer=ISSUER,
                    audience=AUDIENCE,
                )
            except jwt.PyJWTError:
                return httpx.Response(400, json={"error": "invalid_grant"})
            if claims.get("actor_type") != "USER":
                return httpx.Response(400, json={"error": "invalid_grant"})
            allowed = set(claims.get("permissions", [])).intersection(INTEGRATION_PERMISSIONS)
            if not requested.issubset(allowed):
                return httpx.Response(400, json={"error": "invalid_scope"})
            token = self._issue(
                subject=claims["sub"],
                tenant_id=claims["tenant_id"],
                actor_type="USER",
                permissions=sorted(requested),
                roles=claims.get("roles", []),
                extra_claims={"act": {"sub": CLIENT_ID}},
            )
        else:
            return httpx.Response(400, json={"error": "unsupported_grant_type"})

        return httpx.Response(
            200,
            json={
                "access_token": token,
                "token_type": "Bearer",
                "expires_in": 300,
                "scope": " ".join(sorted(requested)),
            },
        )

    def _issue(
        self,
        *,
        subject: str,
        tenant_id: str,
        actor_type: str,
        permissions: list[str],
        roles: list[str],
        extra_claims: dict | None = None,
    ) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": AUDIENCE,
            "sub": subject,
            "tenant_id": tenant_id,
            "actor_type": actor_type,
            "roles": roles,
            "permissions": permissions,
            "iat": now,
            "exp": now + 300,
        }
        if extra_claims:
            claims.update(extra_claims)
        return jwt.encode(claims, self.private_key, algorithm="RS256", headers={"kid": "test"})


@pytest.fixture
def controlled_security() -> ControlledSecurity:
    return ControlledSecurity(rsa.generate_private_key(public_exponent=65537, key_size=2048))


def _oauth_client(security: ControlledSecurity) -> SecurityOAuthClient:
    return SecurityOAuthClient(
        base_url="https://security.test",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        transport=httpx.MockTransport(security.handle),
    )


def _di_probe_transport(public_key) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization", "")
        if not authorization.startswith("Bearer "):
            return httpx.Response(401)
        try:
            claims = jwt.decode(
                authorization[7:],
                public_key,
                algorithms=["RS256"],
                issuer=ISSUER,
                audience=AUDIENCE,
            )
        except jwt.PyJWTError:
            return httpx.Response(401)

        tenant_id = request.url.path.split("/")[3]
        required_permission = request.headers.get("X-Required-Permission")
        if claims.get("tenant_id") != tenant_id:
            return httpx.Response(403)
        if required_permission not in claims.get("permissions", []):
            return httpx.Response(403)
        return httpx.Response(200, json={"actorType": claims.get("actor_type")})

    return httpx.MockTransport(handle)


def _call_controlled_di(*, token: str, public_key, permission: str) -> httpx.Response:
    with httpx.Client(
        base_url="https://di.test",
        transport=_di_probe_transport(public_key),
    ) as client:
        return client.get(
            f"/v1/tenants/{TENANT_ID}/probe",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Required-Permission": permission,
            },
        )


def _decode(token: str, public_key) -> dict:
    return jwt.decode(
        token,
        public_key,
        algorithms=["RS256"],
        issuer=ISSUER,
        audience=AUDIENCE,
    )


def test_delegated_user_flow_is_narrowed_and_accepted_by_di_contract(
    controlled_security: ControlledSecurity,
) -> None:
    user_token = controlled_security.issue_user_token(
        permissions=[
            "audit.evidence.upload",
            "di.document.upload",
            "di.document.read",
        ]
    )

    with _oauth_client(controlled_security) as security_client:
        delegated = security_client.exchange_user_token(
            subject_token=user_token,
            permissions=["di.document.upload"],
        )

    claims = _decode(delegated, controlled_security.public_key)
    assert claims["sub"] == "pc-1"
    assert claims["tenant_id"] == TENANT_ID
    assert claims["actor_type"] == "USER"
    assert claims["permissions"] == ["di.document.upload"]
    assert claims["act"] == {"sub": CLIENT_ID}

    response = _call_controlled_di(
        token=delegated,
        public_key=controlled_security.public_key,
        permission="di.document.upload",
    )
    assert response.status_code == 200


def test_service_flow_is_tenant_scoped_and_accepted_by_di_contract(
    controlled_security: ControlledSecurity,
) -> None:
    with _oauth_client(controlled_security) as security_client:
        service_token = security_client.get_service_token(
            tenant_id=TENANT_ID,
            permissions=["di.document.read"],
        )

    claims = _decode(service_token, controlled_security.public_key)
    assert claims["sub"] == CLIENT_ID
    assert claims["tenant_id"] == TENANT_ID
    assert claims["actor_type"] == "SERVICE"
    assert claims["permissions"] == ["di.document.read"]

    response = _call_controlled_di(
        token=service_token,
        public_key=controlled_security.public_key,
        permission="di.document.read",
    )
    assert response.status_code == 200


def test_delegated_denial_fails_closed_without_service_fallback(
    controlled_security: ControlledSecurity,
) -> None:
    user_token = controlled_security.issue_user_token(
        permissions=["audit.evidence.upload", "di.document.upload"]
    )

    with (
        _oauth_client(controlled_security) as security_client,
        pytest.raises(SecurityTokenError, match="invalid_scope"),
    ):
        security_client.exchange_user_token(
            subject_token=user_token,
            permissions=["di.verification.write"],
        )

    assert controlled_security.grant_types == [
        "urn:ietf:params:oauth:grant-type:token-exchange"
    ]


def test_di_contract_rejects_tenant_mismatch(controlled_security: ControlledSecurity) -> None:
    with _oauth_client(controlled_security) as security_client:
        token = security_client.get_service_token(
            tenant_id="other-tenant",
            permissions=["di.document.read"],
        )

    response = _call_controlled_di(
        token=token,
        public_key=controlled_security.public_key,
        permission="di.document.read",
    )
    assert response.status_code == 403
