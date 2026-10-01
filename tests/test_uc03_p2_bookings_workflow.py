"""P2 bookings screen and the existing journey workflow: internal Journey
ID, open/closed split, summary, upload counts and milestones."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_stage import condition_reasons
from audit_core.uc03_p2_workflow import mark_booking_completed


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2bk")
    with engine.begin() as connection:
        set_tenant_context(connection, created.tenant_id)
        connection.execute(
            text("""INSERT INTO auditcore.journey_stage_states
                (tenant_id, journey_id, stage_code, business_status, audit_state, audit_status,
                 first_started_at_utc, latest_activity_at_utc, version_no)
                VALUES (:t, :j, 'BOOKING', 'BOOKING_STARTED', 'NOT_STARTED', 'NOT_EVALUATED', now(), now(), 1)"""),
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


def _client():
    return TestClient(app, raise_server_exceptions=False)


def test_new_journeys_get_an_internal_journey_id(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        reference = connection.execute(
            text("""INSERT INTO auditcore.journeys (tenant_id, dealer_id, outlet_id, customer_id)
                    VALUES (:t, :d, :o, :c) RETURNING journey_reference"""),
            {"t": journey.tenant_id, "d": journey.dealer_id, "o": journey.outlet_id, "c": journey.customer_id},
        ).scalar_one()
    assert reference.startswith("VJ") and len(reference) == len("VJ2609-000001")


def test_documents_show_upload_counts_and_no_submit_step(journey):
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    client = _client()
    add_batch_pages(journey, [("booking_form", "CLASSIFYING"), ("pan_card", "READY")])
    status = client.get(f"{base}/documents").json()
    assert status["counts"]["documents"] == 2 and status["counts"]["extracted"] == 1
    assert status["counts"]["notClassified"] == 1
    assert "submission" not in status
    assert client.post(f"{base}:submit").status_code in (404, 405)  # completion is rule driven
    # 2026-09-30: the list says when the journey opened for the PC (the first
    # upload) and names the outlet by its id, for the TL's columns.
    [row] = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys", params={"state": "open"}).json()["items"]
    assert row["opened_at"] is not None and row["outlet_code"]


def test_booking_completion_closes_the_existing_stage_and_lists_split(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert mark_booking_completed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        assert not mark_booking_completed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        # The stage engine writes its stage record before it marks the booking completed.
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage, "
                 "booking_completion_state) VALUES (:t, :j, 'BOOKING_COMPLETE', 'COMPLETE')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    client = _client()
    base = f"/p2/v1/tenants/{journey.tenant_id}"
    [row] = client.get(f"{base}/journeys", params={"state": "open"}).json()["items"]
    assert row["current_stage"] == "BOOKING_COMPLETE" and row["closed"] is False
    assert row["booking_completed_at"] is not None and row["delivery_reviewed_at"] is None
    assert client.get(f"{base}/journeys", params={"state": "closed"}).json()["items"] == []
    summary = client.get(f"{base}/journeys:summary").json()
    assert summary["week"]["bookingsCompleted"] == 1 and summary["open"]["bookings"] == 0
    assert summary["closed"] == {"bookings": 1, "deliveries": 0} and summary["tasks"]["open"] >= 0
    timeline = client.get(f"{base}/journeys/{journey.journey_id}/360/timeline").json()
    assert timeline["stages"]["BOOKING"]["status"] == "BOOKING_CLOSED"
    assert timeline["stages"]["BOOKING"]["completedAtUtc"] is not None
    assert any(e["event_type"] == "P2_BOOKING_COMPLETED" for e in timeline["workflowEvents"])

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_stage_states SET business_status='BOOKING_CANCELLED' "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id},
        )
    [closed] = client.get(f"{base}/journeys", params={"state": "closed"}).json()["items"]
    assert closed["cancelled"] is True


def test_claimed_corporate_discount_requires_the_corporate_id(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert "corporateDiscount" not in condition_reasons(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        connection.execute(
            text("""INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind,
                    component_key, source_document_type, amount, source_document_id)
                    VALUES (:t, :j, 'DISCOUNT', 'CORPORATE_PRIVILEGE', 'booking_form', 5000, :d)"""),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": uuid4()},
        )
        reasons = condition_reasons(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert "corporate privilege of ₹5,000" in reasons["corporateDiscount"]
    checklist = _client().get(
        f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/documents").json()["checklist"]
    corporate = [c for c in checklist if c["templateKey"] == "corporate_id"]
    assert corporate and corporate[0]["requirement"] == "REQUIRED" and corporate[0]["status"] == "MISSING"
    assert "corporate privilege" in corporate[0]["reason"]


def test_a_gate_pass_date_alone_never_shows_the_journey_as_delivered(journey):
    """Issue 7 (2026-09-30): the Gate Pass gives the day the vehicle went
    out (deliveries.actual_delivered_at). Delivery completion is the stage
    engine's decision (required documents in, vehicle proof, tasks closed);
    the list, the closed split and the KPIs must not read the date as it."""
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("""INSERT INTO auditcore.deliveries (tenant_id, journey_id, actual_delivered_at, status_source)
                    VALUES (:t, :j, now(), 'EVIDENCE')"""),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    client = _client()
    base = f"/p2/v1/tenants/{journey.tenant_id}"
    [row] = client.get(f"{base}/journeys", params={"state": "open"}).json()["items"]
    assert row["closed"] is False and row["delivery_completed_at"] is None
    assert row["delivered_at"] is not None  # the gate pass date itself still shows
    assert client.get(f"{base}/journeys", params={"state": "closed"}).json()["items"] == []
    summary = client.get(f"{base}/journeys:summary").json()
    assert summary["closed"]["deliveries"] == 0
    timeline = client.get(f"{base}/journeys/{journey.journey_id}/360/timeline").json()
    assert timeline["stages"]["DELIVERY"]["completedAtUtc"] is None
