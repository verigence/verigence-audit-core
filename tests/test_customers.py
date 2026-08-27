import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    get_security_authorization_client,
)


class _AuthorizationClient:
    def __init__(self) -> None:
        self.allowed = {"audit.customer.read", "audit.customer.write"}
        self.role_key = "PC"

    def check_user_permission(
        self,
        *,
        user_id: str,
        tenant_id: str,
        permission_key: str,
    ) -> SecurityAuthorizationDecision:
        return SecurityAuthorizationDecision(
            allowed=permission_key in self.allowed,
            reason_code="ALLOWED" if permission_key in self.allowed else "PERMISSION_DENIED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key=self.role_key,
        )


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

    authorization = _AuthorizationClient()
    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(
        subject="customer-user"
    )
    app.dependency_overrides[get_security_authorization_client] = lambda: authorization
    try:
        yield tenant_id, dealer_id, outlet_id, engine, authorization
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_customer_create_read_update_uses_valid_outlet_hierarchy(customer_setup) -> None:
    tenant_id, dealer_id, outlet_id, _, _ = customer_setup
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
    assert created.json()["mobileNumber"] is None
    assert created.json()["mobileLast4"] == "1234"

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


def test_full_mobile_is_stored_but_masked_without_full_contact_permission(customer_setup) -> None:
    tenant_id, _, outlet_id, engine, authorization = customer_setup
    client = TestClient(app, raise_server_exceptions=False)

    created = client.post(
        f"/v1/tenants/{tenant_id}/outlets/{outlet_id}/customers",
        json={
            "customerTypeCode": "RETAIL",
            "displayName": "Mobile Customer",
            "mobileNumber": "+91 98765 43210",
        },
    )
    assert created.status_code == 201
    body = created.json()
    customer_id = body["customerId"]
    assert body["mobileNumber"] == "******3210"
    assert body["mobileLast4"] == "3210"

    with engine.begin() as connection:
        stored = connection.execute(
            text(
                """
                SELECT mobile_number, mobile_last4
                FROM auditcore.customers
                WHERE tenant_id=:tenant_id AND customer_id=:customer_id
                """
            ),
            {"tenant_id": tenant_id, "customer_id": customer_id},
        ).mappings().one()
    assert stored["mobile_number"] == "+919876543210"
    assert stored["mobile_last4"] == "3210"

    detail = client.get(f"/v1/tenants/{tenant_id}/customers/{customer_id}")
    assert detail.status_code == 200
    assert detail.json()["mobileNumber"] == "******3210"

    authorization.allowed.add("audit.customer.contact.full.read")
    authorization.role_key = "Executive"
    executive_detail = client.get(f"/v1/tenants/{tenant_id}/customers/{customer_id}")
    assert executive_detail.status_code == 200
    assert executive_detail.json()["mobileNumber"] == "+919876543210"
    assert executive_detail.json()["mobileLast4"] == "3210"


def test_full_mobile_patch_derives_last4_and_rejects_mismatch(customer_setup) -> None:
    tenant_id, _, outlet_id, engine, _ = customer_setup
    client = TestClient(app, raise_server_exceptions=False)

    created = client.post(
        f"/v1/tenants/{tenant_id}/outlets/{outlet_id}/customers",
        json={"customerTypeCode": "RETAIL", "displayName": "Patch Customer"},
    )
    assert created.status_code == 201
    customer_id = created.json()["customerId"]

    updated = client.patch(
        f"/v1/tenants/{tenant_id}/customers/{customer_id}",
        json={"mobileNumber": "98765-40001"},
    )
    assert updated.status_code == 200
    assert updated.json()["mobileNumber"] == "******0001"
    assert updated.json()["mobileLast4"] == "0001"

    with engine.begin() as connection:
        stored = connection.execute(
            text(
                "SELECT mobile_number,mobile_last4 FROM auditcore.customers "
                "WHERE tenant_id=:tenant_id AND customer_id=:customer_id"
            ),
            {"tenant_id": tenant_id, "customer_id": customer_id},
        ).mappings().one()
    assert stored["mobile_number"] == "9876540001"
    assert stored["mobile_last4"] == "0001"

    mismatched = client.patch(
        f"/v1/tenants/{tenant_id}/customers/{customer_id}",
        json={"mobileNumber": "9876543210", "mobileLast4": "9999"},
    )
    assert mismatched.status_code == 422
    assert mismatched.json()["errorCode"] == "VAC-VAL-002"
