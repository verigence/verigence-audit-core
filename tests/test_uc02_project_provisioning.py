from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core import project_provisioning
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.di_client import DiClientError
from audit_core.main import app
from audit_core.security_integration import (
    SecurityAdminContext,
    SecurityAdminError,
    SecurityTenant,
)


@dataclass
class ControlledSecurityProvisioning:
    tenant_id: str
    tenant_code: str
    tenant_name: str = "UC02 Provisioned Project"
    fail_create: bool = False
    fail_list: bool = False
    create_calls: list[tuple[str, str, str]] = field(default_factory=list)
    list_calls: list[str] = field(default_factory=list)
    timeout_seconds: list[float] = field(default_factory=list)

    def client_class(self):
        controller = self

        class Client:
            def __init__(self, *, base_url: str, timeout_seconds: float = 5.0) -> None:
                assert base_url == "https://security.test"
                controller.timeout_seconds.append(timeout_seconds)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def create_tenant(
                self,
                *,
                human_bearer_token: str,
                tenant_name: str,
                idempotency_key: str,
            ) -> SecurityTenant:
                controller.create_calls.append(
                    (human_bearer_token, tenant_name, idempotency_key)
                )
                if controller.fail_create:
                    raise SecurityAdminError(
                        "Security administrative endpoint is unavailable",
                        http_status=503,
                    )
                controller.tenant_name = tenant_name
                return SecurityTenant(
                    tenant_id=controller.tenant_id,
                    tenant_code=controller.tenant_code,
                    tenant_name=tenant_name,
                    status="CONFIGURING",
                )

            # Retain this method only to prove the optimized Project directory never
            # calls it. `fail_list=True` must not make persisted Projects disappear.
            def list_tenants(self, *, human_bearer_token: str) -> tuple[SecurityTenant, ...]:
                controller.list_calls.append(human_bearer_token)
                if controller.fail_list:
                    raise SecurityAdminError(
                        "Security administrative request failed with HTTP 503",
                        http_status=503,
                    )
                return (
                    SecurityTenant(
                        tenant_id=controller.tenant_id,
                        tenant_code=controller.tenant_code,
                        tenant_name=controller.tenant_name,
                        status="CONFIGURING",
                    ),
                )

        return Client


@dataclass
class ControlledDiProvisioning:
    fail: bool = False
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    def client_class(self):
        controller = self

        class Client:
            def __init__(self, *, base_url: str) -> None:
                assert base_url == "https://di.test"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def ensure_project_provisioning(
                self,
                *,
                human_token: str,
                tenant_id: str,
                idempotency_key: str,
            ) -> dict:
                controller.calls.append((human_token, tenant_id, idempotency_key))
                if controller.fail:
                    raise DiClientError(
                        status_code=503,
                        code="DI_UNAVAILABLE",
                        retryable=False,
                    )
                return {
                    "tenantId": tenant_id,
                    "provisioningStatus": "READY",
                    "checks": [],
                }

        return Client


@dataclass
class ControlledCompensation:
    calls: list[tuple[str, str, bool]] = field(default_factory=list)

    def __call__(
        self,
        *,
        tenant_id: str,
        human_token: str,
        di_cleanup_required: bool,
    ) -> None:
        self.calls.append((tenant_id, human_token, di_cleanup_required))


@pytest.fixture
def provisioning_setup(monkeypatch):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 provisioning integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-provision-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"PROV-CAT-{suffix}", "name": f"Provision Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"PROV-OEM-{suffix}", "name": f"Provision OEM {suffix}"},
        ).scalar_one()

    security = ControlledSecurityProvisioning(
        tenant_id=tenant_id,
        tenant_code=f"PROJ-{suffix[:12]}",
    )
    di = ControlledDiProvisioning()
    compensation = ControlledCompensation()
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    monkeypatch.setenv("DI_BASE_URL", "https://di.test")
    monkeypatch.setattr(project_provisioning, "SecurityAdminClient", security.client_class())
    monkeypatch.setattr(project_provisioning, "DiClient", di.client_class())
    monkeypatch.setattr(project_provisioning, "_compensate_new_project", compensation)
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[require_super_admin_request] = lambda: HumanAdminRequest(
        user_id="superadmin-provision",
        bearer_token="same-human-superadmin-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-provision",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "category_id": str(category_id),
            "oem_id": str(oem_id),
            "security": security,
            "di": di,
            "compensation": compensation,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _payload(setup: dict) -> dict:
    return {
        "projectName": "UC02 Provisioned Project",
        "oemId": setup["oem_id"],
        "productCategoryId": setup["category_id"],
        "effectiveStartDate": "2026-08-21",
        "timezoneName": "Asia/Kolkata",
        "regionCode": "IN-NORTH",
    }


def _project_count(setup: dict) -> int:
    with setup["engine"].begin() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM auditcore.projects WHERE tenant_id=:tenant_id"),
                {"tenant_id": setup["tenant_id"]},
            ).scalar_one()
        )


def _create_project(client: TestClient, setup: dict, key: str):
    return client.post(
        "/v1/projects",
        headers={"Idempotency-Key": key},
        json=_payload(setup),
    )


