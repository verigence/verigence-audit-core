from __future__ import annotations

import os
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core import uc02_project_admin_stabilization as stabilization
from audit_core.dependencies import HumanAdminRequest, require_super_admin_request
from audit_core.main import app
from audit_core.security_integration import SecurityAdminContext


@dataclass
class DeleteCalls:
    di: list[str] = field(default_factory=list)
    security: list[str] = field(default_factory=list)


@pytest.fixture
def delete_setup(monkeypatch):
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC02 Project delete integration tests")

    engine = create_engine(database_url)
    calls = DeleteCalls()
    suffix = uuid4().hex

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"DEL-CAT-{suffix}", "name": f"Delete Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"DEL-OEM-{suffix}", "name": f"Delete OEM {suffix}"},
        ).scalar_one()

    admin_request = HumanAdminRequest(
        user_id="superadmin-delete",
        bearer_token="same-human-superadmin-token",
        admin_context=SecurityAdminContext(
            user_id="superadmin-delete",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    app.dependency_overrides[require_super_admin_request] = lambda: admin_request

    monkeypatch.setenv("DI_BASE_URL", "https://di.test")
    monkeypatch.setenv("SECURITY_BASE_URL", "https://security.test")

    def delete_di(*, base_url: str, token: str, tenant_id: str):
        assert base_url == "https://di.test"
        assert token == "same-human-superadmin-token"
        calls.di.append(tenant_id)
        return {
            "tenantId": tenant_id,
            "purgeStatus": "REMOVED",
            "deletedStorageObjects": 0,
        }

    def delete_security(*, base_url: str, token: str, tenant_id: str):
        assert base_url == "https://security.test"
        assert token == "same-human-superadmin-token"
        calls.security.append(tenant_id)

    monkeypatch.setattr(stabilization, "_delete_di_project_data", delete_di)
    monkeypatch.setattr(stabilization, "_delete_security_tenant", delete_security)

    try:
        yield {
            "engine": engine,
            "calls": calls,
            "category_id": category_id,
            "oem_id": oem_id,
            "suffix": suffix,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _create_project(setup: dict, *, status: str, with_journey: bool) -> tuple[str, str]:
    tenant_id = f"tenant-delete-{uuid4().hex}"
    project_name = f"Delete Project {uuid4().hex[:8]}"
    engine = setup["engine"]
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date, project_status
                ) VALUES (
                    :tenant_id, :code, :name, :oem_id,
                    :category_id, CURRENT_DATE, :status
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"DEL-{uuid4().hex[:10]}",
                "name": project_name,
                "oem_id": setup["oem_id"],
                "category_id": setup["category_id"],
                "status": status,
            },
        )
        # Put real setup data behind immutable/published triggers so the purge test
        # proves the SECURITY DEFINER function removes configuration, not only Project.
        policy_version_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.project_policy_versions (
                    tenant_id, version_no, lifecycle_status, effective_from,
                    policy_settings, published_at_utc
                ) VALUES (
                    :tenant_id, 1, 'PUBLISHED', CURRENT_DATE,
                    '{"deleteTest": true}'::jsonb, now()
                ) RETURNING policy_version_id
                """
            ),
            {"tenant_id": tenant_id},
        ).scalar_one()
        assert policy_version_id is not None

        if with_journey:
            dealer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                    VALUES (:tenant_id, :code, 'Delete Dealer')
                    RETURNING dealer_id
                    """
                ),
                {"tenant_id": tenant_id, "code": f"DD-{uuid4().hex[:10]}"},
            ).scalar_one()
            outlet_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealer_outlets (
                        tenant_id, dealer_id, outlet_code, outlet_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :code, 'Delete Outlet'
                    ) RETURNING outlet_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "code": f"DO-{uuid4().hex[:10]}",
                },
            ).scalar_one()
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Delete Customer'
                    ) RETURNING customer_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                },
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journeys (
                        tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, :customer_id, :reference
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "customer_id": customer_id,
                    "reference": f"J-{uuid4().hex[:12]}",
                },
            )
    return tenant_id, project_name


def test_active_project_with_zero_journeys_can_be_hard_deleted(delete_setup) -> None:
    setup = delete_setup
    tenant_id, project_name = _create_project(setup, status="ACTIVE", with_journey=False)
    client = TestClient(app, raise_server_exceptions=False)

    impact = client.get(f"/v1/tenants/{tenant_id}/project/deletion-impact")
    assert impact.status_code == 200, impact.text
    assert impact.json()["projectStatus"] == "ACTIVE"
    assert impact.json()["journeyCount"] == 0
    assert impact.json()["canDelete"] is True

    response = client.request(
        "DELETE",
        f"/v1/tenants/{tenant_id}/project",
        headers={"Idempotency-Key": f"delete-{uuid4()}"},
        json={"confirmProjectName": project_name},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["deletionStatus"] == "COMPLETED"
    assert payload["projectStatus"] == "ACTIVE"
    assert payload["journeyCount"] == 0
    assert setup["calls"].di == [tenant_id]
    assert setup["calls"].security == [tenant_id]

    with setup["engine"].begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM auditcore.projects WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one() == 0
        assert connection.execute(
            text(
                "SELECT count(*) FROM auditcore.project_policy_versions "
                "WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).scalar_one() == 0
        receipt = connection.execute(
            text(
                """
                SELECT status, security_receipt, di_receipt, audit_core_receipt
                FROM auditcore.administrative_operations
                WHERE tenant_id=:tenant_id AND operation_type='PROJECT_DELETE'
                ORDER BY created_at_utc DESC
                LIMIT 1
                """
            ),
            {"tenant_id": tenant_id},
        ).mappings().one()
        assert receipt["status"] == "COMPLETED"
        assert receipt["security_receipt"] is not None
        assert receipt["di_receipt"] is not None
        assert receipt["audit_core_receipt"] is not None


def test_project_with_any_journey_is_blocked_before_cross_module_cleanup(delete_setup) -> None:
    setup = delete_setup
    tenant_id, project_name = _create_project(
        setup,
        status="CONFIGURING",
        with_journey=True,
    )
    client = TestClient(app, raise_server_exceptions=False)

    impact = client.get(f"/v1/tenants/{tenant_id}/project/deletion-impact")
    assert impact.status_code == 200, impact.text
    assert impact.json()["journeyCount"] == 1
    assert impact.json()["canDelete"] is False

    response = client.request(
        "DELETE",
        f"/v1/tenants/{tenant_id}/project",
        headers={"Idempotency-Key": f"delete-{uuid4()}"},
        json={"confirmProjectName": project_name},
    )
    assert response.status_code == 409, response.text
    assert "Journey" in response.text
    assert setup["calls"].di == []
    assert setup["calls"].security == []

    with setup["engine"].begin() as connection:
        assert connection.execute(
            text("SELECT count(*) FROM auditcore.projects WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one() == 1
        assert connection.execute(
            text("SELECT count(*) FROM auditcore.journeys WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).scalar_one() == 1
