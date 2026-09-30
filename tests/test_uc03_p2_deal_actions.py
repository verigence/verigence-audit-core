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
    assert summary["bookingDateMissing"] is False
    # no invoice yet; any other date is not a choice (decision 2026-09-30)
    assert client.put(base, json={"basis": "INVOICE_DATE", "reason": "late invoice"}).status_code == 422
    assert client.put(base, json={"basis": "CUSTOM", "onDate": "2026-09-20", "reason": "price revision"}).status_code == 400
    # resetting to the booking date needs no reason
    reset = client.put(base, json={"basis": "BOOKING_DATE"})
    assert reset.status_code == 200, reset.text
    assert reset.json()["appliedDate"] == "2026-09-01"


def test_the_booking_form_date_prices_the_deal_and_is_never_guessed(journey):
    """#38 (2026-09-30): the booking date on the booking form is the pricing
    date; without one the deal is not priced (no fallback to today) and the
    booking-date basis cannot be applied."""
    from p2_support import add_ready_document

    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pricing"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.bookings SET booking_date=NULL WHERE journey_id=:j"),
                           {"j": journey.journey_id})
    summary = client.get(base).json()
    assert summary["bookingDateMissing"] is True and summary["appliedDate"] is None
    assert summary["appliedPriceList"] is None and summary["options"] == []
    assert client.put(base, json={"basis": "BOOKING_DATE"}).status_code == 422

    add_ready_document(journey, "booking_form", booking_date="05/09/2026", customer_name="A")
    summary = client.get(base).json()
    assert summary["bookingDate"] == "2026-09-05" and summary["appliedDate"] == "2026-09-05"
    assert summary["bookingDateMissing"] is False
    assert [o["basis"] for o in summary["options"]] == ["BOOKING_DATE"]


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


def test_insurance_source_is_inhouse_until_the_pc_says_self(journey):
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    assert client.get(f"{base}/360/deal").json()["insurance"]["source"] == "INHOUSE"
    refused = client.put(f"{base}/insurance-source", json={"source": "SELF"})
    assert refused.status_code == 422 and "how you know" in refused.json()["detail"]
    self_insured = client.put(f"{base}/insurance-source", json={"source": "SELF", "reason": "Own policy shown"})
    assert self_insured.status_code == 200, self_insured.text
    assert self_insured.json()["source"] == "SELF" and self_insured.json()["decidedBy"] == "PC"
    assert client.put(f"{base}/insurance-source", json={"source": "INHOUSE"}).json()["source"] == "INHOUSE"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        events = connection.execute(
            text("SELECT details->>'source' FROM auditcore.p2_activity_events WHERE tenant_id=:t AND journey_id=:j "
                 "AND event_type='INSURANCE_SOURCE_SET' ORDER BY created_at_utc"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalars().all()
    assert events == ["SELF", "INHOUSE"]


def test_management_referral_is_the_team_leads_call(journey):
    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    body = {"opted": True, "amount": "15000", "reason": "Referred by the dealer principal"}
    assert client.put(f"{base}/management-referral", json=body).status_code == 403  # a PC
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization(role_key="TL")
    refused = client.put(f"{base}/management-referral", json={"opted": True, "reason": "Referred"})
    assert refused.status_code == 422 and "amount" in refused.json()["detail"]
    approved = client.put(f"{base}/management-referral", json=body)
    assert approved.status_code == 200, approved.text
    assert approved.json()["opted"] is True and approved.json()["amount"] == "15000.00" and approved.json()["setByRole"] == "TL"
    row = next(d for d in client.get(f"{base}/360/deal").json()["discounts"] if d["key"] == "MANAGEMENT_REFERRAL")
    assert row["entitled"] == "15000.00" and row["opted"]["taken"] is True
    out = client.put(f"{base}/management-referral", json={"opted": False, "reason": "Withdrawn after review"})
    assert out.status_code == 200 and out.json()["opted"] is False and out.json()["amount"] is None


def test_the_enable_mr_task_is_raised_and_completed_by_the_team_lead(journey):
    """Decision 2026-09-30: MR is enabled through a task the TL raises on the
    journey; completing it with the amount and reason switches MR on."""
    from audit_core.uc03_p2_tasks import submit_action

    client = TestClient(app, raise_server_exceptions=False)
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    raise_body = {"taskType": "TL_MANAGEMENT_REFERRAL", "category": "PROCESS_CONFIRMATION",
                  "title": "Enable Management Referral (MR)", "description": "Enable MR of 15000. Dealer principal referral.",
                  "assignedRoleCode": "TL", "allowedActions": ["COMPLETE_ACTION", "ADD_COMMENT"],
                  "reference": {"kind": "MANAGEMENT_REFERRAL", "proposedAmount": "15000", "reason": "Dealer principal referral"}}
    assert client.post(f"{base}/tasks", json=raise_body).status_code == 403  # a PC cannot raise it
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization(role_key="TL")
    raised = client.post(f"{base}/tasks", json=raise_body)
    assert raised.status_code == 200, raised.text
    task_id = raised.json()["taskId"]
    pending = next(d for d in client.get(f"{base}/360/deal").json()["discounts"] if d["key"] == "MANAGEMENT_REFERRAL")
    assert pending["opted"]["taken"] is False and pending["management"]["task"]["taskId"] == task_id
    assert pending["management"]["task"]["proposedAmount"] == "15000"

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        with pytest.raises(ValueError, match="assigned to role TL"):
            submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action="COMPLETE_ACTION",
                          actor_id=journey.actor_id, actor_role_code="PC", comment=None, details={})
        done = submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action="COMPLETE_ACTION",
                             actor_id="tl-1", actor_role_code="TL", comment=None, details={"amount": "15000"})
    assert done == {"taskId": task_id, "status": "VERIFIED_COMPLETE", "outcome": "MR_ENABLED"}
    enabled = next(d for d in client.get(f"{base}/360/deal").json()["discounts"] if d["key"] == "MANAGEMENT_REFERRAL")
    assert enabled["opted"]["taken"] is True and enabled["entitled"] == "15000.00" and enabled["management"]["task"] is None
    assert enabled["management"]["reason"] == "Dealer principal referral" and enabled["management"]["setByRole"] == "TL"
