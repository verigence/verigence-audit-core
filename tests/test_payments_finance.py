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

        # A receipt/payment lazily establishes the Journey's Booking linkage but
        # does not guess whether the receipt belongs to Booking or Delivery.
        with engine.begin() as connection:
            first_link = connection.execute(
                text(
                    """
                    SELECT p.booking_id, p.delivery_id, p.payment_stage,
                           j.booking_id AS journey_booking_id,
                           c.journey_id AS customer_journey_id,
                           c.booking_id AS customer_booking_id
                    FROM auditcore.payments p
                    JOIN auditcore.journeys j
                      ON j.tenant_id=p.tenant_id AND j.journey_id=p.journey_id
                    JOIN auditcore.customers c
                      ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                    WHERE p.tenant_id=:tenant_id AND p.payment_id=:payment_id
                    """
                ),
                {"tenant_id": tenant_id, "payment_id": first_id},
            ).mappings().one()
        assert first_link["booking_id"] is not None
        assert first_link["booking_id"] == first_link["journey_booking_id"]
        assert first_link["booking_id"] == first_link["customer_booking_id"]
        assert first_link["customer_journey_id"] == journey_id
        assert first_link["delivery_id"] is None
        assert first_link["payment_stage"] == "UNSPECIFIED"

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
        second_id = second.json()["paymentId"]
        assert second_id != first_id

        listed = client.get(payments_url)
        assert listed.status_code == 200
        assert len(listed.json()) == 2

        with engine.begin() as connection:
            links = connection.execute(
                text(
                    """
                    SELECT payment_id, booking_id, payment_stage
                    FROM auditcore.payments
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                    ORDER BY created_at_utc, payment_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).mappings().all()
        assert len(links) == 2
        assert {row["booking_id"] for row in links} == {first_link["booking_id"]}
        assert {row["payment_stage"] for row in links} == {"UNSPECIFIED"}

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
