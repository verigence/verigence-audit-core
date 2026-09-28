"""P2 bookings screen and the existing journey workflow: internal Journey
ID, open/closed split, summary, submission (timer rule) and milestones."""
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
def journey(monkeypatch):
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
    # submission's background rules are exercised elsewhere
    import audit_core.uc03_p2_submission as submission

    monkeypatch.setattr(submission, "_after_submit", lambda *a, **k: None)
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


def test_submission_waits_for_classification_or_four_minutes(journey):
    base = f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}"
    client = _client()
    assert client.post(f"{base}:submit").status_code == 409  # nothing uploaded
    batch_id, _ = add_batch_pages(journey, [("booking_form", "CLASSIFYING"), ("pan_card", "READY")])
    status = client.get(f"{base}/documents").json()
    assert status["counts"]["documents"] == 2 and status["counts"]["extracted"] == 1
    assert status["submission"]["canSubmit"] is False and status["submission"]["secondsRemaining"] > 0
    assert client.post(f"{base}:submit").status_code == 409

    # 4 minutes later the PC may submit even though a page is still being identified
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - interval '5 minutes' "
                 "WHERE tenant_id=:t AND batch_id=:b"), {"t": journey.tenant_id, "b": batch_id},
        )
    assert client.get(f"{base}/documents").json()["submission"]["canSubmit"] is True
    response = client.post(f"{base}:submit")
    assert response.status_code == 200, response.text
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        stage = connection.execute(
            text("SELECT business_status, capture_completed_at_utc, version_no FROM auditcore.journey_stage_states "
                 "WHERE tenant_id=:t AND journey_id=:j AND stage_code='BOOKING'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().one()
        event = connection.execute(
            text("SELECT actor_id, actor_role_snapshot FROM auditcore.journey_workflow_events "
                 "WHERE tenant_id=:t AND journey_id=:j AND event_type='P2_BOOKING_DOCUMENTS_SUBMITTED'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).mappings().one()
    assert stage["business_status"] == "BOOKING_IN_PROGRESS" and stage["capture_completed_at_utc"] is not None
    assert stage["version_no"] == 2
    assert event["actor_id"] == journey.actor_id
    # after submitting, the timer restarts only with new uploads
    after = client.get(f"{base}/documents").json()["submission"]
    assert after["canSubmit"] is False and after["submittedAtUtc"] is not None


def test_booking_completion_closes_the_existing_stage_and_lists_split(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert mark_booking_completed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        assert not mark_booking_completed(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    client = _client()
    base = f"/p2/v1/tenants/{journey.tenant_id}"
    [row] = client.get(f"{base}/journeys", params={"state": "open"}).json()["items"]
    assert row["current_stage"] == "BOOKING_COMPLETE" and row["closed"] is False
    assert row["booking_completed_at"] is not None
    assert client.get(f"{base}/journeys", params={"state": "closed"}).json()["items"] == []
    summary = client.get(f"{base}/journeys:summary").json()
    assert summary["week"]["bookingsCompleted"] == 1 and summary["open"]["bookings"] == 0
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
        assert "CORPORATE_CUSTOMER" not in condition_reasons(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        connection.execute(
            text("""INSERT INTO auditcore.commercial_line_source_values (tenant_id, journey_id, line_kind,
                    component_key, source_document_type, amount, source_document_id)
                    VALUES (:t, :j, 'DISCOUNT', 'CORPORATE_PRIVILEGE', 'booking_form', 5000, :d)"""),
            {"t": journey.tenant_id, "j": journey.journey_id, "d": uuid4()},
        )
        reasons = condition_reasons(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert "corporate discount" in reasons["CORPORATE_CUSTOMER"]
    checklist = _client().get(
        f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/documents").json()["checklist"]
    corporate = [c for c in checklist if c["templateKey"] == "corporate_id"]
    assert corporate and "corporate discount" in corporate[0]["reason"]
