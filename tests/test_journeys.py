import os
from datetime import date
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal
from audit_core.versioned_masters import (
    create_document_profile,
    create_document_profile_version,
    create_project_policy_version,
    publish_master_version,
)


@pytest.fixture
def journey_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for journey integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-journey-{suffix}"
    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"JCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Journey OEM') RETURNING oem_id"
            ),
            {"code": f"JOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Journey Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"JP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Journey Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"JD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Journey Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"JO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Journey Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()

        policy_version_id = create_project_policy_version(
            connection,
            tenant_id=tenant_id,
            version_no=1,
            effective_from=date(2026, 8, 1),
            actor_id="admin",
        )
        profile_id = create_document_profile(
            connection,
            tenant_id=tenant_id,
            code=f"JPROFILE-{suffix}",
            name="Journey Profile",
            actor_id="admin",
        )
        profile_version_id = create_document_profile_version(
            connection,
            tenant_id=tenant_id,
            profile_id=profile_id,
            version_no=1,
            effective_from=date(2026, 8, 1),
            actor_id="admin",
        )
        publish_master_version(
            connection,
            master_type="POLICY",
            tenant_id=tenant_id,
            version_id=policy_version_id,
            actor_id="admin",
        )
        publish_master_version(
            connection,
            master_type="DOCUMENT_PROFILE",
            tenant_id=tenant_id,
            version_id=profile_version_id,
            actor_id="admin",
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject="journey-user",
        tenant_id=tenant_id,
        permissions=(),
    )
    try:
        yield tenant_id, dealer_id, outlet_id, customer_id, profile_version_id, policy_version_id
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_journey_links_customer_hierarchy_and_keeps_audit_fields_separate(journey_setup) -> None:
    tenant_id, dealer_id, outlet_id, customer_id, profile_version_id, policy_version_id = (
        journey_setup
    )
    client = TestClient(app, raise_server_exceptions=False)

    created = client.post(
        f"/v1/tenants/{tenant_id}/customers/{customer_id}/journeys",
        json={
            "journeyReference": "JR-001",
            "observedStatusCode": "SOURCE_STATUS",
            "observedStatusSource": "OPERATIONAL_INPUT",
            "documentRequirementProfileVersionId": str(profile_version_id),
            "policyVersionId": str(policy_version_id),
        },
    )
    assert created.status_code == 201
    body = created.json()
    journey_id = body["journeyId"]
    assert body["customerId"] == str(customer_id)
    assert body["dealerId"] == str(dealer_id)
    assert body["outletId"] == str(outlet_id)
    assert body["auditState"] == "NOT_STARTED"
    assert body["auditOutcome"] == "PENDING"
    assert body["documentRequirementProfileVersionId"] == str(profile_version_id)
    assert body["policyVersionId"] == str(policy_version_id)

    listed = client.get(f"/v1/tenants/{tenant_id}/customers/{customer_id}/journeys")
    assert listed.status_code == 200
    assert listed.json()[0]["journeyId"] == journey_id

    updated = client.patch(
        f"/v1/tenants/{tenant_id}/journeys/{journey_id}",
        json={
            "observedStatusCode": "SOURCE_STATUS_UPDATED",
            "observedStatusSource": "SOURCE_SYSTEM",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["observedStatusCode"] == "SOURCE_STATUS_UPDATED"
    assert updated.json()["auditState"] == "NOT_STARTED"
    assert updated.json()["auditOutcome"] == "PENDING"

    forbidden_audit_patch = client.patch(
        f"/v1/tenants/{tenant_id}/journeys/{journey_id}",
        json={"auditState": "REVIEW_COMPLETE"},
    )
    assert forbidden_audit_patch.status_code == 400
    assert forbidden_audit_patch.json()["errorCode"] == "VAC-VAL-001"
