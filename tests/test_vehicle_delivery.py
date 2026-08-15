from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_vehicle_registration_and_delivery_status_history_are_journey_facts() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for vehicle/delivery integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-delivery-{suffix}"
    actor_id = f"pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"VCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Delivery OEM') RETURNING oem_id"
            ),
            {"code": f"VOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Delivery Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"VP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_status_codes (
                    tenant_id, domain_key, status_code, status_label
                ) VALUES
                    (:tenant_id, 'DELIVERY', 'READY_TEST', 'Ready for delivery'),
                    (:tenant_id, 'DELIVERY', 'DELIVERED_TEST', 'Delivered')
                """
            ),
            {"tenant_id": tenant_id},
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Delivery Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"VD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Delivery Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"VO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Delivery Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id,
                    journey_reference, audit_state, audit_outcome
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id,
                    'DELIVERY-JOURNEY', 'TL_REVIEW', 'PENDING'
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
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code,
                    dealer_id, outlet_id
                ) VALUES (
                    :tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=(
            "audit.journey.read",
            "audit.journey.update",
            "audit.delivery.read",
            "audit.delivery.write",
        ),
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        base = f"/v1/tenants/{tenant_id}/journeys/{journey_id}"

        vehicle = client.put(
            f"{base}/vehicle",
            json={
                "vin": "VIN-TEST-001",
                "chassisNumber": "CHASSIS-TEST-001",
                "invoiceReference": "INV-001",
                "sourceKind": "EVIDENCE",
            },
        )
        assert vehicle.status_code == 200, vehicle.text
        assert vehicle.json()["vin"] == "VIN-TEST-001"

        registration = client.put(
            f"{base}/registration",
            json={
                "registrationState": "Odisha",
                "registrationDistrict": "Balasore",
                "registrationNumber": "OD-TEST-001",
                "sourceKind": "SOURCE_SYSTEM",
            },
        )
        assert registration.status_code == 200, registration.text
        assert registration.json()["registrationNumber"] == "OD-TEST-001"

        ready = client.put(
            f"{base}/delivery",
            headers={"Idempotency-Key": f"delivery-ready-{suffix}"},
            json={
                "plannedDeliveryAt": "2026-08-16T10:00:00Z",
                "actualDeliveryStatusCode": "READY_TEST",
                "statusSource": "OPERATIONAL_INPUT",
            },
        )
        assert ready.status_code == 200, ready.text
        assert ready.json()["statusLabel"] == "Ready for delivery"

        delivered = client.put(
            f"{base}/delivery",
            headers={"Idempotency-Key": f"delivery-delivered-{suffix}"},
            json={
                "plannedDeliveryAt": "2026-08-16T10:00:00Z",
                "actualDeliveryStatusCode": "DELIVERED_TEST",
                "actualDeliveredAt": "2026-08-16T11:00:00Z",
                "statusSource": "EVIDENCE",
            },
        )
        assert delivered.status_code == 200, delivered.text
        body = delivered.json()
        assert body["actualDeliveryStatusCode"] == "DELIVERED_TEST"
        assert [item["actualDeliveryStatusCode"] for item in body["history"]] == [
            "READY_TEST",
            "DELIVERED_TEST",
        ]

        with engine.begin() as connection:
            audit = connection.execute(
                text(
                    """
                    SELECT audit_state, audit_outcome
                    FROM auditcore.journeys
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).mappings().one()
        assert audit["audit_state"] == "TL_REVIEW"
        assert audit["audit_outcome"] == "PENDING"
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
