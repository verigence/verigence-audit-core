from __future__ import annotations

import os
from dataclasses import dataclass
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationDecision,
    get_security_authorization_client,
)


@dataclass
class AllowAuthorization:
    calls: int = 0

    def check_user_permission(self, *, user_id: str, tenant_id: str, permission_key: str):
        self.calls += 1
        return SecurityAuthorizationDecision(
            allowed=True,
            reason_code="AUTHORIZED",
            user_id=user_id,
            tenant_id=tenant_id,
            permission_key=permission_key,
            role_key="PC",
        )


def _dealer_outlet(connection, *, tenant_id: str, suffix: str, dealer_code: str, outlet_code: str):
    dealer_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
            VALUES (:tenant_id, :dealer_code, :dealer_name)
            RETURNING dealer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_code": f"{dealer_code}-{suffix}",
            "dealer_name": f"Dealer {dealer_code}",
        },
    ).scalar_one()
    outlet_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.dealer_outlets (
                tenant_id, dealer_id, outlet_code, outlet_name
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_code, :outlet_name
            ) RETURNING outlet_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_code": f"{outlet_code}-{suffix}",
            "outlet_name": f"Outlet {outlet_code}",
        },
    ).scalar_one()
    return dealer_id, outlet_id


def _journey(
    connection,
    *,
    tenant_id: str,
    dealer_id,
    outlet_id,
    entered_name: str,
    legal_name: str,
    mobile: str,
    booking_reference: str,
    vin: str,
    chassis: str,
    invoice_reference: str,
    registration_number: str,
    payment_reference: str,
):
    customer_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.customers (
                tenant_id, dealer_id, outlet_id, customer_type_code,
                display_name, legal_name, legal_name_status,
                mobile_number, mobile_last4
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL',
                :entered_name, :legal_name, 'VERIFIED',
                :mobile, :mobile_last4
            ) RETURNING customer_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "dealer_id": dealer_id,
            "outlet_id": outlet_id,
            "entered_name": entered_name,
            "legal_name": legal_name,
            "mobile": mobile,
            "mobile_last4": mobile[-4:],
        },
    ).scalar_one()
    journey_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.journeys (
                tenant_id, dealer_id, outlet_id, customer_id
            ) VALUES (
                :tenant_id, :dealer_id, :outlet_id, :customer_id
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
    booking_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.bookings (
                tenant_id, journey_id, booking_reference, booking_date
            ) VALUES (
                :tenant_id, :journey_id, :booking_reference, CURRENT_DATE
            ) RETURNING booking_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "booking_reference": booking_reference,
        },
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status,
                audit_state, audit_status
            ) VALUES (
                :tenant_id, :journey_id, 'BOOKING', 'BOOKING_CLOSED',
                'COMPLETE', 'NO_FLAGS'
            )
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )
    delivery_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.deliveries (
                tenant_id, journey_id, actual_delivery_status_code,
                status_source, actual_delivered_at
            ) VALUES (
                :tenant_id, :journey_id, 'DELIVERED',
                'SOURCE_SYSTEM', now()
            ) RETURNING delivery_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_stage_states (
                tenant_id, journey_id, stage_code, business_status,
                audit_state, audit_status
            ) VALUES (
                :tenant_id, :journey_id, 'DELIVERY', 'DELIVERED',
                'COMPLETE', 'NO_FLAGS'
            )
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.vehicle_records (
                tenant_id, journey_id, vin, chassis_number,
                dms_reference, invoice_reference, source_kind
            ) VALUES (
                :tenant_id, :journey_id, :vin, :chassis,
                :dms_reference, :invoice_reference, 'SOURCE_SYSTEM'
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "vin": vin,
            "chassis": chassis,
            "dms_reference": f"DMS-{booking_reference}",
            "invoice_reference": invoice_reference,
        },
    )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.registration_records (
                tenant_id, journey_id, registration_number,
                actual_status_code, source_kind
            ) VALUES (
                :tenant_id, :journey_id, :registration_number,
                'REGISTERED', 'SOURCE_SYSTEM'
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "registration_number": registration_number,
        },
    )
    for offset, amount in enumerate((50000, 150000), start=1):
        connection.execute(
            text(
                """
                INSERT INTO auditcore.payments (
                    tenant_id, journey_id, payment_at_utc, amount,
                    currency_code, payment_reference, actual_status_code,
                    status_source
                ) VALUES (
                    :tenant_id, :journey_id, now(), :amount,
                    'INR', :payment_reference, 'RECEIVED',
                    'SOURCE_SYSTEM'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "amount": amount,
                "payment_reference": payment_reference if offset == 1 else f"{payment_reference}-2",
            },
        )
    return journey_id, booking_id, delivery_id


@pytest.fixture
def journey_search_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Journey search integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-search-{suffix}"
    pc_actor = f"pc-search-{suffix}"
    tl_actor = f"tl-search-{suffix}"
    pm_actor = f"pm-search-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, 'Search Category') RETURNING product_category_id
                """
            ),
            {"code": f"SC-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, 'Search OEM') RETURNING oem_id
                """
            ),
            {"code": f"SO-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'Search Project', :oem_id,
                    :category_id, CURRENT_DATE, 'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"SEARCH-{suffix}",
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
                    (:tenant_id, 'DELIVERY', 'DELIVERED', 'Delivered'),
                    (:tenant_id, 'PAYMENT', 'RECEIVED', 'Received')
                """
            ),
            {"tenant_id": tenant_id},
        )

        dealer_a, outlet_a1 = _dealer_outlet(
            connection,
            tenant_id=tenant_id,
            suffix=suffix,
            dealer_code="A",
            outlet_code="A1",
        )
        outlet_a2 = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_code, 'Outlet A2'
                ) RETURNING outlet_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": dealer_a,
                "outlet_code": f"A2-{suffix}",
            },
        ).scalar_one()
        dealer_b, outlet_b1 = _dealer_outlet(
            connection,
            tenant_id=tenant_id,
            suffix=suffix,
            dealer_code="B",
            outlet_code="B1",
        )

        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code,
                    dealer_id, outlet_id
                ) VALUES
                    (:tenant_id, :pc_actor, 'PC', :dealer_a, :outlet_a1),
                    (:tenant_id, :tl_actor, 'TL', :dealer_a, NULL),
                    (:tenant_id, :pm_actor, 'PM', NULL, NULL)
                """
            ),
            {
                "tenant_id": tenant_id,
                "pc_actor": pc_actor,
                "tl_actor": tl_actor,
                "pm_actor": pm_actor,
                "dealer_a": dealer_a,
                "outlet_a1": outlet_a1,
            },
        )

        primary, booking_id, delivery_id = _journey(
            connection,
            tenant_id=tenant_id,
            dealer_id=dealer_a,
            outlet_id=outlet_a1,
            entered_name="Manmohan Oja",
            legal_name="Manmohan Ojha",
            mobile="9819751923",
            booking_reference="DLR-BOOK-1001",
            vin="MA1VIN1234567890",
            chassis="CHASSIS1234567890",
            invoice_reference="INV-1001",
            registration_number="CH01AB1234",
            payment_reference="UTR-1001",
        )
        outlet_two, _, _ = _journey(
            connection,
            tenant_id=tenant_id,
            dealer_id=dealer_a,
            outlet_id=outlet_a2,
            entered_name="Outlet Two Customer",
            legal_name="Outlet Two Legal",
            mobile="9819752923",
            booking_reference="DLR-BOOK-2002",
            vin="MA1VIN2234567890",
            chassis="CHASSIS2234567890",
            invoice_reference="INV-2002",
            registration_number="CH01AB2234",
            payment_reference="UTR-2002",
        )
        hidden, _, _ = _journey(
            connection,
            tenant_id=tenant_id,
            dealer_id=dealer_b,
            outlet_id=outlet_b1,
            entered_name="Dealer B Customer",
            legal_name="Dealer B Legal",
            mobile="9819753923",
            booking_reference="DLR-BOOK-3003",
            vin="MA1VIN3234567890",
            chassis="CHASSIS3234567890",
            invoice_reference="INV-3003",
            registration_number="CH01AB3234",
            payment_reference="UTR-3003",
        )

    current_actor = {"id": pc_actor}
    authorization = AllowAuthorization()
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(
        subject=current_actor["id"]
    )
    app.dependency_overrides[get_security_authorization_client] = lambda: authorization
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "current_actor": current_actor,
            "pc_actor": pc_actor,
            "tl_actor": tl_actor,
            "pm_actor": pm_actor,
            "primary": primary,
            "outlet_two": outlet_two,
            "hidden": hidden,
            "booking_id": booking_id,
            "delivery_id": delivery_id,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _search(client: TestClient, tenant_id: str, query: str):
    return client.get(
        f"/v1/tenants/{tenant_id}/uc03/journey-search",
        params={"q": query},
    )


def test_pc_search_is_outlet_scoped_and_supports_operational_keys(journey_search_setup) -> None:
    setup = journey_search_setup
    client = TestClient(app, raise_server_exceptions=False)

    for query in (
        "Manmohan Oja",
        "Manmohan Ojha",
        "9819751923",
        "DLR-BOOK-1001",
        "MA1VIN1234567890",
        "CHASSIS1234567890",
        "CH01AB1234",
        "INV-1001",
        "DMS-DLR-BOOK-1001",
        "UTR-1001",
    ):
        response = _search(client, setup["tenant_id"], query)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["resultCount"] >= 1
        assert body["items"][0]["journeyId"] == str(setup["primary"])

    mobile = _search(client, setup["tenant_id"], "9819751923").json()["items"][0]
    assert mobile["matchedValue"] == "******1923"

    hidden = _search(client, setup["tenant_id"], "DLR-BOOK-2002")
    assert hidden.status_code == 200
    assert hidden.json()["items"] == []


def test_tl_and_pm_search_expand_only_through_business_assignments(journey_search_setup) -> None:
    setup = journey_search_setup
    client = TestClient(app, raise_server_exceptions=False)

    setup["current_actor"]["id"] = setup["tl_actor"]
    tl_visible = _search(client, setup["tenant_id"], "DLR-BOOK-2002")
    tl_hidden = _search(client, setup["tenant_id"], "DLR-BOOK-3003")
    assert tl_visible.status_code == 200
    assert tl_visible.json()["items"][0]["journeyId"] == str(setup["outlet_two"])
    assert tl_hidden.status_code == 200
    assert tl_hidden.json()["items"] == []

    setup["current_actor"]["id"] = setup["pm_actor"]
    pm_visible = _search(client, setup["tenant_id"], "DLR-BOOK-3003")
    assert pm_visible.status_code == 200
    assert pm_visible.json()["items"][0]["journeyId"] == str(setup["hidden"])


def test_journey_overview_returns_booking_delivery_and_multiple_payments(journey_search_setup) -> None:
    setup = journey_search_setup
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(
        f"/v1/tenants/{setup['tenant_id']}/uc03/journeys/{setup['primary']}/overview"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["journey"]["journeyId"] == str(setup["primary"])
    assert body["journey"]["bookingId"] == str(setup["booking_id"])
    assert body["journey"]["deliveryId"] == str(setup["delivery_id"])
    assert body["customer"]["enteredName"] == "Manmohan Oja"
    assert body["customer"]["legalName"] == "Manmohan Ojha"
    assert body["booking"]["bookingReference"] == "DLR-BOOK-1001"
    assert body["delivery"]["actualDeliveryStatusCode"] == "DELIVERED"
    assert body["vehicle"]["vin"] == "MA1VIN1234567890"
    assert body["registration"]["registrationNumber"] == "CH01AB1234"
    assert len(body["payments"]) == 2
    assert "paymentStage" not in body["payments"][0]


def test_out_of_scope_overview_does_not_disclose_journey(journey_search_setup) -> None:
    setup = journey_search_setup
    client = TestClient(app, raise_server_exceptions=False)

    response = client.get(
        f"/v1/tenants/{setup['tenant_id']}/uc03/journeys/{setup['hidden']}/overview"
    )
    assert response.status_code == 404
