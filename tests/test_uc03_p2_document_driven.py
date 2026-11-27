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
    missing_document_lines,
    set_minimum_booking_amount,
)
from sqlalchemy import text

from audit_core import uc03_p2_worker as worker
from audit_core.db import set_tenant_context
from audit_core.uc03_p2_registry import get_registry
from audit_core.uc03_p2_stage import condition_reasons, requirement_items
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


def _act(journey, task_id, action, *, role="PC", details=None, comment=None):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return submit_action(connection, tenant_id=journey.tenant_id, task_id=task_id, action=action,
                             actor_id=journey.actor_id if role == "PC" else "tl-1", actor_role_code=role,
                             comment=comment, details=details)


def _verify(journey, task_id):
    work = worker.WorkItem(journey.tenant_id, task_id, journey.journey_id, "TASK_VERIFY",
                           str(task_id), {}, 0, None, None, task_id)
    worker._task_verify(journey.engine, work)


def test_an_invoice_financier_alone_is_not_a_finance_case(journey):
    # Product owner 2026-10-27: finance only from the RTO, the insurance
    # hypothecation or a receipt from the financier -- never from the invoice.
    from audit_core.uc03_finance_rule import apply_finance_rule

    _complete_booking(journey)
    _mandatory_delivery_documents(journey, financed_by="CASH", accessories_cost="12500")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert apply_finance_rule(connection, tenant_id=journey.tenant_id,
                                  journey_id=journey.journey_id)["action"] == "NONE"
        reasons = condition_reasons(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert "financeCase" not in reasons and "accessoriesSold" in reasons


def test_invoice_evidence_makes_conditional_documents_mandatory(journey):
    from audit_core.uc03_finance_rule import apply_finance_rule

    _complete_booking(journey)
    _mandatory_delivery_documents(journey, financed_by="HDFC Bank", accessories_cost="12500")
    # The RTO challan proves the finance (hypothecation to the financier).
    add_ready_document(journey, "rto_challan", financer_name="HDFC BANK LTD", hp_charges_amount="1500")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        assert apply_finance_rule(connection, tenant_id=journey.tenant_id,
                                  journey_id=journey.journey_id)["action"] == "CREATED"
        reasons = condition_reasons(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert {"financeCase", "accessoriesSold"} <= set(reasons)
    assert "corporateDiscount" not in reasons

    result = _settle(journey)
    assert result["stage"] == "DELIVERY_DOCUMENT_UPLOAD"
    missing = {item["key"] for item in result["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["missing"]}
    # The DO and the bank approval letter are one requirement, met by either
    # (product owner 2026-10-30); so are the two accessory invoice formats
    # (product owner 2026-10-31: there is no separate Tally accessory invoice).
    assert {"FINANCE_APPROVAL", "ACCESSORY_INVOICE"} == missing

    # ONE task for the journey, a line per missing document (product owner 2026-11-15).
    lines = missing_document_lines(journey)
    assert set(lines) == missing
    [task] = _tasks(journey, "DOCUMENT_MISSING")
    assert task["assigned_role_code"] == "PC"
    assert task["title"] == ("Upload 2 missing documents: Accessory Invoice, "
                             "Delivery Order (Financier) or Bank Approval Letter")
    letter = lines["FINANCE_APPROVAL"]
    assert set(letter["templates"]) == {"delivery_order_cover", "bank_approval_letter"}
    assert "HDFC BANK LTD" in letter["reason"]  # the RTO names the financier
    assert set(lines["ACCESSORY_INVOICE"]["templates"]) == {"accessory_invoice_dms", "accessory_invoice_tally"}
    _settle(journey)
    assert len(_tasks(journey, "DOCUMENT_MISSING")) == 1  # deduplicated, not raised again

    # the missing documents arrive -- only the bank approval letter of the
    # two, and only a Tally-format accessory invoice -- and each task closes itself
    for key in ("bank_approval_letter", "accessory_invoice_tally"):
        _delivery_document(journey, key)
    result = _settle(journey)
    assert {t["task_status"] for t in _tasks(journey, "DOCUMENT_MISSING")} == {"VERIFIED_COMPLETE"}
    assert result["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["passed"] is True
    assert "DELIVERY_DOCUMENTS_COMPLETE" in result["transitions"]


def _delivery_requirements(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        reasons = condition_reasons(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
        items = requirement_items(
            connection, get_registry(), tenant_id=journey.tenant_id,
            journey_id=journey.journey_id, stage="DELIVERY", reasons=reasons,
        )
    return reasons, {item["key"]: item for item in items}


def test_the_debit_note_is_optional_whoever_arranges_the_insurance(journey):
    """Product owner 2026-11-27: the debit note "can be optional but not mandatory". It was
    expected (CONDITIONAL on dealer insurance, with no task), and so held the Delivery's
    mandatory documents open with nothing telling the PC why."""
    add_ready_document(journey, "booking_form", customer_name="A", booking_date="2026-09-01",
                       insurance_by="Dealer")
    _settle(journey)

    reasons, items = _delivery_requirements(journey)
    assert "insuranceByDealer" in reasons
    # the insurance fact says nothing about who registers the vehicle
    assert "registrationByDealer" not in reasons
    assert items["debit_note"]["required"] is False and items["debit_note"]["requirement"] == "OPTIONAL"
    assert "debit_note" not in missing_document_lines(journey)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        gate = connection.execute(text(
            "SELECT details FROM auditcore.p2_stage_gate_state WHERE tenant_id=:t AND journey_id=:j "
            "AND stage_code='DELIVERY' AND gate_key='REQUIRED_DOCUMENTS'"),
            {"t": journey.tenant_id, "j": journey.journey_id}).scalar_one()
    assert "debit_note" not in [m["key"] for m in gate["missing"]]


def test_a_conditional_document_without_the_flag_still_raises_its_task(journey):
    """Guard for the raises_missing_task default: suppression is opt-in, so
    an ordinary CONDITIONAL document still produces its Document Missing
    task."""
    add_ready_document(journey, "customer_invoice_dms", accessories_cost="12500", process_area="DELIVERY")
    _settle(journey)

    _, items = _delivery_requirements(journey)
    assert items["ACCESSORY_INVOICE"]["raisesMissingTask"] is True
    assert "ACCESSORY_INVOICE" in missing_document_lines(journey)
    # The RSA and extended warranty invoices are not mandatory (product owner
    # 2026-11-11): charged or not, neither is ever asked for.
    add_ready_document(journey, "customer_ledger", rsa_amount="1500", additional_warranty_amount="9999",
                       process_area="DELIVERY")
    _settle(journey)
    assert not {"rsa_invoice", "ew_invoice"} & set(missing_document_lines(journey))


def test_a_delivery_document_is_not_asked_for_while_the_journey_is_at_booking(journey):
    """2026-10-28: the accessories are charged, but no accessory invoice can
    exist before delivery; PCs could only comment "it is in booking stage" on
    the task. It is raised once a delivery document is on file. (Since
    2026-11-02 the booking docket alone sells nothing: the ledger's charge
    does. Since 2026-11-11 the RSA invoice this test used is not mandatory.)"""
    add_ready_document(journey, "booking_docket", customer_name="A", booking_date="2026-09-01")
    add_ready_document(journey, "customer_ledger", accessories_cost="12500")
    _settle(journey)
    reasons, items = _delivery_requirements(journey)
    assert "accessoriesSold" in reasons and items["ACCESSORY_INVOICE"]["required"] is True
    assert not [t for t in _tasks(journey, "DOCUMENT_MISSING") if t["task_status"] != "VERIFIED_COMPLETE"]
    add_ready_document(journey, "customer_invoice_dms", accessories_cost="12500", process_area="DELIVERY")
    _settle(journey)
    assert "ACCESSORY_INVOICE" in missing_document_lines(journey)


def test_a_task_closes_when_the_evidence_no_longer_applies(journey):
    invoice = add_ready_document(journey, "customer_invoice_dms", accessories_cost="12500", process_area="DELIVERY")
    _settle(journey)
    # The five mandatory delivery documents are asked for beside it (2026-11-15).
    lines = missing_document_lines(journey)
    assert {"ACCESSORY_INVOICE", "DEALER_STATEMENT"} <= set(lines)
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(
            text("UPDATE auditcore.journey_document_extracted_fields SET effective_value='0'::jsonb "
                 "WHERE tenant_id=:t AND di_document_id=:d AND field_key='accessories_cost'"),
            {"t": journey.tenant_id, "d": invoice},
        )
    _settle(journey)
    lines = missing_document_lines(journey)
    assert "ACCESSORY_INVOICE" not in lines
    assert lines["DEALER_STATEMENT"]["taskStatus"] == "READY"


def _pc_task(journey, task_type: str, category: str, status: str = "READY"):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("INSERT INTO auditcore.p2_tasks (tenant_id, journey_id, task_type, category, origin_kind, "
                 "source_type, source_code, title, description, dedupe_key, assigned_role_code, task_status, "
                 "completion_protocol) VALUES (:t, :j, :type, :category, 'SYSTEM', 'DOCUMENT', :type, 'x', 'x', "
                 ":k, 'PC', :s, 'MACHINE_VERIFIED') RETURNING task_id"),
            {"t": journey.tenant_id, "j": journey.journey_id, "type": task_type, "category": category,
             "k": f"closure-rule-{uuid4().hex}", "s": status}).scalar_one()


def test_a_delivery_file_closes_by_the_one_closure_rule(journey):
    """Product owner 2026-11-27: "we can close a file if all the mandatory documents are uploaded
    and all manual verification tasks closed", with the vehicle's VIN or chassis number on the
    Journey; car photos are optional; a missing mandatory document keeps the file open."""
    _complete_booking(journey)
    # A mandatory document missing: open, whatever else is done.
    add_receipt_payment(journey, amount="480000", receipt_number="R2", receipt_date="2026-09-20")
    _delivery_document(journey, "customer_invoice_dms")
    held = _settle(journey)
    assert held["deliveryCompletionState"] == "IN_PROGRESS"
    assert held["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["passed"] is False

    # Every mandatory document in, a manual verification task open: still open.
    manual = _pc_task(journey, "PC_CONFIRMATION", "MANUAL_VERIFICATION")
    _mandatory_delivery_documents(journey)
    held = _settle(journey)
    assert held["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["passed"] is True
    assert held["delivery"]["gates"]["PC_TASKS_CLOSED"]["passed"] is False
    assert held["stage"] == "DELIVERY_VERIFY_DOCUMENTS" and held["deliveryCompletionState"] == "IN_PROGRESS"

    # That task closed; a document task of the PC (a page to upload again) is open and there are
    # no pictures of the car: neither holds the file. It closes.
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        connection.execute(text("UPDATE auditcore.p2_tasks SET task_status='VERIFIED_COMPLETE', verified_at_utc=now() "
                                "WHERE task_id=:id"), {"id": manual})
    _pc_task(journey, "PC_PAGE_QUALITY_REJECTED", "DOCUMENT_REUPLOAD")
    closed = _settle(journey)
    assert closed["delivery"]["gates"]["VEHICLE_PROOF"]["passed"] is False
    assert closed["stage"] == "DELIVERY_COMPLETE" and closed["deliveryCompletionState"] == "COMPLETE"


def test_without_the_vehicles_vin_or_chassis_on_the_journey_nothing_closes(journey):
    """The VIN or chassis number must be on the Journey (2026-11-27): without it the Journey is a
    booking (uc03_delivery_start), and no delivery file to close."""
    _complete_booking(journey)
    add_receipt_payment(journey, amount="480000", receipt_number="R2", receipt_date="2026-09-20")
    for template in get_registry().documents.values():
        if template.stage == "DELIVERY" and template.requirement == "REQUIRED" and template.key != "payment_receipt":
            document_id = uuid4()
            add_evidence(journey, di_document_id=document_id, document_type_key=template.key,
                         process_area="DELIVERY", chassis=None)
            add_extracted_field(journey, di_document_id=document_id, field_key="marker", value="x",
                                document_type=template.key, confidence=99.0)
    result = _settle(journey)
    assert result["delivery"]["started"] is False
    assert result["stage"] == "BOOKING_COMPLETE" and result["deliveryCompletionState"] == "IN_PROGRESS"


def test_the_minimum_booking_amount_does_not_hold_a_journey_whose_vehicle_is_on_file(journey):
    """Product owner 2026-11-27: "kyc docs and booking docket are mandatory, if they are not, then
    not allowed, otherwise payment if VIN is there can be ignored"."""
    add_ready_document(journey, "booking_form", customer_name="A", booking_date="2026-09-01")
    add_receipt_payment(journey, amount="5000", receipt_number="R1", receipt_date="2026-09-01")  # short of 21,000
    # No KYC, receipts short, no vehicle: a booking, and not a complete one.
    result = _settle(journey)
    assert result["bookingCompletionState"] == "IN_PROGRESS" and result["stage"].startswith("BOOKING_")

    # KYC in; still short of the minimum booking amount, and no vehicle on file: the payment holds it.
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F")
    result = _settle(journey)
    assert result["gates"]["MINIMUM_BOOKING_PAYMENT"]["passed"] is False
    assert result["bookingCompletionState"] == "IN_PROGRESS"

    # The vehicle's chassis is on file: the minimum booking amount is not what keeps the file
    # open -- the booking is complete on its Docket and KYC, and the Journey is worked as a
    # delivery. The amount's own check still says the receipts are short, and the delivery's
    # payment receipt is still one of its mandatory documents.
    for template in get_registry().documents.values():
        if template.stage == "DELIVERY" and template.requirement == "REQUIRED" and template.key != "payment_receipt":
            _delivery_document(journey, template.key)
    result = _settle(journey)
    assert result["delivery"]["started"] is True
    assert result["gates"]["MINIMUM_BOOKING_PAYMENT"]["passed"] is False
    assert result["gates"]["MINIMUM_BOOKING_PAYMENT"]["ignoredForDelivery"] is True
    assert result["bookingCompletionState"] == "COMPLETE"
    assert result["stage"] == "DELIVERY_DOCUMENT_UPLOAD"
    assert [m["key"] for m in result["delivery"]["missing"]] == ["payment_receipt"]


def test_without_the_kyc_a_delivery_does_not_close_whatever_else_is_in(journey):
    add_ready_document(journey, "booking_form", customer_name="A", booking_date="2026-09-01")
    add_receipt_payment(journey, amount="21000", receipt_number="R1", receipt_date="2026-09-01")
    _mandatory_delivery_documents(journey)
    result = _settle(journey)
    assert result["delivery"]["started"] is True
    assert result["gates"]["KYC_EXTRACTED"]["passed"] is False
    assert result["bookingCompletionState"] == "IN_PROGRESS" and result["deliveryCompletionState"] == "IN_PROGRESS"


def test_the_pc_closes_the_car_pictures_task_with_a_proper_reason(journey):
    """Product owner 2026-11-27: "task should be raised for photos.. it has to be there.. but it
    can be closed without upload by PC with a proper reason"."""
    _complete_booking(journey)
    _settle(journey)
    _mandatory_delivery_documents(journey)
    _settle(journey)
    [photos] = _tasks(journey, "DELIVERY_VEHICLE_PHOTOS_MISSING")
    assert photos["task_status"] == "READY" and "CLOSE_TASK" in photos["allowed_actions"]

    with pytest.raises(ValueError, match="reason there are no pictures"):
        _act(journey, photos["task_id"], "CLOSE_TASK", comment="na")
    closed = _act(journey, photos["task_id"], "CLOSE_TASK",
                  comment="Customer took delivery at night; the yard camera was not working.")
    assert closed["status"] == "VERIFIED_COMPLETE"

    # The next settle does not ask again: the reason stands, and it is on the task.
    _settle(journey)
    [photos] = _tasks(journey, "DELIVERY_VEHICLE_PHOTOS_MISSING")
    assert photos["task_status"] == "VERIFIED_COMPLETE"
    assert photos["completion_result"]["outcome"] == "CLOSED_BY_PC"
    assert "yard camera" in photos["completion_result"]["reason"]


def test_delivery_completes_from_rules_and_waits_for_the_tl_review(journey):
    _complete_booking(journey)
    _settle(journey)  # the booking is settled when its papers come in, before the delivery's
    _mandatory_delivery_documents(journey)
    result = _settle(journey)
    assert result["bookingCompletionState"] == "COMPLETE"
    assert result["delivery"]["gates"]["REQUIRED_DOCUMENTS"]["passed"] is True
    # The one closure rule (product owner 2026-11-27): the vehicle's chassis on file, every
    # mandatory document uploaded, no manual verification task open -- the file is closed. Car
    # photos are optional: still asked of the PC below, never holding the file.
    assert result["stage"] == "DELIVERY_COMPLETE"
    assert result["delivery"]["gates"]["VEHICLE_PROOF"]["passed"] is False
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
    # The same number and amount with a date read a day apart is the same receipt: counted once
    # (product owner 2026-11-16, SHI-SS-261028-001).
    add_receipt_payment(journey, amount="300000", receipt_number="R3", receipt_date="2026-09-26")
    result = _settle(journey)
    gate = result["gates"]["MINIMUM_BOOKING_PAYMENT"]
    assert gate["passed"] is True and gate["duplicateReceiptsExcluded"] == 2
    assert [r["receiptNumber"] for r in gate["bookingReceipts"]] == ["R1", "R2"]
    assert gate["bookingReceiptTotal"] == "21000.00"
    assert [(r["receiptNumber"], r["receiptDate"]) for r in gate["deliveryReceipts"]] == [("R3", "2026-09-25")]
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
    # form_21: the Form 21's own row (2026-11-02, uc03_form_21); delivery_receipt: the
    # vehicle delivery receipt's, never a money receipt (2026-11-02).
    assert sorted(r[0] for r in rows) == ["bank_approval_letter", "debit_note", "delivery_receipt", "form_21", "purchase_order",
                                          "valuation_report"]
    assert {(r[1], r[2]) for r in rows} == {("OPTIONAL", "DELIVERY")}
    refs = _requirement_refs_by_document_type_key(delivery)
    assert {"bank_approval_letter", "debit_note", "delivery_receipt", "form_21", "purchase_order", "valuation_report"} <= set(refs)
