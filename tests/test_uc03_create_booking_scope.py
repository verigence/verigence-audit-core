from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.uc03_create_booking import _selected_pc_outlet


def test_selected_pc_satellite_outlet_is_usable_and_unassigned_outlet_is_denied() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for UC03 Create Booking scope integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-uc03-create-{suffix}"
    actor_id = f"uc03-pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.product_categories (category_code, category_name)
                VALUES (:code, :name)
                RETURNING product_category_id
                """
            ),
            {"code": f"UC03-CREATE-CAT-{suffix}", "name": f"Create Category {suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.oems (oem_code, oem_name)
                VALUES (:code, :name)
                RETURNING oem_id
                """
            ),
            {"code": f"UC03-CREATE-OEM-{suffix}", "name": f"Create OEM {suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date, project_status
                ) VALUES (
                    :tenant_id, :project_code, 'Create Booking Project', :oem_id,
                    :category_id, CURRENT_DATE - 1, 'ACTIVE'
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "project_code": f"P-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name)
                VALUES (:tenant_id, :code, 'Satellite Dealer')
                RETURNING dealer_id
                """
            ),
            {"tenant_id": tenant_id, "code": f"D-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name, outlet_classification
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Satellite Outlet', 'SATELLITE'
                )
                RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"O-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.business_assignments (
                    tenant_id, security_actor_id, business_role_code, dealer_id, outlet_id
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

    with engine.begin() as connection:
        connection.execute(text("SET LOCAL ROLE audit_core_runtime"))
        set_tenant_context(connection, tenant_id)
        selected = _selected_pc_outlet(
            connection,
            tenant_id=tenant_id,
            actor_id=actor_id,
            outlet_id=outlet_id,
        )
        assert selected["dealer_id"] == dealer_id
        assert selected["outlet_id"] == outlet_id
        assert selected["outlet_classification"] == "SATELLITE"

        with pytest.raises(AuthorizationError):
            _selected_pc_outlet(
                connection,
                tenant_id=tenant_id,
                actor_id=actor_id,
                outlet_id=uuid4(),
            )

    engine.dispose()
