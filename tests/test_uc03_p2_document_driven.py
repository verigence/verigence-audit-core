"""Phase 2 is document driven: evidence in the Booking Form or invoices makes
conditional documents mandatory (with a Document Missing task for the PC),
and Booking / Delivery complete from the rules, never from a button.
Delivery then waits for the Team Lead's review (requirements document,
28 Sep 2026)."""
from __future__ import annotations

from uuid import uuid4

import pytest
from conftest import delete_tenant_data
from p2_support import (
    add_evidence,
    add_extracted_field,
    add_ready_document,
    add_receipt_payment,
    create_p2_journey,
    database_engine,
    set_minimum_booking_amount,
)
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import condition_reasons
from audit_core.uc03_p2_tasks import submit_action


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2dd")
    set_minimum_booking_amount(created, "21000")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _settle(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return worker.settle_journey(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _delivery_document(journey, document_type_key, **fields):
    document_id = uuid4()
    add_evidence(journey, di_document_id=document_id, document_type_key=document_type_key, process_area="DELIVERY")
    for key, value in (fields or {"marker": "x"}).items():
        add_extracted_field(journey, di_document_id=document_id, field_key=key, value=value,
                            document_type=document_type_key, confidence=99.0)
    return document_id


def _complete_booking(journey):
    add_ready_document(journey, "booking_form", customer_name="A", booking_date="2026-09-01")
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F")
    add_receipt_payment(journey, amount="21000", receipt_number="R1", receipt_date="2026-09-01")


def _mandatory_delivery_documents(journey, **invoice_fields):
    # the balance receipt after the booking amount is the Delivery receipt
    add_receipt_payment(journey, amount="480000", receipt_number="R2", receipt_date="2026-09-20")
    for template in get_registry().documents.values():
        if template.stage == "DELIVERY" and template.requirement == "REQUIRED" and template.key != "payment_receipt":
            fields = invoice_fields if template.key == "customer_invoice_dms" else {}
            _delivery_document(journey, template.key, **fields)


def _tasks(journey, task_type):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return [dict(r) for r in connection.execute(
            text("SELECT * FROM auditcore.p2_tasks WHERE tenant_id=:t AND journey_id=:j AND task_type=:k "
                 "ORDER BY created_at_utc"),
            {"t": journey.tenant_id, "j": journey.journey_id, "k": task_type},
        ).mappings().all()]


def _act(journey, task_id, action, *, role="PC", details=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action=action,
                             actor_id=journey.actor_id if role == "PC" else "tl-1", actor_role_code=role,
                             comment=None, details=details)


def _verify(journey, task_id):
    work = worker.WorkItem(journey.tenant_id, task_id, journey.journey_id, "TASK_VERIFY",
                           str(task_id), {}, 0, None, None, task_id)
    worker._task_verify(journey.engine, work)


def test_invoice_evidence_makes_conditional_documents_mandatory(journey):
    _complete_booking(journey)
    _mandatory_delivery_documents(journey, financed_by="HDFC Bank", accessories_cost="12500")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        reasons = condition_reasons(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert {"financeCase", "accessoriesSold"} <= set(reasons)
    assert "corporateDiscount" not in reasons

    result = _settle(journey)
    assert result["stage"] == "DELIVERY_DOCUMENT_UPLOAD"
    missing = {item["key"] for item in result["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["missing"]}
    assert {"delivery_order_cover", "bank_approval_letter", "accessory_invoice_dms",
            "accessory_invoice_tally"} == missing

    tasks = {t["reference"]["requirementKey"]: t for t in _tasks(journey, "DOCUMENT_MISSING")}
    assert set(tasks) == missing
    letter = tasks["bank_approval_letter"]
    assert letter["assigned_role_code"] == "PC" and letter["title"] == "Upload the Bank Approval Letter"
    assert "HDFC Bank" in letter["description"]
    _settle(journey)
    assert len(_tasks(journey, "DOCUMENT_MISSING")) == 4  # deduplicated, not raised again

    # the missing documents arrive; each task closes itself
    for key in missing:
        _delivery_document(journey, key)
    result = _settle(journey)
    assert {t["task_status"] for t in _tasks(journey, "DOCUMENT_MISSING")} == {"VERIFIED_COMPLETE"}
    assert result["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["passed"] is True
    assert "DELIVERY_DOCUMENTS_COMPLETE" in result["transitions"]


def test_a_task_closes_when_the_evidence_no_longer_applies(journey):
    invoice = add_ready_document(journey, "customer_invoice_dms", rsa_amount="1500")
    _settle(journey)
    [task] = _tasks(journey, "DOCUMENT_MISSING")
    assert task["reference"]["requirementKey"] == "rsa_invoice"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_document_extracted_fields SET effective_value='0'::jsonb "
                 "WHERE tenant_id=:t AND di_document_id=:d AND field_key='rsa_amount'"),
            {"t": journey.tenant_id, "d": invoice},
        )
    _settle(journey)
    [task] = _tasks(journey, "DOCUMENT_MISSING")
    assert task["task_status"] == "VERIFIED_COMPLETE"


def test_delivery_completes_from_rules_and_waits_for_the_tl_review(journey):
    _complete_booking(journey)
    _mandatory_delivery_documents(journey)
    result = _settle(journey)
    assert result["bookingCompletionState"] == "COMPLETE"
    assert result["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["passed"] is True
    assert result["stage"] == "DELIVERY_VERIFY_DOCUMENTS"  # no pictures of the car yet
    assert "DELIVERY_DOCUMENTS_COMPLETE" in result["transitions"]

    [photos] = _tasks(journey, "DELIVERY_VEHICLE_PHOTOS_MISSING")
    assert photos["assigned_role_code"] == "PC" and "PROVIDE_VEHICLE_ID" in photos["allowed_actions"]
    with pytest.raises(ValueError, match="17 characters"):
        _act(journey, photos["task_id"], "PROVIDE_VEHICLE_ID", details={"vin": "MA3SHORT"})
    _act(journey, photos["task_id"], "PROVIDE_VEHICLE_ID",
         details={"vin": "MA3EWDE1S00123456", "engineNumber": "K12N-7654321"})
    _verify(journey, photos["task_id"])
    [photos] = _tasks(journey, "DELIVERY_VEHICLE_PHOTOS_MISSING")
    assert photos["task_status"] == "VERIFIED_COMPLETE"

    [review] = _tasks(journey, "DELIVERY_REVIEW")
    assert review["assigned_role_code"] == "TL" and review["task_status"] == "READY"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        runtime = connection.execute(
            text("SELECT current_stage, delivery_completion_state FROM auditcore.p2_journey_runtime "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id},
        ).one()
        stage_row = connection.execute(
            text("SELECT business_status FROM auditcore.journey_stage_states "
                 "WHERE tenant_id=:t AND journey_id=:j AND stage_code='DELIVERY'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
        identity = connection.execute(
            text("SELECT vin, engine_number, entered_by_role FROM auditcore.p2_vehicle_identifications "
                 "WHERE tenant_id=:t AND journey_id=:j"), {"t": journey.tenant_id, "j": journey.journey_id},
        ).one()
    assert tuple(runtime) == ("DELIVERY_COMPLETE", "COMPLETE")
    assert stage_row == "DELIVERY_COMPLETED"
    assert tuple(identity) == ("MA3EWDE1S00123456", "K12N7654321", "PC")

    # a PC cannot close the Team Lead's review
    with pytest.raises(ValueError, match="role TL"):
        _act(journey, review["task_id"], "COMPLETE_ACTION")
    _act(journey, review["task_id"], "COMPLETE_ACTION", role="TL")
    _verify(journey, review["task_id"])
    [review] = _tasks(journey, "DELIVERY_REVIEW")
    assert review["task_status"] == "VERIFIED_COMPLETE"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        reviewed = connection.execute(
            text("SELECT review_completed_at_utc FROM auditcore.journeys WHERE tenant_id=:t AND journey_id=:j"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()
        events = connection.execute(
            text("SELECT event_type, actor_id FROM auditcore.journey_workflow_events "
                 "WHERE tenant_id=:t AND journey_id=:j ORDER BY recorded_at_utc"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).all()
    assert reviewed is not None
    assert [e[0] for e in events] == ["P2_BOOKING_COMPLETED", "P2_DELIVERY_COMPLETED", "P2_DELIVERY_REVIEWED"]
    assert events[-1][1] == "tl-1"
    # delivery stays complete; nothing is raised again
    assert _settle(journey)["stage"] == "DELIVERY_COMPLETE"
    assert len(_tasks(journey, "DELIVERY_REVIEW")) == 1


def test_open_pc_tasks_hold_the_delivery(journey):
    _complete_booking(journey)
    _mandatory_delivery_documents(journey)
    _delivery_document(journey, "gate_pass")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("INSERT INTO auditcore.p2_vehicle_identifications (tenant_id, journey_id, vin, entered_by_actor_id, "
                 "entered_by_role) VALUES (:t, :j, 'MA3EWDE1S00123456', 'pc', 'PC')"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        )
    # a low-confidence value on the invoice becomes a manual verification task
    [invoice] = [d for d in _invoice_ids(journey)]
    add_extracted_field(journey, di_document_id=invoice, field_key="grand_total_amount", value="2510000",
                        confidence=60.0, document_type="customer_invoice_dms")
    result = _settle(journey)
    gate = result["delivery"]["gates"]["PC_TASKS_CLOSED"]
    assert gate["passed"] is False and gate["pendingCount"] >= 1
    assert result["stage"] == "DELIVERY_VERIFY_DOCUMENTS"
    assert _tasks(journey, "DELIVERY_REVIEW") == []


def _invoice_ids(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT di_document_id FROM auditcore.evidence WHERE tenant_id=:t AND journey_id=:j "
                 "AND document_type_key='customer_invoice_dms'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalars().all()


def test_receipts_after_the_minimum_booking_amount_are_delivery_receipts(journey):
    add_receipt_payment(journey, amount="11000", receipt_number="R1", receipt_date="2026-09-01")
    add_receipt_payment(journey, amount="11000", receipt_number="R1", receipt_date="2026-09-01")  # duplicate
    missing = {i["key"] for i in _settle(journey)["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["missing"]}
    assert "payment_receipt" in missing
    add_receipt_payment(journey, amount="10000", receipt_number="R2", receipt_date="2026-09-03")
    add_receipt_payment(journey, amount="300000", receipt_number="R3", receipt_date="2026-09-25")
    # same number and amount on another date is a different receipt; it counts
    add_receipt_payment(journey, amount="300000", receipt_number="R3", receipt_date="2026-09-26")
    result = _settle(journey)
    gate = result["gates"]["MINIMUM_BOOKING_PAYMENT"]
    assert gate["passed"] is True and gate["duplicateReceiptsExcluded"] == 1
    assert [r["receiptNumber"] for r in gate["bookingReceipts"]] == ["R1", "R2"]
    assert gate["bookingReceiptTotal"] == "21000.00"
    assert [(r["receiptNumber"], r["receiptDate"]) for r in gate["deliveryReceipts"]] == [
        ("R3", "2026-09-25"), ("R3", "2026-09-26")]
    missing = {i["key"] for i in result["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["missing"]}
    assert "payment_receipt" not in missing and "dealer_receipt" not in missing


def test_conditional_documents_have_a_requirement_row_so_di_reads_them(journey):
    from audit_core.uc03_document_capture_v2 import (
        _requirement_refs_by_document_type_key,
    )
    from audit_core.uc03_unified_document_capture import _merged_candidate_requirements

    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        for _ in range(2):  # idempotent
            worker._ensure_p2_requirement_rows(connection, tenant_id=journey.tenant_id,
                                               journey_id=journey.journey_id)
        rows = connection.execute(
            text("SELECT document_type_key, requirement_level, process_area FROM auditcore.journey_document_requirements "
                 "WHERE tenant_id=:t AND journey_id=:j AND requirement_key LIKE 'p2\\_%'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).all()
        _, delivery = _merged_candidate_requirements(connection, tenant_id=journey.tenant_id,
                                                     journey_id=journey.journey_id)
    assert sorted(r[0] for r in rows) == ["bank_approval_letter", "debit_note", "purchase_order", "valuation_report"]
    assert {(r[1], r[2]) for r in rows} == {("OPTIONAL", "DELIVERY")}
    refs = _requirement_refs_by_document_type_key(delivery)
    assert {"bank_approval_letter", "debit_note", "purchase_order", "valuation_report"} <= set(refs)
