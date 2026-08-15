import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


@pytest.fixture
def customer_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for customer integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-customer-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"CCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Customer OEM') RETURNING oem_id"
            ),
            {"code": f"COEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Customer Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"CP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Customer Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"CD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Customer Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"CO-{suffix}"},
        ).scalar_one()

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="customer-user",
        tenant_id=tenant_id,
        permissions=(),
    )
    try:
        yield tenant_id, dealer_id, outlet_id
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_customer_create_read_update_uses_valid_outlet_hierarchy(customer_setup) -> None:
    tenant_id, dealer_id, outlet_id = customer_setup
    client = TestClient(app, raise_server_exceptions=False)

    created = client.post(
        f"/v1/tenants/{tenant_id}/outlets/{outlet_id}/customers",
        json={
            "customerTypeCode": "RETAIL",
            "displayName": "Customer One",
            "mobileLast4": "1234",
        },
    )
    assert created.status_code == 201
    customer_id = created.json()["customerId"]
    assert created.json()["dealerId"] == str(dealer_id)
    assert created.json()["outletId"] == str(outlet_id)

    listed = client.get(f"/v1/tenants/{tenant_id}/outlets/{outlet_id}/customers")
    assert listed.status_code == 200
    assert listed.json()[0]["customerId"] == customer_id

    detail = client.get(f"/v1/tenants/{tenant_id}/customers/{customer_id}")
    assert detail.status_code == 200

    updated = client.patch(
        f"/v1/tenants/{tenant_id}/customers/{customer_id}",
        json={"displayName": "Customer One Updated", "status": "INACTIVE"},
    )
    assert updated.status_code == 200
    assert updated.json()["displayName"] == "Customer One Updated"
    assert updated.json()["status"] == "INACTIVE"

    missing_outlet = "00000000-0000-0000-0000-000000000001"
    invalid = client.post(
        f"/v1/tenants/{tenant_id}/outlets/{missing_outlet}/customers",
        json={"customerTypeCode": "RETAIL", "displayName": "Invalid"},
    )
    assert invalid.status_code == 404
    assert invalid.json()["errorCode"] == "VAC-NF-003"
