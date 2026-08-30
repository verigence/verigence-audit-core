from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.uc03_attribute_resolution import apply_customer_relationship_review


def test_booking_stage_customer_and_delivery_links_share_one_journey() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 stage-link integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-stage-link-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"SL-PCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Stage Link OEM') RETURNING oem_id"
            ),
            {"code": f"SL-OEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Stage Link Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"SL-P-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Stage Link Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"SL-D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Stage Link Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"SL-O-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'PENDING', 'PC Entered Name'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'UC03-STAGE-LINK'
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

        # Generic Journey creation alone must not manufacture a Booking row.
        before_stage = connection.execute(
            text(
                "SELECT booking_id FROM auditcore.journeys "
                "WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        assert before_stage is None

        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_stage_states (
                    tenant_id, journey_id, stage_code, business_status,
                    audit_state, audit_status, version_no
                ) VALUES (
                    :tenant_id, :journey_id, 'BOOKING', 'BOOKING_STARTED',
                    'NOT_STARTED', 'NOT_EVALUATED', 1
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )

        linked = connection.execute(
            text(
                """
                SELECT j.booking_id, c.journey_id AS customer_journey_id,
                       c.booking_id AS customer_booking_id
                FROM auditcore.journeys j
                JOIN auditcore.customers c
                  ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one()
        assert linked["booking_id"] is not None
        assert linked["customer_journey_id"] == journey_id
        assert linked["customer_booking_id"] == linked["booking_id"]

        delivery_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.deliveries (tenant_id, journey_id)
                VALUES (:tenant_id, :journey_id)
                RETURNING delivery_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        delivery_link = connection.execute(
            text(
                """
                SELECT d.booking_id, j.booking_id AS journey_booking_id,
                       j.delivery_id AS journey_delivery_id
                FROM auditcore.deliveries d
                JOIN auditcore.journeys j
                  ON j.tenant_id=d.tenant_id AND j.journey_id=d.journey_id
                WHERE d.tenant_id=:tenant_id AND d.delivery_id=:delivery_id
                """
            ),
            {"tenant_id": tenant_id, "delivery_id": delivery_id},
        ).mappings().one()
        assert delivery_link["booking_id"] == linked["booking_id"]
        assert delivery_link["journey_booking_id"] == linked["booking_id"]
        assert delivery_link["journey_delivery_id"] == delivery_id

        type_result = apply_customer_relationship_review(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            attribute_key="customer_relationship_type",
            value="S/O",
            actor_id="pc-test",
        )
        name_result = apply_customer_relationship_review(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            attribute_key="customer_relationship_name",
            value="Document Parent Name",
            actor_id="pc-test",
        )
        assert type_result[2] == "APPLIED"
        assert name_result[2] == "APPLIED"

        customer = connection.execute(
            text(
                """
                SELECT display_name, relationship_type, relationship_name
                FROM auditcore.customers
                WHERE tenant_id=:tenant_id AND customer_id=:customer_id
                """
            ),
            {"tenant_id": tenant_id, "customer_id": customer_id},
        ).mappings().one()
        assert customer["display_name"] == "PC Entered Name"
        assert customer["relationship_type"] == "S/O"
        assert customer["relationship_name"] == "Document Parent Name"

    engine.dispose()
