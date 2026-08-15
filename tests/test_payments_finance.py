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


def test_multiple_payments_verification_exception_and_finance_are_recorded() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for payments integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-payment-{suffix}"
    actor_id = f"tl-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"PCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Payment OEM') RETURNING oem_id"
            ),
            {"code": f"POEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Payment Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"PP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Payment Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"PD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Payment Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"PO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Payment Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'PAYMENT-JOURNEY'
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
                    :tenant_id, :actor_id, 'TL', :dealer_id, :outlet_id
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
        permissions=("audit.payment.read", "audit.payment.write", "audit.payment.verify"),
    )
    try:
        client = TestClient(app, raise_server_exceptions=False)
        payments_url = f"/v1/tenants/{tenant_id}/journeys/{journey_id}/payments"

        first = client.post(
            payments_url,
            json={
                "amount": "30000.00",
                "paymentMethodCode": "CARD",
                "paymentReference": "MR-1",
                "statusSource": "OPERATIONAL_INPUT",
            },
        )
        assert first.status_code == 201, first.text
        first_id = first.json()["paymentId"]

        verified = client.patch(
            payments_url,
            json={
                "paymentId": first_id,
                "verification": {
                    "result": "EXCEPTION",
                    "notes": "Receipt requires audit follow-up",
                    "verifiedByRoleCode": "TL",
                },
            },
        )
        assert verified.status_code == 200, verified.text
        assert verified.json()["verifications"][0]["result"] == "EXCEPTION"
        assert Decimal(str(verified.json()["amount"])) == Decimal("30000.00")

        second = client.post(
            payments_url,
            json={
                "amount": "1360000.00",
                "paymentMethodCode": "BANK_TRANSFER",
                "paymentReference": "DO-1",
                "statusSource": "SOURCE_SYSTEM",
            },
        )
        assert second.status_code == 201, second.text
        listed = client.get(payments_url)
        assert listed.status_code == 200
        assert len(listed.json()) == 2

        finance_url = f"/v1/tenants/{tenant_id}/journeys/{journey_id}/finance"
        finance = client.put(
            finance_url,
            json={
                "financeTypeCode": "BANK_LOAN",
                "providerName": "Example Bank",
                "doReference": "DO-1",
                "financedAmount": "1360000.00",
                "sourceKind": "OPERATIONAL_INPUT",
            },
        )
        assert finance.status_code == 200, finance.text
        assert Decimal(str(finance.json()["financedAmount"])) == Decimal("1360000.00")
        assert client.get(finance_url).json() == finance.json()
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
