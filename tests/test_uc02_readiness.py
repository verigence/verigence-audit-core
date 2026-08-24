from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core import readiness
from audit_core.dependencies import HumanAdminRequest, require_project_admin_request
from audit_core.di_client import DiClientError
from audit_core.main import app
from audit_core.security_integration import (
    SecurityAdminContext,
    SecurityAdminError,
    SecurityTenant,
)


@dataclass
class ControlledSecurityReadiness:
    status: str = "CONFIGURING"
    fail_http_status: int | None = None
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

            def get_tenant(
                self,
                *,
                human_bearer_token: str,
                tenant_id: str,
            ) -> SecurityTenant:
                controller.calls.append((human_bearer_token, tenant_id))
                if controller.fail_http_status is not None:
                    raise SecurityAdminError(
                        "Security unavailable",
                        http_status=controller.fail_http_status,
                    )
                return SecurityTenant(
                    tenant_id=tenant_id,
                    tenant_code="tenant-test",
                    tenant_name="Readiness Project",
                    status=controller.status,
                )

        return Client


@dataclass
class ControlledDiReadiness:
    fail: bool = False
    provisioning_status: str = "READY"
    version_states: dict[str, str] = field(
        default_factory=lambda: {
            "DOCUMENT_TYPES": "ACTIVE",
            "EXTRACTION_PROFILES": "PUBLISHED",
            "REQUIREMENT_PROFILES": "PUBLISHED",
        }
    )
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

            def _maybe_fail(self) -> None:
                if controller.fail:
                    raise DiClientError(
                        status_code=503,
                        code="DI_UNAVAILABLE",
                        retryable=True,
                    )

            def get_project_provisioning(
                self,
                *,
                human_token: str,
                tenant_id: str,
            ) -> dict:
                self._maybe_fail()
                controller.calls.append(("provisioning", human_token, tenant_id))
                return {
                    "tenantId": tenant_id,
                    "provisioningStatus": controller.provisioning_status,
                    "checks": [],
                }

            def list_project_masters(
                self,
                *,
                human_token: str,
                tenant_id: str,
            ) -> list[dict]:
                self._maybe_fail()
                controller.calls.append(("catalogue", human_token, tenant_id))
                return [
                    {"masterKey": master_key}
                    for master_key in controller.version_states
                ]

            def list_project_master_versions(
                self,
                *,
                human_token: str,
                tenant_id: str,
                master_key: str,
            ) -> dict:
                self._maybe_fail()
                controller.calls.append((master_key, human_token, tenant_id))
                return {
                    "masterKey": master_key,
                    "versions": [
                        {
                            "versionId": str(uuid4()),
                            "status": controller.version_states[master_key],
                        }
                    ],
                }

        return Client


