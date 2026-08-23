from __future__ import annotations

import httpx
import pytest

from audit_core.security_integration import SecurityAdminClient, SecurityAdminError

HUMAN_TOKEN = "security-human-token"


def test_admin_context_forwards_exact_human_bearer_token() -> None:
    observed: dict[str, str] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        observed["authorization"] = request.headers.get("Authorization", "")
        observed["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "userId": "user-123",
                "roles": ["platform.super_admin"],
                "permissions": ["security.tenant.read", "security.tenant.create"],
                "mustChangePassword": False,
            },
        )

    with SecurityAdminClient(
        base_url="https://security.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        context = client.get_admin_context(human_bearer_token=HUMAN_TOKEN)

    assert observed == {
        "authorization": f"Bearer {HUMAN_TOKEN}",
        "path": "/security/v1/platform/me",
    }
    assert context.user_id == "user-123"
    assert context.is_super_admin is True
    assert context.admin_scopes[0].role_key == "platform.super_admin"
    assert context.admin_scopes[0].scope_type == "PLATFORM"
    assert context.admin_scopes[0].scope_id is None


def test_admin_context_does_not_promote_non_super_admin_role() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "userId": "user-123",
                "roles": ["platform.admin"],
                "permissions": ["security.tenant.read"],
                "mustChangePassword": False,
            },
        )

    with SecurityAdminClient(
        base_url="https://security.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        context = client.get_admin_context(human_bearer_token=HUMAN_TOKEN)

    assert context.is_super_admin is False


def test_admin_context_fails_closed_on_security_denial() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("Authorization") == f"Bearer {HUMAN_TOKEN}"
        return httpx.Response(403, json={"code": "PERMISSION_DENIED"})

    with (
        SecurityAdminClient(
            base_url="https://security.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(SecurityAdminError, match="HTTP 403"),
    ):
        client.get_admin_context(human_bearer_token=HUMAN_TOKEN)


def test_admin_context_rejects_untrusted_response_shape() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "userId": "user-123",
                "roles": "platform.super_admin",
                "permissions": [],
            },
        )

    with (
        SecurityAdminClient(
            base_url="https://security.test",
            transport=httpx.MockTransport(handle),
        ) as client,
        pytest.raises(SecurityAdminError, match="invalid shape"),
    ):
        client.get_admin_context(human_bearer_token=HUMAN_TOKEN)
