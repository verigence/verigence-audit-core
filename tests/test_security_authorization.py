import json

import httpx
import pytest

from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
)


def test_security_authorization_client_uses_service_identity_and_exact_decision() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/security/v1/service/token":
            assert request.headers.get("authorization", "").startswith("Basic ")
            assert b"audience=security" in request.content
            return httpx.Response(
                200,
                json={
                    "accessToken": "service-token",
                    "tokenType": "Bearer",
                    "audience": "security",
                },
            )
        if request.url.path == "/security/v1/authorization/check":
            assert request.headers["authorization"] == "Bearer service-token"
            payload = json.loads(request.content)
            assert payload == {
                "userId": "user-1",
                "tenantId": "tenant-1",
                "permissionKey": "audit.journey.read",
            }
            return httpx.Response(
                200,
                json={
                    "allowed": True,
                    "decision": "ALLOW",
                    "reasonCode": "AUTHORIZED",
                    "userId": "user-1",
                    "tenantId": "tenant-1",
                    "permissionKey": "audit.journey.read",
                    "moduleKey": "AUDIT",
                    "classification": "OPERATING",
                    "roleKey": "PC",
                },
            )
        raise AssertionError(f"Unexpected Security request: {request.url}")

    transport = httpx.MockTransport(handler)
    with SecurityAuthorizationClient(
        base_url="https://security.test",
        client_id="audit-core",
        client_secret="secret",
        transport=transport,
    ) as client:
        decision = client.check_user_permission(
            user_id="user-1",
            tenant_id="tenant-1",
            permission_key="audit.journey.read",
        )

    assert decision.allowed is True
    assert decision.role_key == "PC"
    assert [request.url.path for request in requests] == [
        "/security/v1/service/token",
        "/security/v1/authorization/check",
    ]


def test_security_authorization_client_rejects_mismatched_decision() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/security/v1/service/token":
            return httpx.Response(
                200,
                json={
                    "accessToken": "service-token",
                    "tokenType": "Bearer",
                    "audience": "security",
                },
            )
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "reasonCode": "AUTHORIZED",
                "userId": "another-user",
                "tenantId": "tenant-1",
                "permissionKey": "audit.journey.read",
                "roleKey": "PC",
            },
        )

    with SecurityAuthorizationClient(
        base_url="https://security.test",
        client_id="audit-core",
        client_secret="secret",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(SecurityAuthorizationError):
            client.check_user_permission(
                user_id="user-1",
                tenant_id="tenant-1",
                permission_key="audit.journey.read",
            )
