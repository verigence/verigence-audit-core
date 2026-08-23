from __future__ import annotations

import httpx

from audit_core.security_integration import SecurityAdminClient


def test_uc02_admin_identity_uses_security_platform_me_contract() -> None:
    """Regression guard: UC02 must use Security's canonical Platform /me endpoint.

    This deliberately lives outside test_uc02_admin_security.py so a feature merge that
    accidentally restores the retired admin-context implementation/test pair still fails CI.
    """

    observed_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        observed_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "userId": "guard-user",
                "roles": ["platform.super_admin"],
                "permissions": ["security.tenant.read"],
                "mustChangePassword": False,
            },
        )

    with SecurityAdminClient(
        base_url="https://security.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        context = client.get_admin_context(human_bearer_token="guard-token")

    assert observed_paths == ["/security/v1/platform/me"]
    assert context.is_super_admin is True
