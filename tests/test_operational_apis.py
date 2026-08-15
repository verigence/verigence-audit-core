from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_daily_crm_and_escalation_routes_persist_operational_work() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for operational API integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-operational-api-{suffix}"
    actor_id = f"actor-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"OCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Operational OEM') RETURNING oem_id"
            ),
            {"code": f"OOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Operational Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"OP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Operational Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"OD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Operational Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"OO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Operational Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'OPERATIONAL-JOURNEY'
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
                    :tenant_id, :actor_id, 'PM', :dealer_id, :outlet_id
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
        permissions=(
            "audit.daily_ops.read",
            "audit.daily_ops.execute",
            "audit.crm.read",
            "audit.crm.manage",
            "audit.escalation.read",
            "audit.escalation.manage",
        ),
    )
    client = TestClient(app, raise_server_exceptions=False)

    try:
        daily_base = f"/v1/tenants/{tenant_id}/outlets/{outlet_id}/daily-ops"
        created_daily = client.post(daily_base, json={"businessDate": "2026-08-15"})
        assert created_daily.status_code == 201, created_daily.text
        run_id = created_daily.json()["runId"]

        completed_daily = client.post(
            f"/v1/tenants/{tenant_id}/daily-ops/{run_id}/complete",
            headers={"Idempotency-Key": f"daily-complete-{suffix}"},
        )
        assert completed_daily.status_code == 200, completed_daily.text
        assert completed_daily.json()["status"] == "COMPLETED"
        replayed_daily = client.post(
            f"/v1/tenants/{tenant_id}/daily-ops/{run_id}/complete",
            headers={"Idempotency-Key": f"daily-complete-{suffix}"},
        )
        assert replayed_daily.status_code == 200, replayed_daily.text
        assert replayed_daily.json() == completed_daily.json()

        journey_base = f"/v1/tenants/{tenant_id}/journeys/{journey_id}"
        crm_key = f"crm-create-{suffix}"
        created_crm = client.post(
            f"{journey_base}/crm-interactions",
            headers={"Idempotency-Key": crm_key},
            json={"interactionType": "MANDATORY_FOLLOW_UP", "notes": "Configured trigger"},
        )
        assert created_crm.status_code == 201, created_crm.text
        crm_id = created_crm.json()["crmInteractionId"]
        crm_task_id = created_crm.json()["workflowTaskId"]
        replayed_crm = client.post(
            f"{journey_base}/crm-interactions",
            headers={"Idempotency-Key": crm_key},
            json={"interactionType": "MANDATORY_FOLLOW_UP", "notes": "Configured trigger"},
        )
        assert replayed_crm.status_code == 201, replayed_crm.text
        assert replayed_crm.json()["crmInteractionId"] == crm_id

        escalation_key = f"escalation-create-{suffix}"
        created_escalation = client.post(
            f"{journey_base}/escalations",
            headers={"Idempotency-Key": escalation_key},
            json={
                "escalationType": "DEALER_DISPUTE",
                "summary": "Dispute requires review",
                "severity": "HIGH",
                "assignedRoleCode": "PM",
                "assignedActorId": actor_id,
            },
        )
        assert created_escalation.status_code == 201, created_escalation.text
        escalation_id = created_escalation.json()["escalationId"]
        escalation_task_id = created_escalation.json()["workflowTaskId"]
        replayed_escalation = client.post(
            f"{journey_base}/escalations",
            headers={"Idempotency-Key": escalation_key},
            json={
                "escalationType": "DEALER_DISPUTE",
                "summary": "Dispute requires review",
                "severity": "HIGH",
                "assignedRoleCode": "PM",
                "assignedActorId": actor_id,
            },
        )
        assert replayed_escalation.status_code == 201, replayed_escalation.text
        assert replayed_escalation.json()["escalationId"] == escalation_id

        resolved = client.patch(
            f"{journey_base}/escalations",
            headers={"Idempotency-Key": f"escalation-resolve-{suffix}"},
            json={
                "escalationId": escalation_id,
                "status": "RESOLVED",
                "resolutionNotes": "Resolved as an audit follow-up only.",
            },
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == "RESOLVED"

        with engine.begin() as connection:
            task_count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.workflow_tasks
                    WHERE tenant_id = :tenant_id
                      AND workflow_task_id IN (:crm_task_id, :escalation_task_id)
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "crm_task_id": crm_task_id,
                    "escalation_task_id": escalation_task_id,
                },
            ).scalar_one()
            escalation_count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.escalations
                    WHERE tenant_id = :tenant_id AND escalation_id = :escalation_id
                    """
                ),
                {"tenant_id": tenant_id, "escalation_id": escalation_id},
            ).scalar_one()
        assert task_count == 2
        assert escalation_count == 1
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
