from __future__ import annotations

from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from audit_core import role_mappings
from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security_integration import (
    SecurityAdminClient,
    SecurityAdminContext,
    SecurityGlobalUser,
)


def test_security_admin_client_lists_active_users_with_same_human_token() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/security/v1/platform/users"
        assert request.headers["Authorization"] == "Bearer human-superadmin-token"
        assert request.url.params["userStatus"] == "ACTIVE"
        assert request.url.params["search"] == "amit"
        assert request.url.params["limit"] == "25"
        assert request.url.params["offset"] == "0"
        return httpx.Response(
            200,
            json=[
                {
                    "userId": "user-1",
                    "displayName": "Amit User",
                    "primaryEmail": "amit@example.com",
                    "primaryMobile": "+910000000000",
                    "status": "ACTIVE",
                    "clerkSubject": "clerk-secret-to-facade",
                    "onboardingStatus": "ACTIVE",
                    "createdAtUtc": "2026-08-21T00:00:00Z",
                    "updatedAtUtc": "2026-08-21T00:00:00Z",
                }
            ],
        )

    with SecurityAdminClient(
        base_url="https://security.test",
        transport=httpx.MockTransport(handle),
    ) as client:
        users = client.list_global_users(
            human_bearer_token="human-superadmin-token",
            search="amit",
            limit=25,
        )

    assert users == (
        SecurityGlobalUser(
            user_id="user-1",
            display_name="Amit User",
            primary_email="amit@example.com",
            status="ACTIVE",
        ),
    )


def test_role_mapping_candidate_facade_returns_only_approved_selector_fields(
    monkeypatch,
) -> None:
    calls: list[tuple[str, str | None, int]] = []

    class FakeSecurityAdminClient:
        def __init__(self, *, base_url: str) -> None:
            assert base_url == "https://security.test"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback) -> None:
            return None

        def list_global_users(
            self,
            *,
            human_bearer_token: str,
            search: str | None,
            limit: int,
        ) -> tuple[SecurityGlobalUser, ...]:
            calls.append((human_bearer_token, search, limit))
            return (
                SecurityGlobalUser(
                    user_id="user-1",
                    display_name="Amit User",
                    primary_email="amit@example.com",
                    status="ACTIVE",
                ),
            )

    admin_request = HumanAdminRequest(
        user_id="superadmin-1",
        bearer_token="same-human-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-1",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )

    def connection_override():
        yield SimpleNamespace()

    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    monkeypatch.setattr(role_mappings, "SecurityAdminClient", FakeSecurityAdminClient)
    monkeypatch.setattr(role_mappings, "set_tenant_context", lambda connection, tenant_id: None)
    monkeypatch.setattr(role_mappings, "_require_project", lambda connection, tenant_id: None)
    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[require_super_admin_request] = lambda: admin_request
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get(
            "/v1/tenants/tenant-1/role-mapping-candidates?q=amit&limit=25"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "userId": "user-1",
            "displayName": "Amit User",
            "primaryEmail": "amit@example.com",
            "status": "ACTIVE",
        }
    ]
    assert calls == [("same-human-token", "amit", 25)]
