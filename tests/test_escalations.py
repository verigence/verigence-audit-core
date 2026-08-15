from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.escalations import (
    create_escalation_with_task,
    get_escalation,
    resolve_escalation,
)
from audit_core.workflow import (
    claim_workflow_task,
    complete_workflow_task,
    get_workflow_task,
    start_workflow_task,
)


def test_escalation_is_traceable_durable_and_does_not_change_delivery_status() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for escalation integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-escalation-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"ECAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Escalation OEM') RETURNING oem_id"
            ),
            {"code": f"EOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Escalation Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"EP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Escalation Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"ED-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Escalation Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"EO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Escalation Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'ESCALATION-JOURNEY'
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
                INSERT INTO auditcore.business_status_codes (
                    tenant_id, domain_key, status_code, status_label
                ) VALUES (
                    :tenant_id, 'DELIVERY', 'DELIVERED', 'Delivered'
                )
                """
            ),
            {"tenant_id": tenant_id},
        )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.deliveries (
                    tenant_id, journey_id, actual_delivery_status_code,
                    status_label_snapshot, status_source, recorded_by_actor_id
                ) VALUES (
                    :tenant_id, :journey_id, 'DELIVERED',
                    'Delivered', 'SOURCE_SYSTEM', 'source-system'
                )
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        )

        escalation_id, task_id = create_escalation_with_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            escalation_type="DEALER_DISPUTE",
            summary="Booking dispute requires project follow-up",
            effect_key=f"escalation-{suffix}",
            severity="HIGH",
            assigned_role_code="PM",
            assigned_actor_id="pm-1",
            details="Operational dispute recorded for audit follow-up only.",
            created_by_actor_id="pc-1",
            correlation_id=f"escalation-{suffix}",
        )
        task = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
        )
        assert task["task_status"] == "READY"
        assert task["assigned_role_code"] == "PM"
        assert task["assigned_actor_id"] == "pm-1"
        assert task["task_payload"]["escalationId"] == str(escalation_id)

        claim_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id="pm-1",
        )
        start_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id="pm-1",
        )
        resolve_escalation(
            connection,
            tenant_id=tenant_id,
            escalation_id=escalation_id,
            resolution_notes="Follow-up completed without changing dealer transaction status.",
        )
        complete_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id="pm-1",
        )

    engine.dispose()

    restarted_engine = create_engine(database_url)
    try:
        with restarted_engine.begin() as connection:
            escalation = get_escalation(
                connection,
                tenant_id=tenant_id,
                escalation_id=escalation_id,
            )
            assert escalation["escalation_status"] == "RESOLVED"
            assert escalation["assigned_role_code"] == "PM"
            assert escalation["assigned_actor_id"] == "pm-1"
            assert escalation["resolved_at_utc"] is not None

            task = get_workflow_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=task_id,
            )
            assert task["task_status"] == "COMPLETED"

            state = connection.execute(
                text(
                    """
                    SELECT j.audit_state, j.audit_outcome,
                           d.actual_delivery_status_code
                    FROM auditcore.journeys j
                    JOIN auditcore.deliveries d
                      ON d.tenant_id = j.tenant_id AND d.journey_id = j.journey_id
                    WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).mappings().one()
            assert state["audit_state"] == "NOT_STARTED"
            assert state["audit_outcome"] == "PENDING"
            assert state["actual_delivery_status_code"] == "DELIVERED"
    finally:
        restarted_engine.dispose()
