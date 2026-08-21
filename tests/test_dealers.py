import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    get_principal,
    require_super_admin_request,
)
from audit_core.main import app
from audit_core.security import Principal
from audit_core.security_integration import SecurityAdminContext


@pytest.fixture
def dealer_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for dealer integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-dealer-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name) RETURNING product_category_id
                """
            ),
            {"code": f"DCAT-{suffix}", "name": f"Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name) RETURNING oem_id
                """
            ),
            {"code": f"DOEM-{suffix}", "name": f"OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :project_code, 'Dealer Test Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"DP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    admin_request = HumanAdminRequest(
        user_id="dealer-admin",
        bearer_token="test-human-token",
        admin_context=SecurityAdminContext(
            user_id="dealer-admin",
            is_super_admin=True,
            admin_scopes=(),
        ),
    )
    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[require_super_admin_request] = lambda: admin_request
    # Outlet routes are converted to the UC02 human-admin boundary in the next package.
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="dealer-user",
        tenant_id=tenant_id,
        permissions=(),
    )
    try:
        yield tenant_id
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_dealer_and_outlet_create_read_update_inactivate(dealer_setup) -> None:
    tenant_id = dealer_setup
    client = TestClient(app, raise_server_exceptions=False)

    dealer = client.post(
        f"/v1/tenants/{tenant_id}/dealers",
        json={"dealerCode": "CALLER-MUST-NOT-CONTROL", "dealerName": "Dealer One"},
    )
    assert dealer.status_code == 201
    assert dealer.headers["etag"] == '"1"'
    dealer_payload = dealer.json()
    dealer_id = dealer_payload["dealerId"]
    assert dealer_payload["dealerCode"] != "CALLER-MUST-NOT-CONTROL"
    assert dealer_payload["versionNo"] == 1

    detail = client.get(f"/v1/tenants/{tenant_id}/dealers/{dealer_id}")
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"1"'
    assert detail.json()["dealerId"] == dealer_id
    assert client.get(f"/v1/tenants/{tenant_id}/dealers").json()[0]["dealerId"] == dealer_id

    dealer_patch = client.patch(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}",
        headers={"If-Match": detail.headers["etag"]},
        json={"dealerName": "Dealer One Updated", "status": "INACTIVE"},
    )
    assert dealer_patch.status_code == 200
    assert dealer_patch.headers["etag"] == '"2"'
    assert dealer_patch.json()["status"] == "INACTIVE"
    assert dealer_patch.json()["versionNo"] == 2

    stale = client.patch(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}",
        headers={"If-Match": '"1"'},
        json={"dealerName": "Stale Dealer"},
    )
    assert stale.status_code == 409
    assert stale.json()["errorCode"] == "VAC-CONFLICT-001"

    outlet = client.post(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets",
        json={
            "outletCode": "O001",
            "outletName": "Outlet One",
            "outletClassification": "ONSITE",
            "city": "Pune",
        },
    )
    assert outlet.status_code == 201
    outlet_id = outlet.json()["outletId"]

    outlet_detail = client.get(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}"
    )
    assert outlet_detail.status_code == 200
    assert outlet_detail.json()["dealerId"] == dealer_id

    outlet_patch = client.patch(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}",
        json={"status": "INACTIVE"},
    )
    assert outlet_patch.status_code == 200
    assert outlet_patch.json()["status"] == "INACTIVE"

    assert client.delete(f"/v1/tenants/{tenant_id}/dealers/{dealer_id}").status_code == 405
    assert (
        client.delete(
            f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}"
        ).status_code
        == 405
    )


def test_outlet_requires_matching_dealer_hierarchy(dealer_setup) -> None:
    tenant_id = dealer_setup
    client = TestClient(app, raise_server_exceptions=False)
    missing_dealer_id = "00000000-0000-0000-0000-000000000001"

    response = client.post(
        f"/v1/tenants/{tenant_id}/dealers/{missing_dealer_id}/outlets",
        json={"outletCode": "O404", "outletName": "Invalid Outlet"},
    )

    assert response.status_code == 404
    assert response.json()["errorCode"] == "VAC-NF-002"
