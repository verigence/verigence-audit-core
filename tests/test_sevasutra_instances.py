"""A Project's SevaSutra instance: asked for in Project Administration, reported back by SevaSutra."""
from __future__ import annotations

import os
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_project_admin_request,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security import ServiceIntegrationPrincipal
from audit_core.security_integration import SecurityAdminContext
from audit_core.sevasutra_feed import load_service_projects
from audit_core.uc03_pc_booking_documents import require_audit_service_principal

H = {"Authorization": "Bearer x"}


def _admin(super_admin: bool) -> None:
    app.dependency_overrides[require_project_admin_request] = lambda: HumanAdminRequest(
        user_id="admin-1",
        bearer_token="x",
        admin_context=SecurityAdminContext(user_id="admin-1", is_super_admin=super_admin, admin_scopes=()),
    )


def _service(subject: str) -> None:
    app.dependency_overrides[require_audit_service_principal] = lambda: ServiceIntegrationPrincipal(subject=subject)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    app.dependency_overrides.clear()


@pytest.fixture()
def projects():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is required")
    engine = create_engine(url)
    suffix = uuid4().hex[:10]
    service, other, sales = f"tenant-seva-{suffix}", f"tenant-seva-o{suffix}", f"tenant-seva-s{suffix}"
    with engine.begin() as c:
        cat = c.execute(
            text("INSERT INTO auditcore.product_categories (category_code, category_name) VALUES (:c, 'V') RETURNING product_category_id"),
            {"c": f"SC-{suffix}"},
        ).scalar_one()
        oem = c.execute(
            text("INSERT INTO auditcore.oems (oem_code, oem_name) VALUES (:c, 'Mahindra Test') RETURNING oem_id"), {"c": f"SO-{suffix}"}
        ).scalar_one()
        for tenant, kind, code in ((service, "SERVICE", "A"), (other, "SERVICE", "B"), (sales, "SALES", "C")):
            c.execute(
                text(
                    "INSERT INTO auditcore.projects (tenant_id, project_code, project_name, oem_id, product_category_id,"
                    " effective_start_date, audit_type, project_status) VALUES (:t, :c, :n, :o, :k, CURRENT_DATE, :a, 'CONFIGURING')"
                ),
                {"t": tenant, "c": f"SP-{code}-{suffix}", "n": f"Project {code}", "o": oem, "k": cat, "a": kind},
            )
    app.dependency_overrides[get_engine] = lambda: engine
    yield engine, service, other, sales, suffix
    for t in (service, other, sales):
        delete_tenant_data(engine, t)
    engine.dispose()


def test_only_a_super_admin_creates_an_instance(projects):
    _, service, *_ = projects
    _admin(False)
    r = TestClient(app).put(f"/v1/tenants/{service}/sevasutra-instance", json={"dealershipName": "Aditya Motors", "subdomain": "aditya"}, headers=H)
    assert r.status_code == 403


def test_create_read_and_change_an_instance(projects):
    engine, service, other, sales, suffix = projects
    _admin(True)
    client = TestClient(app)
    assert client.get(f"/v1/tenants/{service}/sevasutra-instance", headers=H).json() == {"instance": None}
    sub = f"a{suffix}"
    r = client.put(f"/v1/tenants/{service}/sevasutra-instance", json={"dealershipName": " Aditya Motors ", "subdomain": sub.upper()}, headers=H)
    assert r.status_code == 200, r.text
    inst = r.json()["instance"]
    assert (inst["dealershipName"], inst["subdomain"], inst["status"]) == ("Aditya Motors", sub, "REQUESTED")

    # The same address cannot go to a second Project.
    r = client.put(f"/v1/tenants/{other}/sevasutra-instance", json={"dealershipName": "Other", "subdomain": sub}, headers=H)
    assert r.status_code == 409
    # Nor to a sales audit, nor a malformed or reserved one.
    assert client.put(f"/v1/tenants/{sales}/sevasutra-instance", json={"dealershipName": "Sales", "subdomain": f"s{suffix}"}, headers=H).status_code == 422
    assert client.put(f"/v1/tenants/{other}/sevasutra-instance", json={"dealershipName": "Other", "subdomain": "-bad-"}, headers=H).status_code == 422
    assert client.put(f"/v1/tenants/{other}/sevasutra-instance", json={"dealershipName": "Other", "subdomain": "www"}, headers=H).status_code == 422

    # SevaSutra reports it live; renaming keeps the status, a new address asks again.
    _service("sevasutra")
    r = client.post(
        "/v1/service/sevasutra/instances",
        json={"instances": [{"tenantId": service, "dealershipName": "Aditya Motors", "dealerCode": "aditya-motors", "liveHostname": f"{sub}.sevasutra.jbrconsulting.in", "status": "LIVE"}]},
        headers=H,
    )
    assert r.json() == {"updated": 1}
    inst = client.get(f"/v1/tenants/{service}/sevasutra-instance", headers=H).json()["instance"]
    assert inst["status"] == "LIVE" and inst["dealerCode"] == "aditya-motors" and inst["reportedAtUtc"]
    inst = client.put(f"/v1/tenants/{service}/sevasutra-instance", json={"dealershipName": "Aditya Motors Pvt", "subdomain": sub}, headers=H).json()["instance"]
    assert (inst["dealershipName"], inst["status"]) == ("Aditya Motors Pvt", "LIVE")
    inst = client.put(f"/v1/tenants/{service}/sevasutra-instance", json={"dealershipName": "Aditya Motors Pvt", "subdomain": f"b{suffix}"}, headers=H).json()["instance"]
    assert inst["status"] == "REQUESTED" and inst["statusMessage"] is None

    # The feed carries it to SevaSutra.
    feed = {p.tenantId: p for p in load_service_projects(engine)}
    assert feed[service].instance is not None and feed[service].instance.subdomain == f"b{suffix}"
    assert feed[other].instance is None


