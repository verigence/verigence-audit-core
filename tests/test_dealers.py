import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import (
    HumanAdminRequest,
    get_connection,
    require_project_admin_request,
)
from audit_core.main import app
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
    app.dependency_overrides[require_project_admin_request] = lambda: admin_request
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
            "outletCode": "CALLER-MUST-NOT-CONTROL",
            "outletName": "Outlet One",
            "outletClassification": "ONSITE",
            "addressText": "Baner Road",
            "city": "Pune",
            "stateRegion": "Maharashtra",
            "postalCode": "411045",
            "googlePlaceId": "place-123",
            "latitude": "18.5590",
            "longitude": "73.7868",
            "monthlyVehicleVolume": 250,
        },
    )
    assert outlet.status_code == 201
    assert outlet.headers["etag"] == '"1"'
    outlet_payload = outlet.json()
    outlet_id = outlet_payload["outletId"]
    assert outlet_payload["outletCode"] != "CALLER-MUST-NOT-CONTROL"
    assert outlet_payload["googlePlaceId"] == "place-123"
    assert outlet_payload["addressText"] == "Baner Road"
    assert outlet_payload["monthlyVehicleVolume"] == 250
    assert outlet_payload["versionNo"] == 1

    outlet_detail = client.get(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}"
    )
    assert outlet_detail.status_code == 200
    assert outlet_detail.headers["etag"] == '"1"'
    assert outlet_detail.json()["dealerId"] == dealer_id

    outlet_patch = client.patch(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}",
        headers={"If-Match": outlet_detail.headers["etag"]},
        json={
            "addressText": "Manual Address Updated",
            "googlePlaceId": None,
            "latitude": None,
            "longitude": None,
            "status": "INACTIVE",
        },
    )
    assert outlet_patch.status_code == 200
    assert outlet_patch.headers["etag"] == '"2"'
    patched = outlet_patch.json()
    assert patched["status"] == "INACTIVE"
    assert patched["addressText"] == "Manual Address Updated"
    assert patched["googlePlaceId"] is None
    assert patched["latitude"] is None
    assert patched["longitude"] is None
    assert patched["versionNo"] == 2

    stale_outlet = client.patch(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}",
        headers={"If-Match": '"1"'},
        json={"outletName": "Stale Outlet"},
    )
    assert stale_outlet.status_code == 409
    assert stale_outlet.json()["errorCode"] == "VAC-CONFLICT-001"


def test_manual_outlet_address_does_not_require_maps(dealer_setup) -> None:
    tenant_id = dealer_setup
    client = TestClient(app, raise_server_exceptions=False)

    dealer = client.post(
        f"/v1/tenants/{tenant_id}/dealers",
        json={"dealerName": "Manual Address Dealer"},
    )
    dealer_id = dealer.json()["dealerId"]

    outlet = client.post(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets",
        json={
            "outletName": "Manual Outlet",
            "addressText": "MG Road",
            "city": "Bengaluru",
        },
    )

    assert outlet.status_code == 201
    assert outlet.json()["addressText"] == "MG Road"
    assert outlet.json()["googlePlaceId"] is None
    assert outlet.json()["latitude"] is None
    assert outlet.json()["longitude"] is None


def test_outlet_requires_matching_dealer_hierarchy(dealer_setup) -> None:
    tenant_id = dealer_setup
    client = TestClient(app, raise_server_exceptions=False)
    missing_dealer_id = "00000000-0000-0000-0000-000000000001"

    response = client.post(
        f"/v1/tenants/{tenant_id}/dealers/{missing_dealer_id}/outlets",
        json={"outletName": "Invalid Outlet"},
    )

    assert response.status_code == 404
    assert response.json()["errorCode"] == "VAC-NF-002"


