from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal
from audit_core.workflow import create_workflow_task


def test_task_api_completes_idempotently_and_preserves_history() -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for task API integration test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-task-api-{suffix}"
    actor_id = f"actor-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"TCAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Task OEM') RETURNING oem_id"
            ),
            {"code": f"TOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Task Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"TP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Task Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"TD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Task Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"TO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Task Customer'
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
                    :tenant_id, :dealer_id, :outlet_id, :customer_id, 'TASK-API-JOURNEY'
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
                    :tenant_id, :actor_id, 'TL', :dealer_id, :outlet_id
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
            effect_key=f"task-api-{suffix}",
        )

    def connection_override():
        with engine.begin() as connection:
            yield connection

    app.dependency_overrides[get_connection] = connection_override
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=actor_id,
        tenant_id=tenant_id,
        permissions=("audit.work.read", "audit.work.update", "audit.work.manage"),
    )
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/v1/tenants/{tenant_id}/tasks/{task_id}"

    try:
        listed = client.get(f"/v1/tenants/{tenant_id}/tasks")
        assert listed.status_code == 200, listed.text
        assert [item["taskId"] for item in listed.json()] == [str(task_id)]

        claimed = client.post(f"{base}/claim")
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["status"] == "CLAIMED"
        assert claimed.json()["assignedActorId"] == actor_id

        started = client.post(f"{base}/start")
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "IN_PROGRESS"

        missing_key = client.post(f"{base}/complete")
        assert missing_key.status_code == 400

        headers = {"Idempotency-Key": f"complete-{suffix}"}
        completed = client.post(f"{base}/complete", headers=headers)
        assert completed.status_code == 200, completed.text
        assert completed.json()["status"] == "COMPLETED"

        replayed = client.post(f"{base}/complete", headers=headers)
        assert replayed.status_code == 200, replayed.text
        assert replayed.json() == completed.json()

        history = client.get(f"{base}/history")
        assert history.status_code == 200, history.text
        assert [event["eventType"] for event in history.json()] == [
            "CREATED",
            "CLAIMED",
            "STARTED",
            "COMPLETED",
        ]

        with engine.begin() as connection:
            completion_events = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.workflow_task_events
                    WHERE tenant_id = :tenant_id AND workflow_task_id = :task_id
                      AND event_type = 'COMPLETED'
                    """
                ),
                {"tenant_id": tenant_id, "task_id": task_id},
            ).scalar_one()
            idempotency_records = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.idempotency_records
                    WHERE tenant_id = :tenant_id
                      AND operation_key = :operation_key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "operation_key": f"task.complete:{task_id}",
                },
            ).scalar_one()
        assert completion_events == 1
        assert idempotency_records == 1
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
