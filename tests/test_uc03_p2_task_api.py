"""P2 Task Queue API: enriched worklist rows and task history."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import AllowAllAuthorization, create_p2_journey, database_engine

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_tasks import create_p2_task, submit_action


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2tq")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _task(journey, title="Verify 2 fields on Aadhaar"):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        task_id = create_p2_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            task_type="MANUAL_VERIFICATION_REVIEW", category="DOCUMENT_VERIFICATION", origin_kind="SYSTEM",
            source_type="DOCUMENT_FIELD", source_code="MANUAL_VERIFICATION", dedupe_key=f"t:{uuid4()}",
            title=title, description="Check the values", reference={}, severity="MEDIUM", priority="NORMAL",
            assigned_role_code="PC", assigned_actor_id=None, raised_by_actor_id=None, raised_by_role_code=None,
            allowed_actions=["REVIEW_DOCUMENT", "ADD_COMMENT"], completion_protocol="MACHINE_VERIFIED",
        )
        submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action="ADD_COMMENT",
                      actor_id=journey.actor_id, actor_role_code="PC", comment="Dealer will resend")
    return task_id


def test_worklist_rows_carry_customer_context_and_comment_counts(journey):
    task_id = _task(journey)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks", params={"view": "open", "role": "PC"})
    assert response.status_code == 200, response.text
    [row] = [item for item in response.json()["items"] if item["task_id"] == str(task_id)]
    assert row["customer_name"] == "P2 Customer" and row["outlet_name"] == "O"
    assert row["comment_count"] == 1
    assert response.json()["sources"]["legacy"] == 0  # legacy hidden by default

    done = client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks", params={"view": "done"})
    assert all(item["task_id"] != str(task_id) for item in done.json()["items"])


def test_task_detail_includes_history(journey):
    task_id = _task(journey)
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks/{task_id}")
    assert response.status_code == 200, response.text
    events = response.json()["events"]
    assert [e["event_type"] for e in events] == ["ADD_COMMENT"]
    assert events[0]["comment"] == "Dealer will resend"


def test_tabs_classify_and_count(journey):
    _task(journey)
    client = TestClient(app, raise_server_exceptions=False)
    body = client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks", params={"tab": "manual_verification"}).json()
    assert body["counts"]["MANUAL_VERIFICATION"] == 1 and body["counts"]["DOCUMENTS"] == 0
    assert [i["queue_tab"] for i in body["items"]] == ["MANUAL_VERIFICATION"]
    assert client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks", params={"tab": "documents"}).json()["items"] == []


def test_task_queue_tab_mapping():
    from audit_core.uc03_p2_api import task_queue_tab

    assert task_queue_tab("MANUAL_VERIFICATION_REVIEW", "DOCUMENT_VERIFICATION") == "MANUAL_VERIFICATION"
    assert task_queue_tab("FIELD_CORRECTION_REVIEW_P2", "CORRECTION_APPROVAL") == "MANUAL_VERIFICATION"
    assert task_queue_tab("PC_DOCUMENT_REUPLOAD", "PC_DOCUMENT_REUPLOAD") == "DOCUMENTS"
    assert task_queue_tab("DELIVERY_VEHICLE_PHOTOS_MISSING", "EVIDENCE_GAP") == "DOCUMENTS"
    assert task_queue_tab("RULE_DISCREPANCY_REVIEW", "CROSS_DOCUMENT_REVIEW") == "OTHER"


def test_journey_list_shows_existing_journeys_with_dates(journey):
    from sqlalchemy import text

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, booking_confirm_date) "
                 "VALUES (:t, :j, 'BOOKING', DATE '2026-09-02')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys")
    assert response.status_code == 200, response.text
    [row] = response.json()["items"]
    assert row["booking_confirm_date"] == "2026-09-02"
    assert row["current_stage"] == "BOOKING_COMPLETE" and row["phase2"] is False
    assert row["documents"] == 0 and row["open_tasks"] == 0


def test_existing_phase1_tasks_show_until_the_journey_runs_on_p2(journey):
    from sqlalchemy import text

    from audit_core.workflow import create_workflow_task

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        create_workflow_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, workflow_type="UC03_BOOKING",
            process_area="BOOKING", task_type="PC_DOCUMENT_REUPLOAD", assigned_role_code="PC",
            dealer_id=journey.dealer_id, outlet_id=journey.outlet_id,
            task_payload={"title": "Re-upload the Aadhaar"}, effect_key=f"t:{uuid4()}",
        )
        create_workflow_task(  # background job: never shown
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, workflow_type="UC03_BOOKING",
            process_area="BOOKING", task_type="BOOKING_RULE_EVALUATION", effect_key=f"t:{uuid4()}",
        )
    client = TestClient(app, raise_server_exceptions=False)
    body = client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks").json()
    [legacy] = [i for i in body["items"] if i["source_system"] == "LEGACY"]
    assert legacy["title"] == "Re-upload the Aadhaar" and legacy["queue_tab"] == "DOCUMENTS"
    assert legacy["customer_name"] == "P2 Customer"
    listed = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys").json()["items"][0]
    assert listed["open_tasks"] == 1

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_journey_runtime (tenant_id, journey_id, current_stage) "
                 "VALUES (:t, :j, 'BOOKING_DOCUMENT_UPLOAD')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    body = client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks").json()
    assert not [i for i in body["items"] if i["source_system"] == "LEGACY"]
    assert client.get(f"/p2/v1/tenants/{journey.tenant_id}/tasks", params={"includeLegacy": True}).json()["sources"]["legacy"] == 1


def test_acting_on_someone_elses_task_is_403_not_422(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        task_id = create_p2_task(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            task_type="MANUAL_VERIFICATION_REVIEW", category="DOCUMENT_VERIFICATION", origin_kind="SYSTEM",
            source_type="DOCUMENT_FIELD", source_code="MANUAL_VERIFICATION", dedupe_key=f"t:{uuid4()}",
            title="Someone else's task", description="", reference={}, severity="MEDIUM", priority="NORMAL",
            assigned_role_code="PC", assigned_actor_id="another-actor", raised_by_actor_id=None,
            raised_by_role_code=None, allowed_actions=["REVIEW_DOCUMENT", "ADD_COMMENT"],
            completion_protocol="MACHINE_VERIFIED",
        )
    response = TestClient(app).post(
        f"/p2/v1/tenants/{journey.tenant_id}/tasks/{task_id}/actions", json={"action": "REVIEW_DOCUMENT"},
    )
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["errorCode"] == "VAC-AUTH-002" and body["errorCategory"] == "SECURITY"
    assert "assigned to a different actor" in body["detail"]
