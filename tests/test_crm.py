from __future__ import annotations

import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text

from audit_core.crm import (
    create_crm_interaction_with_task,
    get_crm_interaction,
    record_crm_outcome,
)
from audit_core.workflow import (
    claim_workflow_task,
    complete_workflow_task,
    get_workflow_task,
    start_workflow_task,
)


def test_crm_interaction_uses_durable_task_and_retains_outcome() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for CRM integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-crm-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"CCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'CRM OEM') RETURNING oem_id"
            ),
            {"code": f"COEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'CRM Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"CP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'CRM Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"CD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'CRM Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"CO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'CRM Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'CRM-JOURNEY'
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

        effect_key = f"crm-call-{suffix}"
        interaction_id = create_crm_interaction_with_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            interaction_type="MANDATORY_FOLLOW_UP",
            effect_key=effect_key,
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            notes="Configured trigger requested a CRM call.",
            correlation_id=f"crm-{suffix}",
        )
        replay_id = create_crm_interaction_with_task(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            interaction_type="MANDATORY_FOLLOW_UP",
            effect_key=effect_key,
            dealer_id=dealer_id,
            outlet_id=outlet_id,
            correlation_id=f"crm-{suffix}",
        )
        assert replay_id == interaction_id

        interaction = get_crm_interaction(
            connection,
            tenant_id=tenant_id,
            crm_interaction_id=interaction_id,
        )
        task_id = interaction["workflow_task_id"]
        assert task_id is not None
        assert interaction["interaction_status"] == "PENDING"

        task = get_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
        )
        assert task["task_status"] == "READY"
        assert task["process_area"] == "CRM"
        assert task["assigned_role_code"] == "CRM"

        claim_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id="crm-actor",
        )
        start_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id="crm-actor",
        )
        record_crm_outcome(
            connection,
            tenant_id=tenant_id,
            crm_interaction_id=interaction_id,
            actor_id="crm-actor",
            interaction_status="COMPLETED",
            outcome_code="CONTACTED",
            notes="Customer contacted; outcome recorded.",
        )
        complete_workflow_task(
            connection,
            tenant_id=tenant_id,
            workflow_task_id=task_id,
            actor_id="crm-actor",
        )

    engine.dispose()

    restarted_engine = create_engine(database_url)
    try:
        with restarted_engine.begin() as connection:
            persisted = get_crm_interaction(
                connection,
                tenant_id=tenant_id,
                crm_interaction_id=interaction_id,
            )
            assert persisted["interaction_status"] == "COMPLETED"
            assert persisted["outcome_code"] == "CONTACTED"
            assert persisted["actor_id"] == "crm-actor"
            assert persisted["attempted_at_utc"] is not None
            assert persisted["completed_at_utc"] is not None

            persisted_task = get_workflow_task(
                connection,
                tenant_id=tenant_id,
                workflow_task_id=task_id,
            )
            assert persisted_task["task_status"] == "COMPLETED"

            counts = connection.execute(
                text(
                    """
                    SELECT
                        (SELECT count(*) FROM auditcore.crm_interactions
                         WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id) AS interactions,
                        (SELECT count(*) FROM auditcore.workflow_tasks
                         WHERE tenant_id = :tenant_id AND effect_key = :effect_key) AS tasks
                    """
                ),
                {"tenant_id": tenant_id, "task_id": task_id, "effect_key": effect_key},
            ).mappings().one()
            assert counts["interactions"] == 1
            assert counts["tasks"] == 1
    finally:
        restarted_engine.dispose()
