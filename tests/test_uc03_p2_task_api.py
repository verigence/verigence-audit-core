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
