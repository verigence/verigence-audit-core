"""Removing a refused or failed page from the Journey (2026-10-21).

A re-scan of a page refused for image quality is different bytes, so it
never replaces the refused page; the page cannot be retried or read again.
Without a way to take it off the Journey its tasks -- and the delivery gate
-- stayed open for good. POST /pages/{queue}:remove ends that."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi.testclient import TestClient
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
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
from audit_core.uc03_p2_task_producer import sync_processing_failure_tasks


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2rm")
    app.dependency_overrides[get_human_principal] = lambda: HumanPrincipal(subject=created.actor_id)
    app.dependency_overrides[get_security_authorization_client] = lambda: AllowAllAuthorization()
    try:
        yield created
    finally:
        app.dependency_overrides.clear()
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, **params})


def _fail(journey, queue_id, code: str) -> None:
    _sql(journey, "UPDATE auditcore.p2_document_queue SET queue_status='FAILED', last_error=:e WHERE queue_id=:q",
         e=code, q=queue_id)


def _sync(journey) -> None:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        sync_processing_failure_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _task(journey, key: str) -> dict | None:
    row = _sql(journey, "SELECT task_status, reference, completion_result FROM auditcore.p2_tasks "
                        "WHERE tenant_id=:t AND dedupe_key=:k", k=key).mappings().one_or_none()
    return dict(row) if row else None


def _gate(journey) -> list[str]:
    from audit_core.uc03_p2_stage import _open_tasks_for

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [t["taskType"] for t in _open_tasks_for(connection, tenant_id=journey.tenant_id,
                                                        journey_id=journey.journey_id, role="PC",
                                                        tabs=("DOCUMENTS", "MANUAL_VERIFICATION"))]


def _remove(journey, queue_id, reason="Re-scan uploaded as scan2.pdf"):
    return TestClient(app, raise_server_exceptions=False).post(
        f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{queue_id}:remove",
        json={"reason": reason},
    )


def test_removing_the_refused_page_closes_its_task_and_frees_the_gate(journey):
    batch, pages = add_batch_pages(journey, [("booking_form", "READY"), (None, "QUEUED")])
    _fail(journey, pages[1]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE")
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='PARTIAL_FAILURE' WHERE batch_id=:b", b=batch)
    _sync(journey)
    key = f"upload-quality:{journey.journey_id}:{batch}"
    assert _task(journey, key)["task_status"] == "READY" and _gate(journey) == ["PC_PAGE_QUALITY_REJECTED"]
    # The task carries the page's unit so its row can offer "Remove this page".
    assert _task(journey, key)["reference"]["pageQueues"] == [{"page": 2, "queueId": str(pages[1]["queue_id"])}]

    response = _remove(journey, pages[1]["queue_id"])
    assert response.status_code == 200, response.text
    assert response.json() == {"queueId": str(pages[1]["queue_id"]), "status": "CANCELLED", "documentVoided": False}

    task = _task(journey, key)
    assert task["task_status"] == "VERIFIED_COMPLETE"
    assert task["completion_result"]["reason"] == (
        f"Page 2 of packet.pdf was removed from the journey by {journey.actor_id} (PC): Re-scan uploaded as scan2.pdf")
    assert task["completion_result"]["removedPage"]["queueId"] == str(pages[1]["queue_id"])
    assert _gate(journey) == []

    page = _sql(journey, "SELECT queue_status, status_reason, last_error FROM auditcore.p2_document_queue "
                         "WHERE queue_id=:q", q=pages[1]["queue_id"]).mappings().one()
    assert page["queue_status"] == "CANCELLED" and page["last_error"] == "DI_QUALITY_IMAGE_BLUR_SCORE"
    assert page["status_reason"].endswith(": Re-scan uploaded as scan2.pdf")
    batch_row = _sql(journey, "SELECT batch_status FROM auditcore.p2_upload_batches WHERE batch_id=:b", b=batch)
    assert batch_row.scalar_one() == "COMPLETED"
    event = _sql(journey, "SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                          "AND event_type='PAGE_REMOVED'").scalar_one()
    assert event["removedBy"] == journey.actor_id and event["reason"] == "Re-scan uploaded as scan2.pdf"
    assert event["pages"] == [2] and event["lastError"] == "DI_QUALITY_IMAGE_BLUR_SCORE"


def test_a_document_held_for_the_page_is_voided_so_nothing_is_read_twice(journey):
    batch, queue_id, di_document_id = add_page(journey, status="FAILED")
    _fail(journey, queue_id, "DI_QUALITY_IMAGE_CONTRAST")
    _sync(journey)
    response = _remove(journey, queue_id)
    assert response.status_code == 200, response.text
    assert response.json()["documentVoided"] is True
    capture = _sql(journey, "SELECT capture_status FROM auditcore.document_capture_v2_documents "
                            "WHERE tenant_id=:t AND di_document_id=:d", d=di_document_id).scalar_one()
    assert capture == "SUPERSEDED"
    voided = _sql(journey, "SELECT details FROM auditcore.p2_activity_events WHERE tenant_id=:t "
                           "AND event_type='DOCUMENT_VOIDED'").scalar_one()
    assert voided["via"] == "PAGE_REMOVED" and voided["queueId"] == str(queue_id)
    assert _task(journey, f"upload-quality:{journey.journey_id}:{batch}")["task_status"] == "VERIFIED_COMPLETE"


def test_removing_one_refused_page_keeps_the_task_for_the_others(journey):
    batch, pages = add_batch_pages(journey, [(None, "QUEUED"), (None, "QUEUED")])
    _fail(journey, pages[0]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE")
    _fail(journey, pages[1]["queue_id"], "DI_QUALITY_IMAGE_MIN_DIMENSIONS")
    _sync(journey)
    assert _remove(journey, pages[0]["queue_id"]).status_code == 200
    task = _task(journey, f"upload-quality:{journey.journey_id}:{batch}")
    assert task["task_status"] == "READY" and task["reference"]["pageNumbers"] == [2]


def test_a_page_that_failed_otherwise_can_be_removed_too(journey):
    batch, pages = add_batch_pages(journey, [("booking_form", "READY"), (None, "QUEUED")])
    _fail(journey, pages[1]["queue_id"], "DOCUMENT_AI_RESPONSE_INVALID")
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='PARTIAL_FAILURE' WHERE batch_id=:b", b=batch)
    _sync(journey)
    key = f"upload-status:{journey.journey_id}:{batch}"
    assert _task(journey, key)["task_status"] == "READY"
    assert _remove(journey, pages[1]["queue_id"], reason="Uploaded again on its own").status_code == 200
    task = _task(journey, key)
    assert task["task_status"] == "VERIFIED_COMPLETE"
    assert task["completion_result"]["reason"].endswith("removed from the journey by "
                                                        f"{journey.actor_id} (PC): Uploaded again on its own")


def test_only_failed_pages_can_be_removed_and_a_reason_is_needed(journey):
    _, pages = add_batch_pages(journey, [("booking_form", "READY"), (None, "QUEUED")])
    refused = _remove(journey, pages[0]["queue_id"])
    assert refused.status_code == 409 and "refused or could not be processed" in refused.json()["detail"]
    _fail(journey, pages[1]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE")
    assert _remove(journey, pages[1]["queue_id"], reason="").status_code == 400
    assert _remove(journey, pages[1]["queue_id"], reason="   ").status_code == 400
    assert _remove(journey, uuid4()).status_code == 404


def test_another_journeys_page_is_not_found(journey):
    other = create_p2_journey(journey.engine, prefix="p2rmx")
    try:
        _, queue_id, _ = add_page(other, status="FAILED")
        response = TestClient(app, raise_server_exceptions=False).post(
            f"/p2/v1/tenants/{journey.tenant_id}/journeys/{journey.journey_id}/pages/{queue_id}:remove",
            json={"reason": "not mine"},
        )
        assert response.status_code == 404
    finally:
        delete_tenant_data(journey.engine, other.tenant_id)


def test_a_status_task_whose_pages_moved_to_the_quality_task_says_so(journey):
    """Review of #479 item 3: the old status task for a file whose only failed
    page was refused for quality closes saying where the page went, not
    "processed or removed"."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    batch, pages = add_batch_pages(journey, [("booking_form", "READY"), (None, "QUEUED")])
    _fail(journey, pages[1]["queue_id"], "DI_QUALITY_IMAGE_BLUR_SCORE")
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='PARTIAL_FAILURE' WHERE batch_id=:b", b=batch)
    key = f"upload-status:{journey.journey_id}:{batch}"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        raise_or_refresh(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, dedupe_key=key,
                         task_type="PC_UPLOAD_STATUS", source_type="DOCUMENT", source_code="DOCUMENT_PROCESSING_FAILED",
                         title="packet.pdf: 1 page rejected at upload", description="old",
                         reference={"batchId": str(batch)}, severity="HIGH", registry=get_registry())
    _sync(journey)
    assert _task(journey, key)["completion_result"]["reason"] == (
        "The pages still not read were refused for image quality; they are on the file's image-quality task.")
