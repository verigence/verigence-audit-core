from __future__ import annotations

import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from audit_core.dependencies import get_connection, get_principal
from audit_core.main import app
from audit_core.security import Principal


def test_daily_ops_routes_persist_operational_work() -> None:
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
    finally:
        app.dependency_overrides.clear()
        engine.dispose()
