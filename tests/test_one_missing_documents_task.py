"""One missing-documents task per journey (product owner 2026-11-15: "missing docs
should be one task not one for each"), and the per-document answers on it."""
from __future__ import annotations

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_ready_document,
    answer_missing_document,
    create_p2_journey,
    database_engine,
    missing_document_lines,
)
from sqlalchemy import text

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_task_producer import (
    raise_or_refresh,
    sync_document_missing_tasks,
)
from audit_core.uc03_p2_tasks import TaskPermissionError

NOT_REQUIRED = "Document not required to be uploaded"
MANDATORY = {"DEALER_STATEMENT", "VEHICLE_INVOICE", "insurance_cover", "GATE_PASS", "rto_challan"}


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2one")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _sync(journey) -> dict:
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return sync_document_missing_tasks(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _sql(journey, sql: str, **params):
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        return c.execute(text(sql), {"t": journey.tenant_id, "j": journey.journey_id, **params})


def _tasks(journey) -> list[dict]:
    return [dict(r) for r in _sql(journey, "SELECT dedupe_key, task_status, title FROM auditcore.p2_tasks "
                                           "WHERE tenant_id=:t AND journey_id=:j AND task_type='DOCUMENT_MISSING' "
                                           "ORDER BY created_at_utc").mappings().all()]


def _decide(journey, key: str, decision: str, reason: str | None = None) -> dict:
    from audit_core.uc03_not_mandatory_claims import claims, decide_claim

    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        claim = next(x for x in claims(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
                     if x["requirementKey"] == key)
        return decide_claim(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id, task_id=claim["taskId"],
                            decision=decision, reason=reason, actor_id="rv", actor_role_code="REVIEWER")


def _delivery_started(journey) -> None:
    add_ready_document(journey, "booking_docket", customer_name="A", booking_date="2026-09-01")
    add_ready_document(journey, "gate_pass", process_area="DELIVERY", gate_pass_number="GP-1")


def test_every_missing_document_is_a_line_of_one_task(journey):
    _delivery_started(journey)
    _sync(journey)
    lines = missing_document_lines(journey)
    # The five mandatory delivery documents; the gate pass is on file.
    assert set(lines) == MANDATORY - {"GATE_PASS"}
    assert lines["insurance_cover"]["label"] == "Insurance Cover Note or Policy"
    assert lines["VEHICLE_INVOICE"]["label"] == "Vehicle invoice (DMS or Tally)"
    assert lines["DEALER_STATEMENT"]["notRequiredAllowed"] is False
    assert len(_tasks(journey)) == 1 and _tasks(journey)[0]["title"].startswith("Upload 4 missing documents")
    _sync(journey)
    assert len(_tasks(journey)) == 1

    # Either invoice format meets the vehicle invoice; a cover note or a policy (one DI type) meets insurance.
    add_ready_document(journey, "tax_invoice_tally", process_area="DELIVERY", invoice_number="T-1")
    add_ready_document(journey, "insurance_cover", process_area="DELIVERY", policy_number="P-1")
    _sync(journey)
    assert set(missing_document_lines(journey)) == {"DEALER_STATEMENT", "rto_challan"}

    add_ready_document(journey, "cost_sheet", process_area="DELIVERY", ex_showroom_price="1000000")
    add_ready_document(journey, "rto_challan", process_area="DELIVERY", registration_number="OD02AB1234")
    _sync(journey)
    assert missing_document_lines(journey) == {}
    assert _tasks(journey)[0]["task_status"] == "VERIFIED_COMPLETE"


def test_the_per_document_tasks_of_before_close_into_the_one_task(journey):
    _delivery_started(journey)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        raise_or_refresh(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
                         dedupe_key=f"document-missing:{journey.journey_id}:rto_challan", task_type="DOCUMENT_MISSING",
                         source_type="DOCUMENT", source_code="DOCUMENT_MISSING", title="Upload the RTO Challan",
                         description="x", reference={"sourceCode": "DOCUMENT_MISSING", "requirementKey": "rto_challan"},
                         severity="HIGH", registry=get_registry())
    _sync(journey)
    status = {t["dedupe_key"]: t["task_status"] for t in _tasks(journey)}
    assert status[f"document-missing:{journey.journey_id}:rto_challan"] == "VERIFIED_COMPLETE"
    assert status[f"document-missing:{journey.journey_id}"] == "READY"


def test_answers_take_a_document_off_the_task(journey):
    _delivery_started(journey)
    _sync(journey)
    # The RTO challan: temporary registration, by a Team Lead with the number, never the PC.
    with pytest.raises(TaskPermissionError):
        answer_missing_document(journey, "rto_challan", "Temporary registration",
                                comment="Temporary registration: OD02T1234")
    result = answer_missing_document(journey, "rto_challan", "Temporary registration", role="TL",
                                     comment="Temporary registration: OD02T1234, permanent awaited")
    assert result["outcome"] == "DOCUMENT_ANSWERED"
    answer_missing_document(journey, "insurance_cover", "Document uploaded", comment="Document uploaded")
    assert set(missing_document_lines(journey)) == {"DEALER_STATEMENT", "VEHICLE_INVOICE"}
    # Another sync does not ask for them again.
    _sync(journey)
    assert set(missing_document_lines(journey)) == {"DEALER_STATEMENT", "VEHICLE_INVOICE"}
    from audit_core.uc03_document_waivers import answer

    task_id = missing_document_lines(journey)["VEHICLE_INVOICE"]["taskId"]
    with pytest.raises(ValueError, match="Choose the document"), journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        task = dict(c.execute(text("SELECT * FROM auditcore.p2_tasks WHERE task_id=:id"), {"id": task_id})
                    .mappings().one())
        answer(c, tenant_id=journey.tenant_id, task=task, details={"choice": "Document uploaded",
                                                                   "requirementKey": "rto_challan"},
               comment="Document uploaded", actor_id=journey.actor_id, actor_role_code="PC")
    with pytest.raises(ValueError, match="extended warranty or RSA"):
        answer_missing_document(journey, "VEHICLE_INVOICE", "Dealer not provided")


def test_a_not_required_claim_waits_for_the_reviewer_and_a_rejection_asks_again(journey):
    _delivery_started(journey)
    _sync(journey)
    with pytest.raises(ValueError, match="cannot be marked as not required"):
        answer_missing_document(journey, "DEALER_STATEMENT", NOT_REQUIRED, comment=f"{NOT_REQUIRED}: none given")
    answer_missing_document(journey, "VEHICLE_INVOICE", NOT_REQUIRED,
                            comment=f"{NOT_REQUIRED}: the invoice is with the financier this week")
    assert "VEHICLE_INVOICE" not in missing_document_lines(journey)   # waiting with the Reviewer
    review = _sql(journey, "SELECT task_status, assigned_role_code FROM auditcore.p2_tasks WHERE tenant_id=:t "
                           "AND dedupe_key=:k", k=f"not-mandatory-review:{journey.journey_id}").mappings().one()
    assert review["assigned_role_code"] == "REVIEWER" and review["task_status"] in ("READY", "IN_PROGRESS")

    _decide(journey, "VEHICLE_INVOICE", "REJECT", reason="Every delivery has a vehicle invoice; upload it")
    line = missing_document_lines(journey)["VEHICLE_INVOICE"]
    assert "Every delivery has a vehicle invoice; upload it" in line["reason"]

    answer_missing_document(journey, "VEHICLE_INVOICE", NOT_REQUIRED,
                            comment=f"{NOT_REQUIRED}: fleet sale, the invoice is billed to the head office")
    _decide(journey, "VEHICLE_INVOICE", "ACCEPT", reason="Fleet letter seen")
    _sync(journey)
    assert "VEHICLE_INVOICE" not in missing_document_lines(journey)


def test_dealer_not_provided_and_temporary_registration_are_system_remarks(journey):
    from audit_core.uc03_system_remarks import system_remarks

    _delivery_started(journey)
    _sync(journey)
    answer_missing_document(journey, "rto_challan", "Temporary registration", role="REVIEWER",
                            comment="Temporary registration: OD02T1234")
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        remarks = system_remarks(c, tenant_id=journey.tenant_id, journey_id=journey.journey_id)["remarks"]
    [remark] = [r for r in remarks if r["code"] == "TEMPORARY_REGISTRATION"]
    assert "OD02T1234" in remark["text"] and "Reviewer" in remark["text"]


def test_a_claim_on_a_per_document_task_of_before_closes_that_task_with_the_decision(journey):
    """The 50 claims pending on 2026-11-15 were made on per-document tasks, parked
    with the Reviewer; deciding the claim (a waiver row) closes that task too."""
    from audit_core.uc03_p2_tasks import submit_action

    _delivery_started(journey)
    with journey.engine.begin() as c:
        set_tenant_context(c, journey.tenant_id)
        old_id, _ = raise_or_refresh(
            c, tenant_id=journey.tenant_id, journey_id=journey.journey_id,
            dedupe_key=f"document-missing:{journey.journey_id}:rto_challan", task_type="DOCUMENT_MISSING",
            source_type="DOCUMENT", source_code="DOCUMENT_MISSING", title="Upload the RTO Challan", description="x",
            reference={"sourceCode": "DOCUMENT_MISSING", "requirementKey": "rto_challan", "stage": "DELIVERY"},
            severity="HIGH", registry=get_registry())
        submit_action(c, tenant_id=journey.tenant_id, task_id=old_id, action="CLOSE_TASK", actor_id=journey.actor_id,
                      actor_role_code="PC", comment=f"{NOT_REQUIRED}: customer registers the vehicle himself",
                      details={"choice": NOT_REQUIRED})
    status = lambda: _sql(journey, "SELECT task_status FROM auditcore.p2_tasks WHERE task_id=:id",
                          id=old_id).scalar_one()
    assert status() == "AWAITING_REQUESTER_REVIEW"
    _decide(journey, "rto_challan", "REJECT", reason="Dealer registers every vehicle; upload the challan")
    assert status() == "VERIFIED_COMPLETE"
    _sync(journey)
    assert "Dealer registers every vehicle" in missing_document_lines(journey)["rto_challan"]["reason"]