def test_report_records_a_dealership_linked_before_instances(projects):
    _, service, *_ = projects
    _admin(True)
    _service("sevasutra")
    client = TestClient(app)
    body = {"instances": [
        {"tenantId": service, "dealershipName": "Shree Motors", "dealerCode": "shree-motors", "liveHostname": None, "status": "CREATED"},
        {"tenantId": "tenant-does-not-exist", "dealershipName": "X", "dealerCode": "x", "status": "CREATED"},
    ]}
    assert client.post("/v1/service/sevasutra/instances", json=body, headers=H).json() == {"updated": 1}
    inst = client.get(f"/v1/tenants/{service}/sevasutra-instance", headers=H).json()["instance"]
    assert (inst["dealershipName"], inst["status"], inst["subdomain"]) == ("Shree Motors", "CREATED", None)


def test_only_sevasutra_reports_instances(projects):
    _service("hrmgmt")
    r = TestClient(app).post("/v1/service/sevasutra/instances", json={"instances": []}, headers=H)
    assert r.status_code == 403


def _version(engine, tenant: str) -> int:
    with engine.connect() as c:
        return c.execute(text("SELECT version_no FROM auditcore.projects WHERE tenant_id = :t"), {"t": tenant}).scalar_one()


def test_audit_type_is_set_by_a_super_admin(projects):
    engine, _, _, sales, _ = projects
    client = TestClient(app)
    _admin(False)
    r = client.patch(f"/v1/tenants/{sales}/project", json={"auditType": "SERVICE"}, headers={**H, "If-Match": f'"{_version(engine, sales)}"'})
    assert r.status_code == 403
    _admin(True)
    r = client.patch(f"/v1/tenants/{sales}/project", json={"auditType": "SERVICE"}, headers={**H, "If-Match": f'"{_version(engine, sales)}"'})
    assert r.status_code == 200, r.text
    assert r.json()["auditType"] == "SERVICE"


def test_a_project_with_an_instance_stays_a_service_audit(projects):
    engine, service, _, _, suffix = projects
    _admin(True)
    client = TestClient(app)
    client.put(f"/v1/tenants/{service}/sevasutra-instance", json={"dealershipName": "Aditya", "subdomain": f"k{suffix}"}, headers=H)
    r = client.patch(f"/v1/tenants/{service}/project", json={"auditType": "SALES"}, headers={**H, "If-Match": f'"{_version(engine, service)}"'})
    assert r.status_code == 422


def test_attach_an_existing_sevasutra_dealership(projects):
    engine, service, other, _, suffix = projects
    client = TestClient(app)
    demo = f"demo-{suffix}"
    _service("sevasutra")
    r = client.post(
        "/v1/service/sevasutra/instances",
        json={"instances": [], "unlinked": [{"dealerCode": demo, "dealershipName": "Premium Motors", "outlets": 1, "visits": 85}]},
        headers=H,
    )
    assert r.status_code == 200
    _admin(True)
    app.dependency_overrides[require_super_admin_request] = app.dependency_overrides[require_project_admin_request]
    free = client.get("/v1/sevasutra/free-dealerships", headers=H).json()["dealerships"]
    assert {"dealerCode": demo, "dealershipName": "Premium Motors", "outlets": 1, "visits": 85}.items() <= next(f for f in free if f["dealerCode"] == demo).items()
    # Not one SevaSutra does not list.
    r = client.put(f"/v1/tenants/{other}/sevasutra-instance", json={"dealershipName": "Other", "existingDealerCode": "nope"}, headers=H)
    assert r.status_code == 422
    r = client.put(f"/v1/tenants/{service}/sevasutra-instance", json={"dealershipName": "Premium Motors", "existingDealerCode": demo}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["instance"]["dealerCode"] == demo and r.json()["instance"]["status"] == "REQUESTED"
    feed = {p.tenantId: p for p in load_service_projects(engine)}
    assert feed[service].instance.dealerCode == demo
    with engine.begin() as c:
        c.execute(text("DELETE FROM auditcore.sevasutra_free_dealerships WHERE dealer_code = :c"), {"c": demo})
