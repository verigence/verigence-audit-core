from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_engine
from audit_core.hr_work_context import load_work_context
from audit_core.main import app
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.uc03_pc_booking_documents import require_audit_service_principal
from conftest import delete_tenant_data


def _as(subject: str) -> None:
    app.dependency_overrides[require_audit_service_principal] = lambda: (
        ServiceIntegrationPrincipal(subject=subject)
    )


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _only_this_test_project(monkeypatch):
    # The shared local database holds thousands of leftover test projects; read only ours.
    from audit_core import hr_work_context

    monkeypatch.setattr(
        hr_work_context,
        "_PROJECTS_SQL",
        text(
            "SELECT tenant_id, project_code, project_name FROM auditcore.projects"
            " WHERE project_status = 'ACTIVE' AND tenant_id LIKE 'tenant-hrctx-%' ORDER BY tenant_id"
        ),
    )


@pytest.fixture()
def seeded():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required")
    engine = create_engine(url)
    suffix = uuid4().hex[:10]
    tenant = f"tenant-hrctx-{suffix}"
    with engine.begin() as c:
        cat = c.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:c, 'V') RETURNING product_category_id"
            ),
            {"c": f"HC-{suffix}"},
        ).scalar_one()
        oem = c.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'O') RETURNING oem_id"
            ),
            {"c": f"HO-{suffix}"},
        ).scalar_one()
        c.execute(
            text(
                "INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id,"
                " product_category_id, effective_start_date) VALUES (:t, :c, 'HR Context Project',"
                " :o, :k, CURRENT_DATE)"
            ),
            {"t": tenant, "c": f"HP-{suffix}", "o": oem, "k": cat},
        )
        dealer = c.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)"
                " VALUES (:t, :c, 'Ctx Dealer') RETURNING dealer_id"
            ),
            {"t": tenant, "c": f"HD-{suffix}"},
        ).scalar_one()
        outlet = c.execute(
            text(
                "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name,"
                " latitude, longitude) VALUES (:t, :d, :c, 'Ctx Outlet', 20.462500, 85.882800)"
                " RETURNING outlet_id"
            ),
            {"t": tenant, "d": dealer, "c": f"HOU-{suffix}"},
        ).scalar_one()
        for actor, role, with_outlet, status in (
            (f"pc-{suffix}", "PC", True, "ACTIVE"),
            (f"tl-{suffix}", "TL", False, "ACTIVE"),
            (f"old-{suffix}", "PC", True, "INACTIVE"),
        ):
            c.execute(
                text(
                    "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id,"
                    " business_role_code, dealer_id, outlet_id, assignment_status)"
                    " VALUES (:t, :a, :r, :d, :o, :s)"
                ),
                {
                    "t": tenant,
                    "a": actor,
                    "r": role,
                    "d": dealer if with_outlet else None,
                    "o": outlet if with_outlet else None,
                    "s": status,
                },
            )
    yield engine, tenant, suffix
    delete_tenant_data(engine, tenant)
    engine.dispose()


def test_only_the_hr_service_identity_may_read(seeded):
    engine, _, _ = seeded
    app.dependency_overrides[get_engine] = lambda: engine
    _as("verigence-di")
    response = TestClient(app).get(
        "/v1/service/hr/work-context", headers={"Authorization": "Bearer x"}
    )
    assert response.status_code == 403


def test_returns_active_assignments_with_outlet_coordinates(seeded):
    engine, tenant, suffix = seeded
    app.dependency_overrides[get_engine] = lambda: engine
    _as("hrmgmt")
    response = TestClient(app).get(
        "/v1/service/hr/work-context", headers={"Authorization": "Bearer x"}
    )
    assert response.status_code == 200
    mine = [a for a in response.json()["assignments"] if a["tenantId"] == tenant]
    by_user = {a["securityUserId"]: a for a in mine}
    assert set(by_user) == {
        f"pc-{suffix}",
        f"tl-{suffix}",
    }  # the inactive one is not returned
    pc = by_user[f"pc-{suffix}"]
    assert pc["roleCode"] == "PC" and pc["projectName"] == "HR Context Project"
    assert (pc["latitude"], pc["longitude"]) == (20.4625, 85.8828)
    assert pc["outletName"] == "Ctx Outlet" and pc["dealerName"] == "Ctx Dealer"
    tl = by_user[f"tl-{suffix}"]
    assert tl["roleCode"] == "TL" and tl["outletId"] is None and tl["latitude"] is None


def test_loader_writes_nothing(seeded):
    engine, _tenant, _ = seeded
    with engine.connect() as c:
        before = c.execute(
            text("SELECT count(*) FROM auditcore.business_assignments")
        ).scalar_one()
    load_work_context(engine)
    with engine.connect() as c:
        after = c.execute(
            text("SELECT count(*) FROM auditcore.business_assignments")
        ).scalar_one()
    assert before == after
