"""P2 deal actions: pricing date and recheck."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    create_p2_journey,
    database_engine,
    queue_row,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2deal")
    with engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.bookings (tenant_id, journey_id, booking_date) VALUES (:t, :j, DATE '2026-09-01')"),
            {"t": created.tenant_id, "j": created.journey_id},
        )
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def test_pricing_summary_and_validation(journey):
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pricing"
    summary = client.get(base).json()
    assert summary["bookingDate"] == "2026-09-01" and summary["appliedDate"] == "2026-09-01"
    assert summary["basis"] == "BOOKING_DATE" and summary["modelChange"] == "CONFIRM_SKU"
    # no invoice yet / no reason / no price list on that date
    assert client.put(base, json={"basis": "INVOICE_DATE", "reason": "late invoice"}).status_code == 422
    assert client.put(base, json={"basis": "CUSTOM", "onDate": "2026-09-20"}).status_code == 422
    assert client.put(base, json={"basis": "CUSTOM", "onDate": "2026-09-20", "reason": "price revision"}).status_code == 422
    # resetting to the booking date needs no reason
    reset = client.put(base, json={"basis": "BOOKING_DATE"})
    assert reset.status_code == 200, reset.text
    assert reset.json()["appliedDate"] == "2026-09-01"


def test_recheck_requests_reconcile_stage_and_checks(journey):
    client = TestClient(app, raise_server_exceptions=False)
    response = client.post(f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}:recheck")
    assert response.status_code == 202, response.text
    assert queue_row(journey, "JOURNEY_RECONCILE", str(journey.journey_id)) is not None
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        stage_work = connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_work_queue WHERE tenant_id=:t AND journey_id=:j "
                 "AND work_type='STAGE_RECOMPUTE'"), {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
    assert stage_work == 1
    assert "NATIVE:BOOKING" in response.json()["checks"]
