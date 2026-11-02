"""A booking with no booking form, and nothing asking for one, gets a PC task (product owner 2026-11-02).

27 live bookings (15 at Keonjhar) had no booking form read and no task: the
form is a required document, shown only on the stage checklist; its page was
refused for scan quality and that task closed by hand with "uploaded
document", while the new photo was refused too, or nothing was uploaded."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import create_p2_journey, database_engine
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_task_producer import sync_booking_form_missing_task

LATER = datetime.now(UTC) + timedelta(hours=2)


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2bfm")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        result = connection.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})
        return result.mappings().all() if result.returns_rows else None


def _sync(journey, now=LATER) -> str:
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return sync_booking_form_missing_task(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                                              now=now)


def _task(journey):
    rows = _sql(journey, "SELECT task_type, source_code, task_status, title, severity, assigned_role_code "
                         "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k",
                k=f"booking-form-missing:{journey.journey_id}")
    return rows[0] if rows else None


def _requirement(journey) -> str:
    return _sql(journey, "INSERT INTO auditcore.journey_document_requirements (tenant_id, journey_id, requirement_key, "
                         "document_type_key, process_area, requirement_level) VALUES (:t, :j, 'booking_docket', "
                         "'booking_docket', 'BOOKING', 'REQUIRED') RETURNING journey_document_requirement_id")[0][
        "journey_document_requirement_id"]


def _booking_form(journey, requirement) -> None:
    _sql(journey, "INSERT INTO auditcore.evidence (tenant_id, journey_id, customer_id, journey_document_requirement_id, "
                  "di_subject_id, di_document_id, document_type_key, evidence_purpose, process_area, "
                  "association_status, linked_at_utc) VALUES (:t, :j, :c, :r, :s, :d, 'booking_docket', "
                  "'BOOKING_DOCUMENT', 'BOOKING', 'ACTIVE', now())",
         c=journey.customer_id, r=requirement, s=uuid4(), d=uuid4())


def _other_ask(journey, task_type: str = "PC_PAGE_QUALITY_REJECTED", status: str = "READY") -> None:
    _sql(journey, "INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, source_type, "
                  "source_code, dedupe_key, title, description, reference, severity, priority, assigned_role_code, "
                  "allowed_actions, completion_protocol, task_status, completion_result) VALUES (:t, :j, :tt, "
                  "'DOCUMENT_REUPLOAD', 'SYSTEM', 'DOCUMENT', 'X', :k, 'x', 'x', '{}'::jsonb, 'HIGH', 'HIGH', 'PC', "
                  "'[]'::jsonb, 'MACHINE_VERIFIED', :s, '{}'::jsonb)",
         tt=task_type, k=f"other:{uuid4()}", s=status)


def test_a_booking_with_no_booking_form_and_nothing_asking_gets_one_pc_task_that_closes_when_read(journey):
    requirement = _requirement(journey)
    assert _sync(journey, now=datetime.now(UTC)) == "UNCHANGED"  # an hour's grace after the booking is made
    assert _task(journey) is None
    assert _sync(journey) == "RAISED"
    task = _task(journey)
    assert (task["task_type"], task["source_code"], task["task_status"], task["assigned_role_code"]) == (
        "DOCUMENT_MISSING", "BOOKING_FORM_MISSING", "READY", "PC")
    assert task["title"] == "Upload the Booking Docket" and task["severity"] == "HIGH"
    assert _sync(journey) in ("UNCHANGED", "UPDATED")  # one task, never a second
    _booking_form(journey, requirement)
    assert _sync(journey) == "VERIFIED"
    assert _task(journey)["task_status"] == "VERIFIED_COMPLETE"


def test_an_open_ask_for_a_file_or_a_file_being_read_raises_nothing(journey):
    _requirement(journey)
    _other_ask(journey)
    assert _sync(journey) == "UNCHANGED" and _task(journey) is None
    # The Keonjhar case: the refused page's task closed by hand -- now nothing asks.
    _sql(journey, "UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE' WHERE tenant_id=:t "
                  "AND dedupe_key LIKE 'other:%'")
    _sql(journey, "INSERT INTO auditcore.p2_upload_batches (tenant_id, batch_id, journey_id, original_filename, "
                  "content_type, size_bytes, page_count, original_object_key, batch_status, uploaded_by_actor_id) "
                  "VALUES (:t, :b, :j, 'f.pdf', 'application/pdf', 1, 1, 'k', 'PROCESSING', :a)",
         b=uuid4(), a=journey.actor_id)
    assert _sync(journey) == "UNCHANGED" and _task(journey) is None  # still being read
    _sql(journey, "UPDATE auditcore.p2_upload_batches SET batch_status='PARTIAL_FAILURE' WHERE tenant_id=:t")
    assert _sync(journey) == "RAISED"


def test_a_cancelled_or_duplicate_booking_is_never_asked_and_its_open_task_closes(journey):
    _requirement(journey)
    assert _sync(journey) == "RAISED"
    _sql(journey, "INSERT INTO auditcore.journey_stage_states (tenant_id, journey_id, stage_code, business_status) "
                  "VALUES (:t, :j, 'BOOKING', 'BOOKING_CANCELLED') "
                  "ON CONFLICT (tenant_id, journey_id, stage_code) DO UPDATE SET business_status='BOOKING_CANCELLED'")
    assert _sync(journey) == "VERIFIED"
    assert _task(journey)["task_status"] == "VERIFIED_COMPLETE"
