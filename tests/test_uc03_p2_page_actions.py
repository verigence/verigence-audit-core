"""PC page actions: retry a failed page, re-type a page."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    AllowAllAuthorization,
    add_batch_pages,
    create_p2_journey,
    database_engine,
    principal,
    queue_row,
)
from sqlalchemy import text
from starlette.requests import Request

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_api import SetPageTypeCommand, retry_page, set_page_type


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2pg")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def _unit(journey, queue_id):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return dict(connection.execute(
            text("SELECT * FROM auditcore.p2_document_queue WHERE queue_id=:q"), {"q": queue_id},
        ).mappings().one())


def test_failed_page_is_resubmitted_as_a_fresh_document(journey):
    _, [page] = add_batch_pages(journey, [("pan_card", "FAILED")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as connection:
        result = retry_page(tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=page["queue_id"],
                            request=_request(), human_principal=principal(journey),
                            authorization_client=AllowAllAuthorization(), connection=connection)
    assert result["status"] == "QUEUED"
    unit = _unit(journey, page["queue_id"])
    assert unit["di_document_id"] is None and unit["client_upload_id"].endswith("~r1")
    assert queue_row(journey, "DOCUMENT_INGEST", str(page["queue_id"]))["work_status"] == "PENDING"


def test_only_failed_pages_can_be_retried(journey):
    _, [page] = add_batch_pages(journey, [("pan_card", "EXTRACTING")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as connection, pytest.raises(HTTPException):
        retry_page(tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=page["queue_id"],
                   request=_request(), human_principal=principal(journey),
                   authorization_client=AllowAllAuthorization(), connection=connection)


def test_supporting_page_can_be_retyped_and_the_old_result_is_retired(journey):
    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as connection:
        result = set_page_type(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               queue_id=page["queue_id"], command=SetPageTypeCommand(templateKey="customer_kyc"),
                               request=_request(), human_principal=principal(journey),
                               authorization_client=AllowAllAuthorization(), connection=connection)
    new_unit = _unit(journey, result["queueId"])
    assert new_unit["candidate_override"] == ["customer_kyc"]
    assert new_unit["group_source"] == "PC" and new_unit["type_overridden_by_actor_id"] == journey.actor_id
    old = _unit(journey, page["queue_id"])
    assert old["queue_status"] == "MERGED" and str(old["merged_into_queue_id"]) == result["queueId"]
    assert queue_row(journey, "DOCUMENT_INGEST", result["queueId"]) is not None


def test_retyping_rejects_unknown_types(journey):
    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as connection, pytest.raises(HTTPException):
        set_page_type(tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=page["queue_id"],
                      command=SetPageTypeCommand(templateKey="no_such_template"), request=_request(),
                      human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
                      connection=connection)


def _open_unclassified_tasks(journey) -> list[dict]:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT dedupe_key, task_type, severity, title, task_status, reference FROM auditcore.p2_tasks "
                 "WHERE tenant_id=:t AND dedupe_key LIKE 'unclassified:%' ORDER BY created_at_utc"),
            {"t": journey.tenant_id},
        ).mappings().all()]


def test_unclassified_page_raises_a_high_task_that_closes_when_the_type_is_set(journey):
    """Issue 9 (2026-09-30): a page DI could not classify is a High task for
    the PC, pointing at the page; setting its type closes the task."""
    from audit_core.uc03_p2_task_producer import sync_unclassified_page_tasks

    _, [page, classified] = add_batch_pages(
        journey, [(None, "SUPPORTING"), ("pan_card", "READY")], grouping_status="NOT_NEEDED",
    )
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        counts = sync_unclassified_page_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert counts == {"RAISED": 1}
    [task] = _open_unclassified_tasks(journey)
    assert task["task_type"] == "PC_VERIFY_UNRECOGNIZED_DOCUMENT" and task["severity"] == "HIGH"
    assert task["task_status"] == "READY"
    assert task["title"] == "Set the document type: page 1 of packet.pdf"
    assert task["reference"]["queueId"] == str(page["queue_id"]) and task["reference"]["pageNumbers"] == [1]
    assert str(classified["queue_id"]) not in task["dedupe_key"]

    with journey.engine.begin() as connection:
        set_page_type(tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=page["queue_id"],
                      command=SetPageTypeCommand(templateKey="customer_kyc"), request=_request(),
                      human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
                      connection=connection)
    [task] = _open_unclassified_tasks(journey)
    assert task["task_status"] == "VERIFIED_COMPLETE"


def test_unclassified_page_can_be_kept_as_others_without_being_read(journey):
    """Issue 5 (2026-09-30): "Others" keeps the page on file, never sends it
    for reading, and closes the unclassified task."""
    from audit_core.uc03_p2_task_producer import sync_unclassified_page_tasks

    _, [page] = add_batch_pages(journey, [(None, "SUPPORTING")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        sync_unclassified_page_tasks(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    with journey.engine.begin() as connection:
        result = set_page_type(tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               queue_id=page["queue_id"], command=SetPageTypeCommand(templateKey="supporting_document"),
                               request=_request(), human_principal=principal(journey),
                               authorization_client=AllowAllAuthorization(), connection=connection)
    assert result == {"queueId": str(page["queue_id"]), "status": "SUPPORTING", "templateKey": "supporting_document"}
    unit = _unit(journey, page["queue_id"])
    assert unit["queue_status"] == "SUPPORTING" and unit["template_key"] == "supporting_document"
    assert unit["type_overridden_by_actor_id"] == journey.actor_id
    assert queue_row(journey, "DOCUMENT_INGEST", str(page["queue_id"])) is None
    [task] = _open_unclassified_tasks(journey)
    assert task["task_status"] == "VERIFIED_COMPLETE"


def test_others_is_only_for_a_page_that_could_not_be_classified(journey):
    _, [page] = add_batch_pages(journey, [("pan_card", "READY")], grouping_status="NOT_NEEDED")
    with journey.engine.begin() as connection, pytest.raises(HTTPException) as raised:
        set_page_type(tenant_id=journey.tenant_id, journey_id=journey.journey_id, queue_id=page["queue_id"],
                      command=SetPageTypeCommand(templateKey="supporting_document"), request=_request(),
                      human_principal=principal(journey), authorization_client=AllowAllAuthorization(),
                      connection=connection)
    assert raised.value.status_code == 409


def test_resync_leaves_phase2_pages_to_the_worker(journey):
    """Recheck (2026-09-30): a Phase 2 page's values are copied by the
    worker; the Phase 1 resync must not dispatch its background copy for it."""
    from uuid import uuid4

    from audit_core.uc03_unified_document_capture import _phase2_page_ids

    _, [page] = add_batch_pages(journey, [("pan_card", "READY")], grouping_status="NOT_NEEDED")
    phase1_document = uuid4()
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        found = _phase2_page_ids(connection, tenant_id=journey.tenant_id,
                                 document_ids=[page["di_document_id"], phase1_document])
        assert _phase2_page_ids(connection, tenant_id=journey.tenant_id, document_ids=[]) == set()
    assert found == {str(page["di_document_id"])}