def test_create_project_is_synchronous_and_persists_only_ready_state(
    provisioning_setup,
) -> None:
    setup = provisioning_setup
    client = TestClient(app, raise_server_exceptions=False)

    response = _create_project(client, setup, "project-create-0001")

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["tenantId"] == setup["tenant_id"]
    assert body["projectStatus"] == "CONFIGURING"
    assert body["provisioningStatus"] == "READY"
    assert body["currentStep"] == "COMPLETE"
    assert body["errorCode"] is None
    assert body["errorMessage"] is None
    assert setup["security"].timeout_seconds == [20.0]
    assert setup["security"].create_calls == [
        (
            "same-human-superadmin-token",
            "UC02 Provisioned Project",
            "project-create-0001",
        )
    ]
    assert setup["di"].calls == [
        (
            "same-human-superadmin-token",
            setup["tenant_id"],
            "project-create-0001",
        )
    ]
    assert setup["compensation"].calls == []
    assert _project_count(setup) == 1


def test_same_idempotency_key_does_not_duplicate_project_projection(provisioning_setup) -> None:
    setup = provisioning_setup
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Idempotency-Key": "project-create-idempotent-0001"}

    first = client.post("/v1/projects", headers=headers, json=_payload(setup))
    second = client.post("/v1/projects", headers=headers, json=_payload(setup))

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["operationId"] == first.json()["operationId"]
    assert _project_count(setup) == 1
    assert setup["compensation"].calls == []


def test_validation_failure_happens_before_security_write(provisioning_setup) -> None:
    setup = provisioning_setup
    payload = _payload(setup)
    payload["effectiveStartDate"] = "2026-08-22"
    payload["effectiveEndDate"] = "2026-08-21"

    response = TestClient(app, raise_server_exceptions=False).post(
        "/v1/projects",
        headers={"Idempotency-Key": "project-create-invalid-0001"},
        json=payload,
    )

    assert response.status_code == 422
    assert setup["security"].create_calls == []
    assert setup["di"].calls == []
    assert setup["compensation"].calls == []
    assert _project_count(setup) == 0


def test_security_failure_returns_error_without_project_or_compensation(
    provisioning_setup,
) -> None:
    setup = provisioning_setup
    setup["security"].fail_create = True

    response = _create_project(
        TestClient(app, raise_server_exceptions=False),
        setup,
        "project-create-security-failure-0001",
    )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "VAC-SYS-002"
    assert "Security" not in response.json()["detail"]
    assert setup["di"].calls == []
    assert setup["compensation"].calls == []
    assert _project_count(setup) == 0


def test_audit_core_failure_rolls_back_and_compensates_security(
    provisioning_setup,
    monkeypatch,
) -> None:
    setup = provisioning_setup

    def fail_projection(*args, **kwargs):
        raise RuntimeError("controlled local projection failure")

    monkeypatch.setattr(project_provisioning, "_ensure_project_projection", fail_projection)

    response = _create_project(
        TestClient(app, raise_server_exceptions=False),
        setup,
        "project-create-local-failure-0001",
    )

    assert response.status_code == 503
    assert setup["di"].calls == []
    assert setup["compensation"].calls == [
        (setup["tenant_id"], "same-human-superadmin-token", False)
    ]
    assert _project_count(setup) == 0


def test_di_failure_rolls_back_project_and_cleans_uncertain_di_then_security(
    provisioning_setup,
) -> None:
    setup = provisioning_setup
    setup["di"].fail = True

    response = _create_project(
        TestClient(app, raise_server_exceptions=False),
        setup,
        "project-create-di-failure-0001",
    )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "VAC-SYS-002"
    assert setup["compensation"].calls == [
        (setup["tenant_id"], "same-human-superadmin-token", True)
    ]
    assert _project_count(setup) == 0


def test_compensation_removes_di_before_security(monkeypatch) -> None:
    monkeypatch.setenv("DI_BASE_URL", "https://di.test")
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    calls: list[str] = []

    monkeypatch.setattr(
        project_provisioning,
        "_delete_di_provisioning",
        lambda **kwargs: calls.append("DI"),
    )
    monkeypatch.setattr(
        project_provisioning,
        "_delete_security_tenant",
        lambda **kwargs: calls.append("SECURITY"),
    )

    project_provisioning._compensate_new_project(
        tenant_id="tenant-atomic-test",
        human_token="token",
        di_cleanup_required=True,
    )
    assert calls == ["DI", "SECURITY"]

    calls.clear()
    project_provisioning._compensate_new_project(
        tenant_id="tenant-atomic-test",
        human_token="token",
        di_cleanup_required=False,
    )
    assert calls == ["SECURITY"]


def test_project_directory_reads_persisted_project_without_security_tenant_listing(
    provisioning_setup,
) -> None:
    setup = provisioning_setup
    client = TestClient(app, raise_server_exceptions=False)
    created = _create_project(client, setup, "project-create-directory-0001")
    assert created.status_code == 201

    # Even a broken Security Tenant directory must not hide an already-persisted
    # Audit Core Project. SuperAdmin was authorized before the route entered.
    setup["security"].fail_list = True
    response = client.get("/v1/projects")

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "tenantId": setup["tenant_id"],
            "projectCode": setup["security"].tenant_code,
            "projectName": "UC02 Provisioned Project",
            "projectStatus": "CONFIGURING",
            "securityTenantStatus": "NOT_QUERIED",
        }
    ]
    assert setup["security"].list_calls == []


def test_empty_project_directory_does_not_call_security_tenant_listing(
    provisioning_setup,
) -> None:
    setup = provisioning_setup
    setup["security"].fail_list = True

    response = TestClient(app, raise_server_exceptions=False).get("/v1/projects")

    assert response.status_code == 200
    assert response.json() == []
    assert setup["security"].list_calls == []


def test_project_provisioning_retry_endpoint_is_not_exposed() -> None:
    response = TestClient(app, raise_server_exceptions=False).post(
        f"/v1/project-provisioning-operations/{uuid4()}/retry"
    )
    assert response.status_code == 404
