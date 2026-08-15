from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.workflow import create_workflow_task, get_workflow_task


def test_workflow_task_survives_new_engine_process_boundary() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for workflow persistence test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-workflow-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"WCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Workflow OEM') RETURNING oem_id"
            ),
            {"code": f"WOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Workflow Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"WP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Workflow Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"WD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Workflow Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"WO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Workflow Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'WORKFLOW-JOURNEY'
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
        task_id = create_workflow_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            workflow_type="AUDIT_REVIEW",
            process_area="AUDIT",
            task_type="TL_REVIEW",
            assigned_role_code="TL",
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            task_payload={"reason": "PC_SUBMITTED"},
            correlation_id="workflow-persist-1",
        )

    engine.dispose()

    restarted_engine = create_engine(database_url)
    try:
        with restarted_engine.begin() as connection:
            task = get_workflow_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=task_id,
            )
            assert task["task_status"] == "READY"
            assert task["workflow_status"] == "ACTIVE"
            assert task["task_type"] == "TL_REVIEW"
            assert task["assigned_role_code"] == "TL"
            assert task["task_payload"] == {"reason": "PC_SUBMITTED"}
            event_count = connection.execute(
                text(
                    """
                    SELECT count(*)
                    FROM auditcore.workflow_task_events
                    WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                    """
                ),
                {"tenant_id": tenant_id, "task_id": task_id},
            ).scalar_one()
            assert event_count == 1
    finally:
        restarted_engine.dispose()
