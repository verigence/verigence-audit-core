from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core import audit_review
from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_pc_submit_rolls_back_when_required_task_insert_fails(monkeypatch) -> None:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL is required for atomic audit transition test")

    engine = create_engine(database_url)
    suffix = uuid4().hex
    tenant_id = f"tenant-atomic-{suffix}"
    actor_id = f"pc-{suffix}"

    with engine.begin() as connection:
        category_id = connection.execute(
            text(
                "INSERT INTO auditcore.product_categories (category_code, category_name) "
                "VALUES (:code, 'Vehicle') RETURNING product_category_id"
            ),
            {"code": f"ACAT-{suffix}"},
        ).scalar_one()
        oem_id = connection.execute(
            text(
                "INSERT INTO auditcore.oems (oem_code, oem_name) "
                "VALUES (:code, 'Atomic OEM') RETURNING oem_id"
            ),
            {"code": f"AOEM-{suffix}"},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.projects (
                    tenant_id, project_code, project_name, oem_id,
                    product_category_id, effective_start_date
                ) VALUES (
                    :tenant_id, :code, 'Atomic Project', :oem_id,
                    :category_id, CURRENT_DATE
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "code": f"AP-{suffix}",
                "oem_id": oem_id,
                "category_id": category_id,
            },
        )
        dealer_id = connection.execute(
            text(
                "INSERT INTO auditcore.dealers (tenant_id, dealer_code, dealer_name) "
                "VALUES (:tenant_id, :code, 'Atomic Dealer') RETURNING dealer_id"
            ),
            {"tenant_id": tenant_id, "code": f"AD-{suffix}"},
        ).scalar_one()
        outlet_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.dealer_outlets (
                    tenant_id, dealer_id, outlet_code, outlet_name
                ) VALUES (
                    :tenant_id, :dealer_id, :code, 'Atomic Outlet'
                ) RETURNING outlet_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "code": f"AO-{suffix}"},
        ).scalar_one()
        customer_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.customers (
                    tenant_id, dealer_id, outlet_id, customer_type_code, display_name
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, 'RETAIL', 'Atomic Customer'
                ) RETURNING customer_id
                """
            ),
            {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
        ).scalar_one()
        journey_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.journeys (
                    tenant_id, dealer_id, outlet_id, customer_id,
                    journey_reference, audit_state, audit_started_at_utc
                ) VALUES (
                    :tenant_id, :dealer_id, :outlet_id, :customer_id,
                    'ATOMIC-JOURNEY', 'IN_PROGRESS', now()
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
                    :tenant_id, :actor_id, 'PC', :dealer_id, :outlet_id
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
        permissions=("audit.journey.submit",),
    )

    def fail_task_creation(*args, **kwargs):
        raise RuntimeError("injected workflow task insert failure")

    monkeypatch.setattr(audit_review, "create_workflow_task", fail_task_creation)
    try:
        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            f"/v1/tenants/{tenant_id}/journeys/{journey_id}/audit/submit",
            headers={"Idempotency-Key": f"atomic-submit-{suffix}"},
        )
        assert response.status_code == 500

        with engine.begin() as connection:
            state = connection.execute(
                text(
                    """
                    SELECT audit_state, audit_outcome
                    FROM auditcore.journeys
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).mappings().one()
            task_count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.workflow_tasks
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).scalar_one()
            audit_event_count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.audit_events
                    WHERE tenant_id = :tenant_id
                      AND entity_type = 'JOURNEY'
                      AND entity_id = :entity_id
                    """
                ),
                {"tenant_id": tenant_id, "entity_id": str(journey_id)},
            ).scalar_one()
            outbox_count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.outbox_events
                    WHERE tenant_id = :tenant_id AND journey_id = :journey_id
                    """
                ),
                {"tenant_id": tenant_id, "journey_id": journey_id},
            ).scalar_one()
            idempotency_count = connection.execute(
                text(
                    """
                    SELECT count(*) FROM auditcore.idempotency_records
                    WHERE tenant_id = :tenant_id
                      AND operation_key = :operation_key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "operation_key": f"audit.submit:{journey_id}",
                },
            ).scalar_one()

        assert state["audit_state"] == "IN_PROGRESS"
        assert state["audit_outcome"] == "PENDING"
        assert task_count == 0
        assert audit_event_count == 0
        assert outbox_count == 0
        assert idempotency_count == 0
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
