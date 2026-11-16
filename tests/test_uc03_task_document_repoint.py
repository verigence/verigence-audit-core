"""Tasks follow the copy of a document in use, and a link to a replaced copy
opens the copy in use (UTK-SA-261103-044, 2026-11-16)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from fastapi import HTTPException
from p2_support import (
    add_extracted_field,
    add_page,
    add_ready_document,
    create_p2_journey,
    database_engine,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.errors import DocumentReplacedError
from audit_core.uc03_p2_documents import _document_context, current_copy_of
from audit_core.uc03_p2_task_producer import document_pointers, sync_booking_date_task
from audit_core.uc03_task_document_repoint import repoint_tenant, stale_tasks


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2rpt")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _booking_date_task(journey):
    return _sql(journey, "SELECT task_status, round_number, reference FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND dedupe_key=:k",
                k=f"booking-date:{journey.journey_id}").mappings().one()


def _sync(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_booking_date_task(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _replace(journey, old, new_type="booking_form"):
    """The page copy is read again with its neighbours: its evidence is
    superseded and the grouped copy takes its place."""
    _sql(journey, "UPDATE auditcore.evidence SET association_status='SUPERSEDED' "
                  "WHERE tenant_id=:t AND journey_id=:j AND di_document_id=:d", d=old)
    new = add_ready_document(journey, new_type, customer_name="A")
    add_extracted_field(journey, di_document_id=new, field_key="booking_date", value=None)
    return new


def test_document_pointers_ignore_order_and_unrelated_keys():
    assert document_pointers({"documentIds": ["b", "a"], "fieldKey": "x"}) == {"documentIds": ["a", "b"]}
    assert document_pointers(None) == {}


def test_the_booking_date_task_follows_the_copy_in_use_without_reopening(journey):
    page_copy = add_ready_document(journey, "booking_form", customer_name="A")
    add_extracted_field(journey, di_document_id=page_copy, field_key="booking_date", value=None)
    assert _sync(journey) == {"RAISED": 1}
    assert _booking_date_task(journey)["reference"]["documentId"] == str(page_copy)

    grouped = _replace(journey, page_copy)
    assert _sync(journey) == {"UPDATED": 1}
    task = _booking_date_task(journey)
    assert task["reference"]["documentId"] == str(grouped)
    assert task["reference"]["documentIds"] == [str(grouped)]
    assert (task["task_status"], task["round_number"]) == ("READY", 1)  # same issue: not reopened
    assert _sync(journey) == {"UNCHANGED": 1}  # and no churn once it points right


def test_the_repoint_job_moves_open_tasks_off_replaced_copies(journey):
    page_copy = add_ready_document(journey, "booking_form", customer_name="A")
    add_extracted_field(journey, di_document_id=page_copy, field_key="booking_date", value=None)
    _sync(journey)
    # Replaced before this fix: the task stayed on the page copy.
    grouped = _replace(journey, page_copy)

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert [str(r["journey_id"]) for r in stale_tasks(connection, journey.tenant_id)] == [
            str(journey.journey_id)]
        dry = repoint_tenant(connection, tenant_id=journey.tenant_id, commit=False)
    assert dry["staleTasks"] == 1 and dry["dryRunStillStale"] == 0
    assert _booking_date_task(journey)["reference"]["documentId"] == str(page_copy)  # dry run wrote nothing

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        done = repoint_tenant(connection, tenant_id=journey.tenant_id, commit=True)
    assert done["staleTasks"] == 1 and done["stillStale"] == 0
    assert _booking_date_task(journey)["reference"]["documentId"] == str(grouped)


def test_a_link_to_a_merged_page_copy_leads_to_the_grouped_copy(journey):
    _, page_queue, page_copy = add_page(journey, page_number=1, status="MERGED")
    _, grouped_queue, grouped = add_page(journey, page_number=1, status="READY")
    _sql(journey, "UPDATE auditcore.p2_document_queue SET merged_into_queue_id=:g WHERE queue_id=:q",
         g=grouped_queue, q=page_queue)
    _sql(journey, "UPDATE auditcore.document_capture_v2_documents SET capture_status='SUPERSEDED' "
                  "WHERE tenant_id=:t AND di_document_id=:d", d=page_copy)
    _sql(journey, "UPDATE auditcore.document_capture_v2_documents SET capture_status='CLASSIFIED' "
                  "WHERE tenant_id=:t AND di_document_id=:d", d=grouped)

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert current_copy_of(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               document_id=page_copy) == str(grouped)
        with pytest.raises(DocumentReplacedError) as raised:
            _document_context(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                              document_id=page_copy)
    assert raised.value.extensions == {"currentDocumentId": str(grouped)}
    assert raised.value.error_code == "VAC-NF-034" and raised.value.status_code == 404


def test_a_replaced_copy_with_several_in_use_guesses_nothing(journey):
    old = add_ready_document(journey, "aadhaar", aadhaar_name="A")
    _sql(journey, "UPDATE auditcore.evidence SET association_status='SUPERSEDED' "
                  "WHERE tenant_id=:t AND di_document_id=:d", d=old)
    add_ready_document(journey, "aadhaar", aadhaar_name="A")
    add_ready_document(journey, "aadhaar", aadhaar_name="A")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert current_copy_of(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               document_id=old) is None
        with pytest.raises(HTTPException) as raised:
            _document_context(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                              document_id=uuid4())
    assert raised.value.status_code == 404


def test_a_replaced_single_copy_leads_to_the_one_in_use(journey):
    old = add_ready_document(journey, "booking_form", customer_name="A")
    new = _replace(journey, old)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert current_copy_of(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                               document_id=old) == str(new)
