from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text


def test_new_project_gets_complete_booking_reference_defaults_and_outlet_district() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Booking reference integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-reference-{suffix}"

    try:
        with engine.begin() as connection:
            category_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.product_categories (category_code, category_name)
                    VALUES (:code, :name)
                    RETURNING product_category_id
                    """
                ),
                {"code": f"UC03-REF-CAT-{suffix}", "name": f"UC03 Reference Category {suffix}"},
            ).scalar_one()
            oem_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.oems (oem_code, oem_name)
                    VALUES (:code, :name)
                    RETURNING oem_id
                    """
                ),
                {"code": f"UC03-REF-OEM-{suffix}", "name": f"UC03 Reference OEM {suffix}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date,
                        timezone_name, project_status
                    ) VALUES (
                        :tenant_id, :project_code, 'UC03 Reference Project', :oem_id,
                        :category_id, CURRENT_DATE, 'Asia/Kolkata', 'ACTIVE'
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_code": f"UC03-REF-{suffix}",
                    "oem_id": oem_id,
                    "category_id": category_id,
                },
            )

            dealer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                    VALUES (:tenant_id, :code, 'Reference Dealer')
                    RETURNING dealer_id
                    """
                ),
                {"tenant_id": tenant_id, "code": f"REF-D-{suffix}"},
            ).scalar_one()
            outlet_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealer_outlets (
                        tenant_id, dealer_id, outlet_code, outlet_name, city
                    ) VALUES (
                        :tenant_id, :dealer_id, :code, 'Reference Outlet', 'Mohali'
                    )
                    RETURNING outlet_id
                    """
                ),
                {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"REF-O-{suffix}"},
            ).scalar_one()

            rows = connection.execute(
                text(
                    """
                    SELECT domain_key, status_code, status_label
                    FROM auditcore.business_status_codes
                    WHERE tenant_id=:tenant_id AND is_active=true
                    """
                ),
                {"tenant_id": tenant_id},
            ).mappings().all()

            # Exercise the exact FK that rejected the live PC Submit Booking path.
            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, 'INDIVIDUAL', 'Trade In Test Customer'
                    )
                    RETURNING customer_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "dealer_id": dealer_id,
                    "outlet_id": outlet_id,
                },
            ).scalar_one()
            journey_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journeys (
                        tenant_id, dealer_id, outlet_id, customer_id
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, :customer_id
                    )
                    RETURNING journey_id
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
                    INSERT INTO auditcore.trade_in_cases (
                        tenant_id, journey_id, actual_status_code, source_kind
                    ) VALUES (
                        :tenant_id, :journey_id, 'NO_EXCHANGE', 'OPERATIONAL_INPUT'
                    )
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            )

        values = {(row["domain_key"], row["status_code"]): row["status_label"] for row in rows}
        assert values[("CUSTOMER_TYPE", "CORPORATE")] == "Corporate"
        assert values[("DEAL_TYPE", "IN_SCOPE")] == "In-Scope"
        assert values[("DEAL_SOURCE", "WALK_IN")] == "Walk-in"
        assert values[("LEAD_SOURCE", "IN_HOUSE")] == "In House"
        assert values[("TERRITORY_CATEGORIZATION", "SAME_TERRITORY")] == "Same Territory"
        assert values[("REGISTRATION_STATE", "PB")] == "Punjab"
        assert values[("REGISTRATION_TYPE", "PERMANENT")] == "Permanent"
        assert values[("REGISTRATION_CATEGORY", "PRIVATE")] == "Private"
        assert values[("DISTRICT", "OTHER")] == "Other / Not Listed"
        assert values[("DISTRICT", "MOHALI")] == "Mohali"
        assert values[("TRADE_IN", "EXCHANGE_TAKEN")] == "Exchange Taken"
        assert values[("TRADE_IN", "NO_EXCHANGE")] == "No Exchange"
    finally:
        engine.dispose()