def test_outlet_hard_delete_is_preflighted_and_idempotent(dealer_setup) -> None:
    tenant_id = dealer_setup
    client = TestClient(app, raise_server_exceptions=False)

    dealer = client.post(
        f"/v1/tenants/{tenant_id}/dealers",
        json={"dealerName": "Delete Outlet Dealer"},
    )
    dealer_id = dealer.json()["dealerId"]
    outlet = client.post(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets",
        json={"outletName": "Delete Me"},
    )
    outlet_id = outlet.json()["outletId"]
    path = f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}"

    impact = client.get(f"{path}/deletion-impact")
    assert impact.status_code == 200
    assert impact.json()["canDelete"] is True
    assert all(count == 0 for count in impact.json()["dependencies"].values())

    headers = {"Idempotency-Key": f"outlet-delete-{uuid4().hex}"}
    deleted = client.delete(path, headers=headers)
    assert deleted.status_code == 204
    replay = client.delete(path, headers=headers)
    assert replay.status_code == 204
    assert client.get(path).status_code == 404


def test_outlet_hard_delete_rejects_business_assignment_dependency(dealer_setup) -> None:
    tenant_id = dealer_setup
    client = TestClient(app, raise_server_exceptions=False)

    dealer = client.post(
        f"/v1/tenants/{tenant_id}/dealers",
        json={"dealerName": "Assigned Dealer"},
    )
    dealer_id = dealer.json()["dealerId"]
    outlet = client.post(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets",
        json={"outletName": "Assigned Outlet"},
    )
    outlet_id = outlet.json()["outletId"]

    engine = create_engine(os.environ["DATABASE_URL"])
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.business_assignments (
                        tenant_id, security_actor_id, business_role_code,
                        dealer_id, outlet_id
                    ) VALUES (
                        :tenant_id, 'pc-delete-test', 'PC', :dealer_id, :outlet_id
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                },
            )
    finally:
        engine.dispose()

    path = f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets/{outlet_id}"
    impact = client.get(f"{path}/deletion-impact")
    assert impact.status_code == 200
    assert impact.json()["canDelete"] is False
    assert impact.json()["dependencies"]["businessAssignments"] == 1

    deleted = client.delete(
        path,
        headers={"Idempotency-Key": f"blocked-outlet-delete-{uuid4().hex}"},
    )
    assert deleted.status_code == 422
    assert deleted.json()["errorCode"] == "VAC-VAL-002"
    assert client.get(path).status_code == 200


def test_dealer_hard_delete_rejects_outlet_then_deletes_when_empty(dealer_setup) -> None:
    tenant_id = dealer_setup
    client = TestClient(app, raise_server_exceptions=False)

    dealer = client.post(
        f"/v1/tenants/{tenant_id}/dealers",
        json={"dealerName": "Dealer Delete Test"},
    )
    dealer_id = dealer.json()["dealerId"]
    outlet = client.post(
        f"/v1/tenants/{tenant_id}/dealers/{dealer_id}/outlets",
        json={"outletName": "Temporary Outlet"},
    )
    outlet_id = outlet.json()["outletId"]

    dealer_path = f"/v1/tenants/{tenant_id}/dealers/{dealer_id}"
    impact = client.get(f"{dealer_path}/deletion-impact")
    assert impact.status_code == 200
    assert impact.json()["canDelete"] is False
    assert impact.json()["dependencies"]["outlets"] == 1

    blocked = client.delete(
        dealer_path,
        headers={"Idempotency-Key": f"blocked-dealer-{uuid4().hex}"},
    )
    assert blocked.status_code == 422
    assert blocked.json()["errorCode"] == "VAC-VAL-002"

    outlet_path = f"{dealer_path}/outlets/{outlet_id}"
    assert (
        client.delete(
            outlet_path,
            headers={"Idempotency-Key": f"clear-outlet-{uuid4().hex}"},
        ).status_code
        == 204
    )

    impact_after = client.get(f"{dealer_path}/deletion-impact")
    assert impact_after.status_code == 200
    assert impact_after.json()["canDelete"] is True

    headers = {"Idempotency-Key": f"dealer-delete-{uuid4().hex}"}
    assert client.delete(dealer_path, headers=headers).status_code == 204
    assert client.delete(dealer_path, headers=headers).status_code == 204
    assert client.get(dealer_path).status_code == 404
