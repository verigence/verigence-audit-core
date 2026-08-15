from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_booking_records_sales_consultant_and_product_snapshot() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for booking integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-booking-{suffix}"
    actor_id = f"pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"BCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Booking OEM') RETURNING oem_id"
            ),
            {"code": f"BOEM-{suffix}"},
        ).scalar_one()
        model_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_models (oem_id, model_code, model_name) "
                "VALUES (:oem_id, 'CRETA', 'Creta') RETURNING model_id"
            ),
            {"oem_id": oem_id},
        ).scalar_one()
        variant_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_variants (model_id, variant_code, variant_name) "
                "VALUES (:model_id, 'S-O-MT', 'S(O) MT') RETURNING variant_id"
            ),
            {"model_id": model_id},
        ).scalar_one()
        colour_id = connection.execute(
            text(
                "INSERT INTO auditcore.colours (oem_id, colour_code, colour_name) "
                "VALUES (:oem_id, 'A-WHITE', 'Atlas White') RETURNING colour_id"
            ),
            {"oem_id": oem_id},
        ).scalar_one()
        sku_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_skus (
                    oem_id, model_id, variant_id, colour_id, sku_code
                ) VALUES (
                    :oem_id, :model_id, :variant_id, :colour_id, :sku_code
                ) RETURNING product_sku_id
                """
            ),
            {
                "oem_id": oem_id,
                "model_id": model_id,
                "variant_id": variant_id,
                "colour_id": colour_id,
                "sku_code": f"BSKU-{suffix}",
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Booking Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"BP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Booking Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"BD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Booking Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"BO-{suffix}"},
        ).scalar_one()
        sales_staff_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealership_staff (
                    tenant_id, dealer_id, outlet_id, staff_role_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'SC', 'Sales Consultant'
                ) RETURNING dealership_staff_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Booking Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'BOOKING-JOURNEY'
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
        permissions=("audit.journey.read", "audit.journey.update"),
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        url = f"/v1/tenants/{tenant_id}/journeys/{journey_id}/booking"
        response = client.put(
            url,
            json={
                "bookingReference": "B-2026-001",
                "bookingDate": "2026-08-16",
                "salesStaffId": str(sales_staff_id),
                "productSkuId": str(sku_id),
                "selectionSource": "OPERATIONAL_INPUT",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["salesStaffId"] == str(sales_staff_id)
        assert body["product"] == {
            "productSkuId": str(sku_id),
            "modelCode": "CRETA",
            "modelName": "Creta",
            "variantCode": "S-O-MT",
            "variantName": "S(O) MT",
            "colourCode": "A-WHITE",
            "colourName": "Atlas White",
            "selectionSource": "OPERATIONAL_INPUT",
        }

        read_back = client.get(url)
        assert read_back.status_code == 200
        assert read_back.json() == body
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