@pytest.fixture
def readiness_setup(monkeypatch):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 Readiness integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-ready-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"READY-CAT-{suffix}", "name": f"Ready Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"READY-OEM-{suffix}", "name": f"Ready OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date, project_status
                ) VALUES (
                    :tenant_id, :code, 'Readiness Project', :oem_id,
                    :category_id, CURRENT_DATE, 'CONFIGURING'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"READY-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Readiness Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"RD-{suffix}"},
        ).scalar_one()
        outlet_ids = []
        for index, classification in enumerate(("ONSITE", "SATELLITE"), start=1):
            outlet_ids.append(
                connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.dealer_outlets (
                            tenant_id, dealer_id, outlet_code, outlet_name,
                            outlet_classification, address_text, status
                        ) VALUES (
                            :tenant_id, :dealer_id, :code, :name,
                            :classification, 'Manual Address', 'ACTIVE'
                        )
                        RETURNING outlet_id
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "dealer_id": dealer_id,
                        "code": f"RO-{index}-{suffix}",
                        "name": f"Readiness Outlet {index}",
                        "classification": classification,
                    },
                ).scalar_one()
            )

        for index, outlet_id in enumerate(outlet_ids, start=1):
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.business_assignments (
                        tenant_id, security_actor_id, business_role_code,
                        dealer_id, outlet_id, assignment_status
                    ) VALUES (
                        :tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id, 'ACTIVE'
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "actor_id": f"pc-ready-{index}",
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                },
            )

    security = ControlledSecurityReadiness()
    di = ControlledDiReadiness()
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")
    monkeypatch.setenv("DI_BASE_URL", "https://di.test")
    monkeypatch.setattr(readiness, "SecurityAdminClient", security.client_class())
    monkeypatch.setattr(readiness, "DiClient", di.client_class())
    admin_request = HumanAdminRequest(
        user_id="superadmin-ready",
        bearer_token="same-human-superadmin-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-ready",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    app.dependency_overrides[require_project_admin_request] = lambda: admin_request
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "outlet_ids": [str(value) for value in outlet_ids],
            "security": security,
            "di": di,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _checks(payload: dict) -> dict[str, dict]:
    return {check["checkKey"]: check for check in payload["checks"]}


def test_readiness_uses_real_di_states_and_reports_missing_audit_core_masters(
    readiness_setup,
) -> None:
    setup = readiness_setup
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(f"/v1/tenants/{setup['tenant_id']}/project/readiness")

    assert response.status_code == 200, response.text
    payload = response.json()
    checks = _checks(payload)
    assert payload["readyToActivate"] is False
    assert checks["PROJECT_SETUP_COMPLETE"]["status"] == "PASS"
    assert checks["SECURITY_TENANT_LIFECYCLE"]["status"] == "PASS"
    assert checks["DEALER_OUTLET_STRUCTURE"]["status"] == "PASS"
    assert checks["ACTIVE_OUTLET_PC_COVERAGE"]["status"] == "PASS"
    assert checks["PROJECT_MASTERS_READY"]["status"] == "FAIL"
    assert "Product Master" in checks["PROJECT_MASTERS_READY"]["message"]
    assert checks["DI_PROJECT_READY"]["status"] == "PASS"
    assert checks["OPTIONAL_OUTLET_MAP_METADATA"]["severity"] == "WARNING"
    assert checks["OPTIONAL_OUTLET_MAP_METADATA"]["status"] == "FAIL"
    assert setup["security"].calls == [
        ("same-human-superadmin-token", setup["tenant_id"])
    ]
    assert all(call[1] == "same-human-superadmin-token" for call in setup["di"].calls)


def test_missing_pc_coverage_is_blocking_but_satellite_classification_is_not(
    readiness_setup,
) -> None:
    setup = readiness_setup
    with setup["engine"].begin() as connection:
        connection.execute(
            text(
                """
                UPDATE auditcore.business_assignments
                SET assignment_status='INACTIVE', effective_to=now()
                WHERE tenant_id=:tenant_id AND outlet_id=:outlet_id
                """
            ),
            {"tenant_id": setup["tenant_id"], "outlet_id": setup["outlet_ids"][1]},
        )

    response = TestClient(app, raise_server_exceptions=False).get(
        f"/v1/tenants/{setup['tenant_id']}/project/readiness"
    )

    assert response.status_code == 200
    check = _checks(response.json())["ACTIVE_OUTLET_PC_COVERAGE"]
    assert check["severity"] == "BLOCKING"
    assert check["status"] == "FAIL"
    assert "1 active Dealer Outlet" in check["message"]


def test_security_dependency_failure_is_reported_as_pending(readiness_setup) -> None:
    setup = readiness_setup
    setup["security"].fail_http_status = 503

    response = TestClient(app, raise_server_exceptions=False).get(
        f"/v1/tenants/{setup['tenant_id']}/project/readiness"
    )

    assert response.status_code == 200
    check = _checks(response.json())["SECURITY_TENANT_LIFECYCLE"]
    assert check["severity"] == "BLOCKING"
    assert check["status"] == "PENDING"
    assert response.json()["readyToActivate"] is False


def test_missing_security_tenant_is_blocking(readiness_setup) -> None:
    setup = readiness_setup
    setup["security"].fail_http_status = 404

    response = TestClient(app, raise_server_exceptions=False).get(
        f"/v1/tenants/{setup['tenant_id']}/project/readiness"
    )

    assert response.status_code == 200
    check = _checks(response.json())["SECURITY_TENANT_LIFECYCLE"]
    assert check["status"] == "FAIL"
    assert "missing" in check["message"].lower()


def test_di_dependency_failure_is_pending_and_never_ready(readiness_setup) -> None:
    setup = readiness_setup
    setup["di"].fail = True

    response = TestClient(app, raise_server_exceptions=False).get(
        f"/v1/tenants/{setup['tenant_id']}/project/readiness"
    )

    assert response.status_code == 200
    check = _checks(response.json())["DI_PROJECT_READY"]
    assert check["severity"] == "BLOCKING"
    assert check["status"] == "PENDING"
    assert response.json()["readyToActivate"] is False


def test_di_missing_required_published_state_is_blocking(readiness_setup) -> None:
    setup = readiness_setup
    setup["di"].version_states["EXTRACTION_PROFILES"] = "DRAFT"

    response = TestClient(app, raise_server_exceptions=False).get(
        f"/v1/tenants/{setup['tenant_id']}/project/readiness"
    )

    assert response.status_code == 200
    check = _checks(response.json())["DI_PROJECT_READY"]
    assert check["status"] == "FAIL"
    assert "EXTRACTION_PROFILES (PUBLISHED)" in check["message"]
