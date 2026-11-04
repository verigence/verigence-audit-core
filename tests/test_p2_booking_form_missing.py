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


# ── The PC closes a missing-document task by hand (product owner 2026-11-02) ──
# A single page uploaded for it is often refused for scan quality and never
# read, so the machine check never closes it. The PC closes it as the
# image-quality task: "Document uploaded" or "Document not required to be
# uploaded", with the comment; the producer does not bring it back.

def _close(journey, task_id, comment, choice=None, role="PC"):
    from audit_core.uc03_p2_tasks import submit_action

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action="CLOSE_TASK",
                             actor_id=journey.actor_id, actor_role_code=role, comment=comment,
                             details={"choice": choice} if choice else {})


def _row(journey, key):
    return _sql(journey, "SELECT task_id, task_status, allowed_actions, completion_result, round_number "
                         "FROM auditcore.p2_tasks WHERE tenant_id=:t AND dedupe_key=:k", k=key)[0]


def _raise_missing(journey, key, source_code="DOCUMENT_MISSING", title="Upload the RSA Invoice"):
    """What sync_document_missing_tasks (and, for the docket, sync_booking_form_missing_task) raise."""
    from audit_core.uc03_p2_registry import get_registry
    from audit_core.uc03_p2_task_producer import raise_or_refresh

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return raise_or_refresh(
            connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id, dedupe_key=key,
            task_type="DOCUMENT_MISSING", source_type="DOCUMENT", source_code=source_code, title=title,
            description="The booking docket says RSA was sold. Upload it; this task closes itself once it is read.",
            reference={"generatedBy": "SYSTEM", "sourceType": "DOCUMENT", "sourceCode": source_code,
                       "requirementKey": "rsa_invoice", "templateKeys": ["rsa_invoice"],
                       "conditions": ["RSA_SOLD"], "stage": "DELIVERY"},
            severity="HIGH", registry=get_registry(),
        )[1]


def test_the_pc_closes_a_missing_document_task_as_uploaded_and_the_producer_does_not_reopen_it(journey):
    key = f"document-missing:{journey.journey_id}:rsa_invoice"
    assert _raise_missing(journey, key) == "RAISED"
    row = _row(journey, key)
    # Rows raised before (and after) this change do not list CLOSE_TASK: it is accepted all the same.
    assert "CLOSE_TASK" not in row["allowed_actions"]
    with pytest.raises(ValueError, match="comment"):
        _close(journey, row["task_id"], "  ", choice="Document uploaded")
    assert _row(journey, key)["task_status"] == "READY"
    assert _close(journey, row["task_id"], "Document uploaded", choice="Document uploaded") == {
        "taskId": str(row["task_id"]), "status": "VERIFIED_COMPLETE", "outcome": "CLOSED_BY_PC"}
    closed = _row(journey, key)
    result = closed["completion_result"]
    assert closed["task_status"] == "VERIFIED_COMPLETE"
    assert (result["outcome"], result["reason"], result["choice"], result["closedRole"]) == (
        "CLOSED_BY_PC", "Document uploaded", "Document uploaded", "PC")
    [event] = _sql(journey, "SELECT event_type, comment FROM auditcore.p2_task_events WHERE tenant_id=:t "
                            "AND task_id=:id AND event_type='CLOSE_TASK'", id=row["task_id"])
    assert event["comment"] == "Document uploaded"
    # The document is still not read: the next producer run finds it missing again and leaves the task closed.
    assert _raise_missing(journey, key) == "ACCEPTED_EXCEPTION"
    after = _row(journey, key)
    assert (after["task_status"], after["round_number"]) == ("VERIFIED_COMPLETE", 1)
    assert _sql(journey, "SELECT count(*) AS n FROM auditcore.p2_task_events WHERE tenant_id=:t AND task_id=:id "
                         "AND event_type='REOPENED'", id=row["task_id"])[0]["n"] == 0


def _decide(journey, task_id, decision, reason=None, role="REVIEWER"):
    from audit_core.uc03_not_mandatory_claims import decide_claim

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return decide_claim(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                            task_id=task_id, decision=decision, reason=reason, actor_id="a-reviewer",
                            actor_role_code=role)


def _review_task(journey):
    rows = _sql(journey, "SELECT task_id, task_status, assigned_role_code, reference FROM auditcore.p2_tasks "
                         "WHERE tenant_id=:t AND dedupe_key=:k", k=f"not-mandatory-review:{journey.journey_id}")
    return rows[0] if rows else None


