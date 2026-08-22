from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_principal
from audit_core.main import app
from audit_core.security import Principal


@pytest.fixture
def uc03_work_items_setup():
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Work Item integration tests")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-work-{suffix}"
    actor_id = f"uc03-pc-{suffix}"
    journey_ids = []

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"UC03-W-CAT-{suffix}", "name": f"UC03 Work Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"UC03-W-OEM-{suffix}", "name": f"UC03 Work OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date,
                    timezone_name, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'UC03 Work Project', :oem_id,
                    :category_id, CURRENT_DATE - 1,
                    'Asia/Kolkata', 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"UC03-W-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )

        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Authorized Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Authorized Outlet')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"O-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code,
                    dealer_id, outlet_id
                ) VALUES (:tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id)
                """
            ),
            {
                "tenant_id": tenant_id,
                "actor_id": actor_id,
                "dealer_id": dealer_id,
                "outlet_id": outlet_id,
            },
        )

        base_activity = datetime(2026, 8, 23, 4, 0, tzinfo=timezone.utc)
        for index in range(12):
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id, customer_type_code,
                        display_name, mobile_last4
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL',
                        :display_name, :mobile_last4
                    )
                    RETURNING customer_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "display_name": f"Customer {index:02d}",
                    "mobile_last4": f"{index:04d}",
                },
            ).scalar_one()
            journey_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journeys (
                        tenant_id, dealer_id, outlet_id, customer_id,
                        journey_reference
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, :customer_id,
                        :journey_reference
                    )
                    RETURNING journey_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                    "customer_id": customer_id,
                    "journey_reference": f"J-{index:02d}",
                },
            ).scalar_one()
            journey_ids.append(journey_id)
            activity = base_activity - timedelta(minutes=index)
            booking_started = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc) + timedelta(
                minutes=index
            )
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.bookings (
                        tenant_id, journey_id, booking_reference, booking_date
                    ) VALUES (
                        :tenant_id, :journey_id, :booking_reference, DATE '2026-08-20'
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "booking_reference": f"B-{index:02d}",
                },
            )
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journey_stage_states (
                        tenant_id, journey_id, stage_code, business_status,
                        audit_state, audit_status, first_started_at_utc,
                        latest_activity_at_utc
                    ) VALUES (
                        :tenant_id, :journey_id, 'BOOKING', 'BOOKING_IN_PROGRESS',
                        'IN_PROGRESS', 'NOT_EVALUATED', :started_at, :activity
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "started_at": booking_started,
                    "activity": activity,
                },
            )

            if index < 2:
                delivered_at = datetime(2026, 8, 22, 19, 0, tzinfo=timezone.utc) + timedelta(
                    minutes=index
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.deliveries (
                            tenant_id, journey_id, actual_delivery_status_code,
                            actual_delivered_at, status_source
                        ) VALUES (
                            :tenant_id, :journey_id, NULL,
                            :delivered_at, 'OPERATIONAL_INPUT'
                        )
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "journey_id": journey_id,
                        "delivered_at": delivered_at,
                    },
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.journey_stage_states (
                            tenant_id, journey_id, stage_code, business_status,
                            audit_state, audit_status, first_started_at_utc,
                            business_completed_at_utc, latest_activity_at_utc
                        ) VALUES (
                            :tenant_id, :journey_id, 'DELIVERY', 'DELIVERY_COMPLETED',
                            'IN_PROGRESS', 'FLAGS_RAISED', :started_at,
                            :completed_at, :activity
                        )
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "journey_id": journey_id,
                        "started_at": delivered_at - timedelta(hours=1),
                        "completed_at": delivered_at,
                        "activity": activity + timedelta(seconds=10),
                    },
                )

        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, severity, finding_status,
                    title, description
                ) VALUES (
                    :tenant_id, :journey_id, 'CRITICAL', 'OPEN',
                    'Test critical flag', 'UC03 C0 test flag'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_ids[0]},
        )
        customer_for_first = connection.execute(
            text(
                """
                SELECT customer_id FROM auditcore.journeys
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_ids[0]},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence_ingestion_operations (
                    tenant_id, journey_id, customer_id, idempotency_key,
                    evidence_purpose, operation_status
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id, :idempotency_key,
                    'BOOKING_DOCKET', 'DI_ACCEPTED'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_ids[0],
                "customer_id": customer_for_first,
                "idempotency_key": f"uc03-work-{suffix}",
            },
        )

        # A Journey outside the PC assignment must not appear in the work list.
        other_dealer = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Other Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"D2-{suffix}"},
        ).scalar_one()
        other_outlet = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (:tenant_id, :dealer_id, :code, 'Other Outlet')
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": other_dealer, "code": f"O2-{suffix}"},
        ).scalar_one()
        other_customer = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (:tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', 'Hidden Customer')
                RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": other_dealer, "outlet_id": other_outlet},
        ).scalar_one()
        hidden_journey = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id
                ) VALUES (:tenant_id, :dealer_id, :outlet_id, :customer_id)
                RETURNING journey_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "dealer_id": other_dealer,
                "outlet_id": other_outlet,
                "customer_id": other_customer,
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_reference, booking_date)
                VALUES (:tenant_id, :journey_id, 'HIDDEN', DATE '2026-08-20')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": hidden_journey},
        )

    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=("audit.journey.read",),
    )
    try:
        yield {
            "engine": engine,
            "tenant_id": tenant_id,
            "actor_id": actor_id,
            "journey_ids": journey_ids,
        }
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_work_items_enforce_scope_and_page_at_ten(uc03_work_items_setup) -> None:
    setup = uc03_work_items_setup
    client = TestClient(app, raise_server_exceptions=False)
    path = f"/v1/tenants/{setup['tenant_id']}/uc03/work-items"

    first = client.get(path)
    assert first.status_code == 200
    payload = first.json()
    assert payload["pageSize"] == 10
    assert len(payload["items"]) == 10
    assert payload["nextCursor"]
    assert payload["previousCursor"] is None
    assert payload["filters"] == {
        "workType": "ALL",
        "fromDate": None,
        "toDate": None,
        "timezoneName": "Asia/Kolkata",
    }
    assert all(item["customerDisplayName"] != "Hidden Customer" for item in payload["items"])

    second = client.get(path, params={"cursor": payload["nextCursor"]})
    assert second.status_code == 200
    second_payload = second.json()
    assert second_payload["pageSize"] == 2
    assert second_payload["nextCursor"] is None
    first_ids = {item["journeyId"] for item in payload["items"]}
    second_ids = {item["journeyId"] for item in second_payload["items"]}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_ids | second_ids) == 12

    newest = payload["items"][0]
    assert newest["openFlagCount"] == 1
    assert newest["totalFlagCount"] == 1
    assert newest["highestOpenSeverity"] == "CRITICAL"
    assert newest["processingDocumentCount"] == 1
    assert newest["proposalReadyCount"] == 0
    assert newest["nextActionCode"] is None


