"""The journey list says how long the latest upload took to read, or that it
is still being processed (product owner 2026-10-20)."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_page,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_human_principal
from audit_core.main import app
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import get_security_authorization_client
from audit_core.uc03_p2_runtime import record_activity


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2up")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _item(journey):
    client = TestClient(app, raise_server_exceptions=False)
    response = client.get(f"/p2/v1/tenants/{journey.tenant_id}/journeys", params={"state": "open"})
    assert response.status_code == 200, response.text
    return next(i for i in response.json()["items"] if i["journey_id"] == str(journey.journey_id))


def test_processing_then_the_time_it_took(journey):
    batch, page, _ = add_page(journey, status="EXTRACTING")
    item = _item(journey)
    assert item["processing_now"] is True
    assert item["upload_started_at"] is not None

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        c.execute(text("UPDATE auditcore.p2_upload_batches SET created_at_utc=now() - interval '20 minutes' "
                       "WHERE batch_id=:b"), {"b": batch})
        c.execute(text("UPDATE auditcore.p2_document_queue SET queue_status='READY' WHERE queue_id=:q"), {"q": page})
        record_activity(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                        event_type="DOCUMENT_STATUS_CHANGED", subject_type="DOCUMENT_PAGE", subject_id=str(page),
                        details={"status": "READY", "batchId": str(batch)})
    item = _item(journey)
    assert item["processing_now"] is False
    assert item["upload_finished_at"] is not None
    assert item["upload_finished_at"] > item["upload_started_at"]
