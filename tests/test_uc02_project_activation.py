from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core import project_activation
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.readiness import ProjectReadinessResponse, ReadinessCheck
from audit_core.security_integration import (
    SecurityAdminContext,
    SecurityAdminError,
    SecurityTenant,
)


@dataclass
class ControlledSecurityActivation:
    fail: bool = False
    returned_status: str = "ACTIVE"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def client_class(self):
        controller = self

        class Client:
            def __init__(self, *, base_url: str) -> None:
                assert base_url == "https://security.test"

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                return None

            def activate_tenant(
                self,
                *,
                human_bearer_token: str,
                tenant_id: str,
            ) -> SecurityTenant:
                controller.calls.append((human_bearer_token, tenant_id))
                if controller.fail:
                    raise SecurityAdminError(
                        "Security activation unavailable",
                        http_status=503,
                    )
                return SecurityTenant(
                    tenant_id=tenant_id,
                    tenant_code="activation-test",
                    tenant_name="Activation Project",
                    status=controller.returned_status,
                )

        return Client


def _ready_response() -> ProjectReadinessResponse:
    return ProjectReadinessResponse(
        readyToActivate=True,
        evaluatedAtUtc=datetime.now(UTC),
        checks=[
            ReadinessCheck(
                area="PROJECT",
                checkKey="PROJECT_SETUP_COMPLETE",
                severity="BLOCKING",
                status="PASS",
                message="Project setup is complete.",
                targetTask="PROJECT_DETAILS",
            )
        ],
    )


def _blocked_response() -> ProjectReadinessResponse:
    return ProjectReadinessResponse(
        readyToActivate=False,
        evaluatedAtUtc=datetime.now(UTC),
        checks=[
            ReadinessCheck(
                area="ROLE_MAPPING",
                checkKey="ACTIVE_OUTLET_PC_COVERAGE",
                severity="BLOCKING",
                status="FAIL",
                message="PC coverage is incomplete.",
                targetTask="ROLE_MAPPING",
            )
        ],
    )


@pytest.fixture
def activation_setup(monkeypatch):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 activation integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-activate-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"ACT-CAT-{suffix}", "name": f"Activation Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"ACT-OEM-{suffix}", "name": f"Activation OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date, project_status
                ) VALUES (
                    :tenant_id, :code, 'Activation Project', :oem_id,
                    :category_id, CURRENT_DATE, 'CONFIGURING'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"ACT-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )

    admin_request = HumanAdminRequest(
        user_id="superadmin-activate",
        bearer_token="same-human-superadmin-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-activate",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    security = ControlledSecurityActivation()
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    monkeypatch.setattr(project_activation, "SecurityAdminClient", security.client_class())
    app.dependency_overrides[get_engine] = lambda: engine
    app.dependency_overrides[require_super_admin_request] = lambda: admin_request
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "security": security,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _status(engine, tenant_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "SELECT project_status FROM auditcore.projects "
                    "WHERE tenant_id=:tenant_id"
                ),
                {"tenant_id": tenant_id},
            ).scalar_one()
        )


def test_activation_forwards_same_human_token_then_marks_audit_core_active(
    activation_setup,
    monkeypatch,
) -> None:
    setup = activation_setup
    monkeypatch.setattr(project_activation, "evaluate_project_readiness", lambda **_: _ready_response())

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/v1/tenants/{setup['tenant_id']}/project/activate",
        headers={"Idempotency-Key": "activate-project-0001"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["projectStatus"] == "ACTIVE"
    assert response.json()["securityTenantStatus"] == "ACTIVE"
    assert setup["security"].calls == [
        ("same-human-superadmin-token", setup["tenant_id"])
    ]
    assert _status(setup["engine"], setup["tenant_id"]) == "ACTIVE"


def test_readiness_failure_blocks_security_activation_and_local_transition(
    activation_setup,
    monkeypatch,
) -> None:
    setup = activation_setup
    monkeypatch.setattr(
        project_activation,
        "evaluate_project_readiness",
        lambda **_: _blocked_response(),
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/v1/tenants/{setup['tenant_id']}/project/activate",
        headers={"Idempotency-Key": "activate-project-blocked-0001"},
    )

    assert response.status_code == 409
    assert response.json()["errorCode"] == "VAC-CONFLICT-001"
    assert setup["security"].calls == []
    assert _status(setup["engine"], setup["tenant_id"]) == "CONFIGURING"


def test_security_activation_failure_compensates_and_leaves_audit_core_configuring(
    activation_setup,
    monkeypatch,
) -> None:
    setup = activation_setup
    setup["security"].fail = True
    compensation_calls: list[str] = []
    monkeypatch.setattr(project_activation, "evaluate_project_readiness", lambda **_: _ready_response())
    monkeypatch.setattr(
        project_activation,
        "_restore_security_configuring",
        lambda **kwargs: compensation_calls.append(str(kwargs["tenant_id"])),
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/v1/tenants/{setup['tenant_id']}/project/activate",
        headers={"Idempotency-Key": "activate-project-security-fail-0001"},
    )

    assert response.status_code == 503
    assert response.json()["errorCode"] == "VAC-SYS-001"
    assert setup["security"].calls == [
        ("same-human-superadmin-token", setup["tenant_id"])
    ]
    assert compensation_calls == [setup["tenant_id"]]
    assert _status(setup["engine"], setup["tenant_id"]) == "CONFIGURING"


def test_non_active_security_response_is_compensated_and_returns_conflict(
    activation_setup,
    monkeypatch,
) -> None:
    setup = activation_setup
    setup["security"].returned_status = "CONFIGURING"
    compensation_calls: list[str] = []
    monkeypatch.setattr(project_activation, "evaluate_project_readiness", lambda **_: _ready_response())
    monkeypatch.setattr(
        project_activation,
        "_restore_security_configuring",
        lambda **kwargs: compensation_calls.append(str(kwargs["tenant_id"])),
    )

    response = TestClient(app, raise_server_exceptions=False).post(
        f"/v1/tenants/{setup['tenant_id']}/project/activate",
        headers={"Idempotency-Key": "activate-project-not-active-0001"},
    )

    assert response.status_code == 409
    assert response.json()["errorCode"] == "VAC-CONFLICT-001"
    assert compensation_calls == [setup["tenant_id"]]
    assert _status(setup["engine"], setup["tenant_id"]) == "CONFIGURING"