def test_work_items_filter_delivery_using_project_timezone(uc03_work_items_setup) -> None:
    setup = uc03_work_items_setup
    client = TestClient(app, raise_server_exceptions=False)
    path = f"/v1/tenants/{setup['tenant_id']}/uc03/work-items"

    response = client.get(
        path,
        params={
            "workType": "DELIVERY",
            "fromDate": "2026-08-23",
            "toDate": "2026-08-23",
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["pageSize"] == 2
    assert all(item["delivery"]["businessDate"] == "2026-08-23" for item in payload["items"])
    assert all(item["delivery"]["businessStatus"] == "DELIVERY_COMPLETED" for item in payload["items"])

    booking_only = client.get(path, params={"workType": "BOOKING"})
    assert booking_only.status_code == 200
    assert booking_only.json()["pageSize"] == 10


def test_work_item_cursor_is_bound_to_filters_and_date_range_is_validated(
    uc03_work_items_setup,
) -> None:
    setup = uc03_work_items_setup
    client = TestClient(app, raise_server_exceptions=False)
    path = f"/v1/tenants/{setup['tenant_id']}/uc03/work-items"

    first = client.get(path, params={"workType": "BOOKING", "limit": 2})
    assert first.status_code == 200
    cursor = first.json()["nextCursor"]
    assert cursor

    mismatch = client.get(
        path,
        params={"workType": "DELIVERY", "limit": 2, "cursor": cursor},
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["errorCode"] == "VAC-VAL-001"

    invalid_range = client.get(
        path,
        params={"fromDate": "2026-08-24", "toDate": "2026-08-23"},
    )
    assert invalid_range.status_code == 400
    assert invalid_range.json()["errorCode"] == "VAC-VAL-001"

    over_limit = client.get(path, params={"limit": 11})
    assert over_limit.status_code == 400
