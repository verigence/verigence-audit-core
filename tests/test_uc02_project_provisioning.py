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
from audit_core.security_integration import SecurityAdminContext, SecurityTenant


@dataclass
class ControlledSecurityProvisioning:
    tenant_id: str
    tenant_code: str
    create_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def client_class(self):
        controller = self

        class Client:
            def __init__(self, *, base_url: str) -> None:
                assert base_url == "https://security.test"

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
                return SecurityTenant(
                    tenant_id=controller.tenant_id,
                    tenant_code=controller.tenant_code,
                    tenant_name=tenant_name,
                    status="CONFIGURING",
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
                        retryable=True,
                    )
                return {
                    "tenantId": tenant_id,
                    "provisioningStatus": "READY",
                    "checks": [],
                }

        return Client


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
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    monkeypatch.setenv("DI_BASE_URL", "https://di.test")
    monkeypatch.setattr(project_provisioning, "SecurityAdminClient", security.client_class())
    monkeypatch.setattr(project_provisioning, "DiClient", di.client_class())
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


def test_create_project_runs_security_audit_core_di_once_and_replays_same_operation(
    provisioning_setup,
) -> None:
    setup = provisioning_setup
    client = TestClient(app, raise_server_exceptions=False)

    first = client.post(
        "/v1/projects",
        headers={"Idempotency-Key": "project-create-0001"},
        json=_payload(setup),
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["tenantId"] == setup["tenant_id"]
    assert body["projectStatus"] == "CONFIGURING"
    assert body["provisioningStatus"] == "READY"
    assert body["currentStep"] == "COMPLETE"
    operation_id = body["operationId"]

    second = client.post(
        "/v1/projects",
        headers={"Idempotency-Key": "project-create-0001"},
        json=_payload(setup),
    )
    assert second.status_code == 201
    assert second.json()["operationId"] == operation_id
    assert setup["security"].create_calls == [
        (
            "same-human-superadmin-token",
            "UC02 Provisioned Project",
            "project-create-0001",
        )
    ]
    assert setup["di"].calls == [
        ("same-human-superadmin-token", setup["tenant_id"], "project-create-0001")
    ]

    status = client.get(f"/v1/project-provisioning-operations/{operation_id}")
    assert status.status_code == 200
    assert status.json()["provisioningStatus"] == "READY"

    with setup["engine"].begin() as connection:
        project = connection.execute(
            text(
                """
                SELECT project_code, project_name, project_status
                FROM auditcore.projects WHERE tenant_id=:tenant_id
                """
            ),
            {"tenant_id": setup["tenant_id"]},
        ).mappings().one()
        operation = connection.execute(
            text(
                """
                SELECT status, current_step, security_receipt,
                       audit_core_receipt, di_receipt
                FROM auditcore.administrative_operations
                WHERE operation_id=:operation_id
                """
            ),
            {"operation_id": operation_id},
        ).mappings().one()

    assert project["project_code"] == setup["security"].tenant_code
    assert project["project_name"] == "UC02 Provisioned Project"
    assert project["project_status"] == "CONFIGURING"
    assert operation["status"] == "COMPLETED"
    assert operation["current_step"] == "COMPLETE"
    assert operation["security_receipt"]["tenantId"] == setup["tenant_id"]
    assert operation["audit_core_receipt"]["tenantId"] == setup["tenant_id"]
    assert operation["di_receipt"]["provisioningStatus"] == "READY"


def test_retry_after_di_failure_resumes_without_second_security_tenant(
    provisioning_setup,
) -> None:
    setup = provisioning_setup
    setup["di"].fail = True
    client = TestClient(app, raise_server_exceptions=False)

    failed = client.post(
        "/v1/projects",
        headers={"Idempotency-Key": "project-create-recovery-0001"},
        json=_payload(setup),
    )
    assert failed.status_code == 202, failed.text
    body = failed.json()
    assert body["provisioningStatus"] == "RECOVERY_REQUIRED"
    assert body["currentStep"] == "DI"
    operation_id = body["operationId"]
    assert len(setup["security"].create_calls) == 1

    setup["di"].fail = False
    recovered = client.post(
        f"/v1/project-provisioning-operations/{operation_id}/retry"
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["provisioningStatus"] == "READY"
    assert recovered.json()["currentStep"] == "COMPLETE"
    assert len(setup["security"].create_calls) == 1
    assert len(setup["di"].calls) == 2
    assert all(call[0] == "same-human-superadmin-token" for call in setup["di"].calls)
