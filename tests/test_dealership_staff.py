import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.dealership_staff import (
    create_staff_reference,
    get_staff_reference,
    inactivate_staff_reference,
)


def test_booking_can_reference_dealership_staff_without_security_identity() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for dealership staff integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-staff-{suffix}"
    try:
        with engine.begin() as connection:
            category_id = connection.execute(
                text(
                    "INSERT INTO auditcore.product_categories (category_code, category_name) "
                    "VALUES (:code, :name) RETURNING product_category_id"
                ),
                {"code": f"SCAT-{suffix}", "name": f"Category {suffix}"},
            ).scalar_one()
            oem_id = connection.execute(
                text(
                    "INSERT INTO auditcore.oems (oem_code, oem_name) "
                    "VALUES (:code, :name) RETURNING oem_id"
                ),
                {"code": f"SOEM-{suffix}", "name": f"OEM {suffix}"},
            ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.projects (
                        tenant_id, project_code, project_name, oem_id,
                        product_category_id, effective_start_date
                    ) VALUES (
                        :tenant_id, :code, 'Staff Test', :oem_id,
                        :category_id, CURRENT_DATE
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "code": f"SP-{suffix}",
                    "oem_id": oem_id,
                    "category_id": category_id,
                },
            )
            dealer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                    VALUES (:tenant_id, :code, 'Staff Dealer') RETURNING dealer_id
                    """
                ),
                {"tenant_id": tenant_id, "code": f"SD-{suffix}"},
            ).scalar_one()
            outlet_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.dealer_outlets (
                        tenant_id, dealer_id, outlet_code, outlet_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :code, 'Staff Outlet'
                    ) RETURNING outlet_id
                    """
                ),
                {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"SO-{suffix}"},
            ).scalar_one()

            staff_id = create_staff_reference(
                connection,
                tenant_id=tenant_id,
                dealer_id=dealer_id,
                outlet_id=outlet_id,
                staff_role_code="SALES_CONSULTANT",
                display_name="Dealer Sales Consultant",
                employee_reference="EMP-101",
            )

            customer_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.customers (
                        tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                    ) VALUES (
                        :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Customer One'
                    ) RETURNING customer_id
                    """
                ),
                {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
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
            booking_staff_id = connection.execute(
                text(
                    """
                    INSERT INTO auditcore.bookings (tenant_id, journey_id, sales_staff_id)
                    VALUES (:tenant_id, :journey_id, :staff_id)
                    RETURNING sales_staff_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id, "staff_id": staff_id},
            ).scalar_one()

            assert booking_staff_id == staff_id
            assert get_staff_reference(
                connection,
                tenant_id=tenant_id,
                dealership_staff_id=staff_id,
            )["display_name"] == "Dealer Sales Consultant"

            inactivate_staff_reference(
                connection,
                tenant_id=tenant_id,
                dealership_staff_id=staff_id,
            )
            assert get_staff_reference(
                connection,
                tenant_id=tenant_id,
                dealership_staff_id=staff_id,
            )["status"] == "INACTIVE"
    finally:
        engine.dispose()
