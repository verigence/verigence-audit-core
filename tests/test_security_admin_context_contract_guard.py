from __future__ import annotations

import httpx

from audit_core.security_integration import SecurityAdminClient


def test_uc02_admin_identity_uses_security_admin_context_contract() -> None:
    """Pin Audit Core UC02 administration to Security DEV's live admin-context API.

    Security and DI both use /security/v1/platform/admin-context for current human
    administrator classification. Keep this guard separate from the client unit test so an
    implementation/test pair cannot drift together to a different endpoint unnoticed.
    """

    observed_paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        observed_paths.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "userId": "guard-user",
                "isSuperAdmin": True,
                "adminScopes": [
                    {"roleKey": "SuperAdmin", "scopeType": "PLATFORM", "scopeId": None}
                ],
            },
        )

    with SecurityAdminClient(
        base_url="https://security.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        context = client.get_admin_context(human_bearer_token="guard-token")

    assert observed_paths == ["/security/v1/platform/admin-context"]
    assert context.is_super_admin is True
