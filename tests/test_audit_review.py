from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_pc_submit_send_back_and_pm_review_do_not_change_delivery_status() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for audit review integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-review-{suffix}"
    pc_id = f"pc-{suffix}"
    tl_id = f"tl-{suffix}"
    pm_id = f"pm-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"RCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Review OEM') RETURNING oem_id"
            ),
            {"code": f"ROEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Review Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"RP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_status_codes (
                    tenant_id, domain_key, status_code, status_label
                ) VALUES (
                    :tenant_id, 'DELIVERY', 'DELIVERED_SOURCE_STATUS', 'Delivered'
                )
                """
            ),
            {"tenant_id": tenant_id},
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Review Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"RD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Review Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"RO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Review Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id, journey_reference
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'REVIEW-JOURNEY'
                ) RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
                "customer_id": customer_id,
            },
        ).scalar_one()
        for actor_id, role_code in ((pc_id, "PC"), (tl_id, "TL"), (pm_id, "PM")):
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.business_assignments (
                        tenant_id, security_actor_id, business_role_code,
                        dealer_id, outlet_id
                    ) VALUES (
                        :tenant_id, :actor_id, :role_code, :dealer_id, :outlet_id
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "actor_id": actor_id,
                    "role_code": role_code,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.deliveries (
                    tenant_id, journey_id, actual_delivery_status_code,
                    status_label_snapshot, status_source, recorded_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'DELIVERED_SOURCE_STATUS',
                    'Delivered', 'SOURCE_SYSTEM', :pc_id
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "pc_id": pc_id},
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/v1/tenants/{tenant_id}/journeys/{journey_id}"

    try:
        app.dependency_overrides[get_principal] = lambda: Principal(
            subject=pc_id,
            tenant_id=tenant_id,
            permissions=(
                "audit.journey.read",
                "audit.journey.update",
                "audit.journey.submit",
            ),
        )
        started = client.post(f"{base}/audit/start")
        assert started.status_code == 200, started.text
        assert started.json()["auditState"] == "IN_PROGRESS"

        submitted = client.post(f"{base}/audit/submit")
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["auditState"] == "PC_SUBMITTED"

        app.dependency_overrides[get_principal] = lambda: Principal(
            subject=tl_id,
            tenant_id=tenant_id,
            permissions=("audit.review.read", "audit.review.decide"),
        )
        sent_back = client.post(
            f"{base}/review-decisions",
            json={
                "decision": "SEND_BACK",
                "reviewerRoleCode": "TL",
                "remarks": "PC clarification required",
            },
        )
        assert sent_back.status_code == 201, sent_back.text

        app.dependency_overrides[get_principal] = lambda: Principal(
            subject=pc_id,
            tenant_id=tenant_id,
            permissions=("audit.journey.read", "audit.journey.submit"),
        )
        resubmitted = client.post(f"{base}/audit/submit")
        assert resubmitted.status_code == 200, resubmitted.text
        assert resubmitted.json()["auditState"] == "PC_SUBMITTED"

        app.dependency_overrides[get_principal] = lambda: Principal(
            subject=pm_id,
            tenant_id=tenant_id,
            permissions=("audit.review.read", "audit.review.decide"),
        )
        final = client.post(
            f"{base}/review-decisions",
            json={
                "decision": "BREACH",
                "reviewerRoleCode": "PM",
                "remarks": "Review completed",
            },
        )
        assert final.status_code == 201, final.text
        decisions = client.get(f"{base}/review-decisions")
        assert decisions.status_code == 200
        assert [item["decision"] for item in decisions.json()] == ["SEND_BACK", "BREACH"]

        with engine.begin() as connection:
            state = connection.execute(
                text(
                    """
                    SELECT audit_state, audit_outcome
                    FROM auditcore.journeys
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).mappings().one()
            delivery_status = connection.execute(
                text(
                    """
                    SELECT actual_delivery_status_code
                    FROM auditcore.deliveries
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).scalar_one()
        assert state["audit_state"] == "REVIEW_COMPLETE"
        assert state["audit_outcome"] == "BREACH"
        assert delivery_status == "DELIVERED_SOURCE_STATUS"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
