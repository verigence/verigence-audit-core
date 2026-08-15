from __future__ import annotations

import os
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_insurance_addons_and_trade_in_persist_independently() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for insurance/trade-in integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-insurance-{suffix}"
    actor_id = f"pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"ICAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Insurance OEM') RETURNING oem_id"
            ),
            {"code": f"IOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Insurance Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"IP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Insurance Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"ID-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Insurance Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"IO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Insurance Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'INSURANCE-JOURNEY'
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
            "audit.trade_in.read",
            "audit.trade_in.write",
        ),
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        insurance_url = f"/v1/tenants/{tenant_id}/journeys/{journey_id}/insurance"
        insurance = client.put(
            insurance_url,
            json={
                "insurerName": "Example Insurance",
                "policyReference": "POL-1",
                "standardPremiumAmount": "50000.00",
                "actualPremiumAmount": "50456.00",
                "sourceKind": "OPERATIONAL_INPUT",
                "addons": [
                    {
                        "addonTypeCode": "EXTENDED_WARRANTY",
                        "standardAmount": "15000.00",
                        "actualAmount": "15000.00",
                        "referenceNumber": "EW-1",
                        "sourceKind": "OPERATIONAL_INPUT",
                    }
                ],
            },
        )
        assert insurance.status_code == 200, insurance.text
        body = insurance.json()
        assert Decimal(str(body["actualPremiumAmount"])) == Decimal("50456.00")
        assert body["addons"][0]["addonTypeCode"] == "EXTENDED_WARRANTY"
        assert client.get(insurance_url).json() == body

        trade_url = f"/v1/tenants/{tenant_id}/journeys/{journey_id}/trade-in"
        trade = client.put(
            trade_url,
            json={
                "oldVehicleRegistration": "OD-01-TEST",
                "oldVehicleMakeModel": "Old Vehicle",
                "quotedValue": "40000.00",
                "actualValue": "40000.00",
                "sourceKind": "EVIDENCE",
                "details": {"source": "trade-in document"},
            },
        )
        assert trade.status_code == 200, trade.text
        assert Decimal(str(trade.json()["actualValue"])) == Decimal("40000.00")
        assert "ageing" not in trade.json()
        assert client.get(trade_url).json() == trade.json()
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