def test_the_pc_closes_the_booking_docket_task_as_not_required_and_the_sync_leaves_it_closed(journey):
    # From 2026-11-04 "not required" is the PC's claim, accepted by the Reviewer.
    _requirement(journey)
    assert _sync(journey) == "RAISED"
    key = f"booking-form-missing:{journey.journey_id}"
    choice = "Document not required to be uploaded"
    task_id = _row(journey, key)["task_id"]
    _close(journey, task_id, f"{choice}: booking form is with the TL", choice=choice)
    claimed = _row(journey, key)
    assert claimed["task_status"] == "AWAITING_REQUESTER_REVIEW"
    assert (claimed["completion_result"]["outcome"], claimed["completion_result"]["claimReason"]) == (
        "NOT_MANDATORY_CLAIMED", "booking form is with the TL")
    _decide(journey, task_id, "ACCEPT")
    result = _row(journey, key)["completion_result"]
    assert (result["outcome"], result["choice"]) == ("NOT_MANDATORY_ACCEPTED", choice)
    assert _sync(journey) == "ACCEPTED_EXCEPTION"  # still no booking form read: not raised again
    assert _row(journey, key)["task_status"] == "VERIFIED_COMPLETE"


def test_not_mandatory_claims_go_to_the_reviewer_combined_and_a_rejection_returns_to_the_pc(journey):
    """Product owner 2026-11-04: the PC closes a mandatory-document task as not
    mandatory with a reason; all such claims go to the Reviewer combined; a
    rejected one is back with the PC."""
    from fastapi import HTTPException

    choice = "Document not required to be uploaded"
    rsa = f"document-missing:{journey.journey_id}:rsa_invoice"
    ew = f"document-missing:{journey.journey_id}:ew_invoice"
    _raise_missing(journey, rsa)
    _raise_missing(journey, ew, title="Upload the EW Invoice")
    rsa_id, ew_id = _row(journey, rsa)["task_id"], _row(journey, ew)["task_id"]

    with pytest.raises(ValueError, match="not mandatory"):
        _close(journey, rsa_id, choice, choice=choice)          # the choice alone is no reason
    assert _row(journey, rsa)["task_status"] == "READY"

    _close(journey, rsa_id, f"{choice}: RSA is bundled free by the OEM this month", choice=choice)
    _close(journey, ew_id, f"{choice}: customer declined extended warranty", choice=choice)
    review = _review_task(journey)
    assert review["assigned_role_code"] == "REVIEWER" and review["task_status"] in ("READY", "IN_PROGRESS")
    assert sorted(c["document"] for c in review["reference"]["claims"]) == ["Upload the EW Invoice",
                                                                             "Upload the RSA Invoice"]

    with pytest.raises(HTTPException) as no_reason:
        _decide(journey, ew_id, "REJECT", reason="no")
    assert no_reason.value.status_code == 422
    with pytest.raises(HTTPException) as not_pc:
        _decide(journey, ew_id, "ACCEPT", role="PC")
    assert not_pc.value.status_code == 403

    _decide(journey, rsa_id, "ACCEPT", reason="OEM scheme letter seen")
    _decide(journey, ew_id, "REJECT", reason="EW is on the invoice; upload the EW invoice")
    assert _row(journey, rsa)["task_status"] == "VERIFIED_COMPLETE"
    returned = _row(journey, ew)
    assert (returned["task_status"], returned["round_number"]) == ("RETURNED", 2)
    assert returned["completion_result"]["decisionReason"] == "EW is on the invoice; upload the EW invoice"
    assert _review_task(journey)["task_status"] == "VERIFIED_COMPLETE"   # nothing pending: closed

    # The PC answers the returned task again; the Reviewer's task opens again with it.
    _close(journey, ew_id, f"{choice}: EW line on the invoice was reversed by credit note", choice=choice)
    review = _review_task(journey)
    assert review["task_status"] not in ("VERIFIED_COMPLETE",)
    assert [c["document"] for c in review["reference"]["claims"]] == ["Upload the EW Invoice"]
    # The producer's next run leaves the accepted claim closed.
    assert _raise_missing(journey, rsa) == "ACCEPTED_EXCEPTION"


def test_a_wrong_name_task_still_cannot_be_closed_by_hand(journey):
    key = f"customer-name:{journey.journey_id}:invoice"
    assert _raise_missing(journey, key, source_code="WRONG_CUSTOMER_NAME",
                          title="Re-upload the invoice with the customer's name") == "RAISED"
    with pytest.raises(ValueError, match="not allowed"):
        _close(journey, _row(journey, key)["task_id"], "Document uploaded", choice="Document uploaded")
    assert _row(journey, key)["task_status"] == "READY"
