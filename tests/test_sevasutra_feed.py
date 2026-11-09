"""The read-only service-audit Project feed SevaSutra keeps its dealerships in step with."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_engine
from audit_core.main import app
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.sevasutra_feed import load_service_projects
from audit_core.uc03_pc_booking_documents import require_audit_service_principal


def _as(subject: str) -> None:
    app.dependency_overrides[require_audit_service_principal] = lambda: ServiceIntegrationPrincipal(subject=subject)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _only_this_test_project(monkeypatch):
    # The shared local database holds many leftover test projects; read only ours.
    from audit_core import sevasutra_feed

    monkeypatch.setattr(
        sevasutra_feed,
        "_PROJECTS_SQL",
        text(
            "SELECT p.tenant_id, p.project_code, p.project_name, p.project_status, p.timezone_name,"
            " p.effective_start_date, o.oem_code, o.oem_name FROM auditcore.projects p"
            " LEFT JOIN auditcore.oems o ON o.oem_id = p.oem_id"
            " WHERE p.audit_type = 'SERVICE' AND p.project_status IN ('ACTIVE', 'CONFIGURING')"
            " AND p.tenant_id LIKE 'tenant-seva-%' ORDER BY p.tenant_id"
        ),
    )


@pytest.fixture()
def seeded():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required")
    engine = create_engine(url)
    suffix = uuid4().hex[:10]
    service, sales = f"tenant-seva-{suffix}", f"tenant-seva-s{suffix}"
    with engine.begin() as c:
        cat = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"SC-{suffix}"},
        ).scalar_one()
        oem = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'Mahindra Test') RETURNING oem_id"),
            {"c": f"SO-{suffix}"},
        ).scalar_one()
        for tenant, kind in ((service, "SERVICE"), (sales, "SALES")):
            c.execute(
                text(
                    "INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, product_category_id,"
                    " effective_start_date, audit_type, project_status) VALUES (:t, :c, :n, :o, :k, CURRENT_DATE, :a, 'ACTIVE')"
                ),
                {"t": tenant, "c": f"SP-{kind}-{suffix}", "n": f"{kind.title()} Project", "o": oem, "k": cat, "a": kind},
            )
        dealer = c.execute(
            text("INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) VALUES (:t, :c, 'Shree Test') RETURNING dealer_id"),
            {"t": service, "c": f"SD-{suffix}"},
        ).scalar_one()
        outlet = c.execute(
            text(
                "INSERT INTO auditcore.dealer_outlets (tenant_id, dealer_id, outlet_code, outlet_name, city)"
                " VALUES (:t, :d, :c, 'Rajarhat', 'Kolkata') RETURNING outlet_id"
            ),
            {"t": service, "d": dealer, "c": f"SOU-{suffix}"},
        ).scalar_one()
        for actor, role, with_outlet, status in (
            (f"analyst-{suffix}", "ANALYST", True, "ACTIVE"),
            (f"tl-{suffix}", "TL", False, "ACTIVE"),
            (f"client-{suffix}", "CLIENT", False, "ACTIVE"),
            (f"gone-{suffix}", "ANALYST", True, "INACTIVE"),
        ):
            c.execute(
                text(
                    "INSERT INTO auditcore.business_assignments (tenant_id, security_actor_id, business_role_code, dealer_id,"
                    " outlet_id, assignment_status) VALUES (:t, :a, :r, :d, :o, :s)"
                ),
                {"t": service, "a": actor, "r": role, "d": dealer if with_outlet else None, "o": outlet if with_outlet else None, "s": status},
            )
    yield engine, service, sales, suffix, str(outlet)
    delete_tenant_data(engine, service)
    delete_tenant_data(engine, sales)
    engine.dispose()


def test_only_the_sevasutra_service_identity_may_read(seeded):
    engine, *_ = seeded
    app.dependency_overrides[get_engine] = lambda: engine
    _as("hrmgmt")
    response = TestClient(app).get("/v1/service/sevasutra/projects", headers={"Authorization": "Bearer x"})
    assert response.status_code == 403


def test_returns_service_projects_with_dealers_outlets_and_scopes(seeded):
    engine, service, sales, suffix, outlet = seeded
    app.dependency_overrides[get_engine] = lambda: engine
    _as("sevasutra")
    response = TestClient(app).get("/v1/service/sevasutra/projects", headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    projects = {p["tenantId"]: p for p in response.json()["projects"]}
    assert service in projects and sales not in projects  # sales audits are not SevaSutra's
    p = projects[service]
    assert p["projectName"] == "Service Project" and p["oemName"] == "Mahindra Test"
    assert [d["dealerName"] for d in p["dealers"]] == ["Shree Test"]
    assert [(o["outletName"], o["city"], o["outletId"]) for o in p["outlets"]] == [("Rajarhat", "Kolkata", outlet)]
    by_user = {a["securityUserId"]: a for a in p["assignments"]}
    assert set(by_user) == {f"analyst-{suffix}", f"tl-{suffix}", f"client-{suffix}"}  # inactive left out
    assert by_user[f"analyst-{suffix}"] == {"securityUserId": f"analyst-{suffix}", "roleCode": "ANALYST", "outletIds": [outlet], "dealerIds": []}
    assert by_user[f"client-{suffix}"]["outletIds"] == [] and by_user[f"tl-{suffix}"]["roleCode"] == "TL"


def test_loader_writes_nothing(seeded):
    engine, *_ = seeded
    with engine.connect() as c:
        before = c.execute(text("SELECT count(*) FROM auditcore.business_assignments")).scalar_one()
    load_service_projects(engine)
    with engine.connect() as c:
        after = c.execute(text("SELECT count(*) FROM auditcore.business_assignments")).scalar_one()
    assert before == after
