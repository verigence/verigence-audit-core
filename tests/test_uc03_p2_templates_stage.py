"""P2 templates are valid and executable; the stage engine follows them."""
from __future__ import annotations

import dataclasses
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

from audit_core.db import set_tenant_context
from audit_core.uc03_p2_registry import (
    SUPPORTING_TEMPLATE,
    get_registry,
    validate_registry,
)
from audit_core.uc03_p2_stage import (
    read_booking_stage,
    recompute_journey_stage,
    unreviewed_fields,
)

# ------------------------------------------------------------------ templates


def test_templates_load_and_validate():
    registry = get_registry()
    assert validate_registry(registry) == []
    assert len(registry.controls) == 88  # 25 + 22 deal-audit Audit Core + 41 Rule Engine (blueprint 8.1, six retired)
    assert SUPPORTING_TEMPLATE in registry.documents


def test_every_blueprint_document_has_a_template():
    blueprint_documents = {
        "booking_docket", "pan_card", "aadhaar", "dealer_receipt", "customer_kyc", "gst_certificate",
        "corporate_id", "vehicle_rc", "transfer_letter", "authorization_letter", "wholesale_invoice",
        "customer_invoice_dms", "tax_invoice_tally", "insurance_cover", "accessory_invoice_dms",
        "accessory_invoice_tally", "rto_challan", "customer_ledger", "cost_sheet", "gate_pass",
        "ew_invoice", "rsa_invoice", "value_added_service_document", "no_dues_certificate",
        "payment_receipt", "bank_statement_extract", "credit_note", "gst_declaration",
        "scrappage_certificate_of_deposit",
    }
    assert blueprint_documents <= set(get_registry().documents)


def test_booking_gates_match_the_approved_contract():
    gates = {g.key: g for g in get_registry().stages["BOOKING"].gates}
    # Booking completes on the Booking Docket, PAN or Aadhaar, and receipts
    # reaching the minimum booking amount (requirements document, 28 Sep 2026)
    assert set(gates) == {"BOOKING_FORM_EXTRACTED", "KYC_EXTRACTED", "MINIMUM_BOOKING_PAYMENT"}
    assert gates["KYC_EXTRACTED"].match == "ANY"
    assert set(gates["KYC_EXTRACTED"].documents) == {"pan_card", "aadhaar"}
    delivery = get_registry().stages["DELIVERY"]
    assert delivery.completion_approved is True
    assert {g.key: g.kind for g in delivery.gates} == {
        "REQUIRED_DOCUMENTS": "REQUIRED_DOCUMENTS", "VEHICLE_PROOF": "VEHICLE_PROOF", "PC_TASKS_CLOSED": "OPEN_TASKS",
    }


def test_receipts_resolve_by_stage_and_unknown_types_are_supporting():
    registry = get_registry()
    assert registry.template_for_di_type("dealer_receipt", stage="BOOKING").key == "dealer_receipt"
    assert registry.template_for_di_type("dealer_receipt", stage="DELIVERY").key == "payment_receipt"
    assert registry.template_for_di_type("booking_form").key == "booking_docket"
    assert registry.template_for_di_type(None).key == SUPPORTING_TEMPLATE
    assert registry.template_for_di_type("employer_letter").key == SUPPORTING_TEMPLATE


def test_strict_fields_raise_the_review_bar():
    receipt = get_registry().document("dealer_receipt")
    assert receipt.needs_review("amount_paid", 95.0) is False  # one bar for every field: < 90
    assert receipt.needs_review("amount_paid", 89.0) is True
    assert receipt.needs_review("receipt_date", 95.0) is False
    assert receipt.needs_review("receipt_date", None) is True  # missing confidence is never trusted


def test_validation_rejects_drift():
    registry = get_registry()
    broken_doc = dataclasses.replace(
        registry.document("pan_card"), key_fields=("pan_number", "not_a_di_field"), controls=("NO_SUCH_RULE",),
    )
    broken = dataclasses.replace(registry, documents={**registry.documents, "pan_card": broken_doc})
    problems = validate_registry(broken)
    assert any("not_a_di_field" in p for p in problems)
    assert any("NO_SUCH_RULE" in p for p in problems)


# --------------------------------------------------------------- stage engine


@pytest.fixture
def journey():
    engine = database_engine()
    created = create_p2_journey(engine, prefix="p2st")
    set_minimum_booking_amount(created, "21000")
    try:
        yield created
    finally:
        delete_tenant_data(engine, created.tenant_id)
        engine.dispose()


def _recompute(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return recompute_journey_stage(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)


def _stage_events(journey):
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        return connection.execute(
            text("SELECT COUNT(*) FROM auditcore.p2_activity_events "
                 "WHERE tenant_id=:t AND journey_id=:j AND event_type='STAGE_CHANGED'"),
            {"t": journey.tenant_id, "j": journey.journey_id},
        ).scalar_one()


def test_booking_completes_from_documents_and_payment(journey):
    add_ready_document(journey, "booking_form", customer_name="A")
    add_ready_document(journey, "pan_card", pan_number="ABCDE1234F")
    add_ready_document(journey, "aadhaar", aadhaar_number="1234")
    add_receipt_payment(journey, amount="11000", receipt_number="R1", receipt_date="2026-09-01")
    add_receipt_payment(journey, amount="10000", receipt_number="R2", receipt_date=None)  # dateless still counts

    result = _recompute(journey)
    assert result["stage"] == "BOOKING_COMPLETE"
    assert result["gates"]["MINIMUM_BOOKING_PAYMENT"]["receiptTotal"] == "21000.00"
    assert _stage_events(journey) == 1
    _recompute(journey)
    assert _stage_events(journey) == 1  # no event without a change


def test_missing_document_explains_the_next_action(journey):
    add_ready_document(journey, "booking_docket", customer_name="A")  # legacy key still counts
    add_receipt_payment(journey, amount="25000", receipt_number="R1", receipt_date="2026-09-01")
    result = _recompute(journey)
    assert result["stage"] == "BOOKING_DOCUMENT_UPLOAD"
    assert result["gates"]["BOOKING_FORM_EXTRACTED"]["passed"] is True
    assert "PAN or Aadhaar" in result["gates"]["KYC_EXTRACTED"]["action"]
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        stored = read_booking_stage(connection, tenant_id=journey.tenant_id, journey_id=journey.journey_id)
    assert stored["stage"] == "BOOKING_DOCUMENT_UPLOAD"
    assert stored["gates"]["KYC_EXTRACTED"]["passed"] is False


def test_either_pan_or_aadhaar_completes_the_kyc(journey):
    add_ready_document(journey, "booking_form", customer_name="A")
    add_ready_document(journey, "aadhaar", aadhaar_number="1234")  # no PAN
    add_receipt_payment(journey, amount="21000", receipt_number="R1", receipt_date="2026-09-01")
    result = _recompute(journey)
    assert result["gates"]["KYC_EXTRACTED"]["passed"] is True
    assert result["stage"] == "BOOKING_COMPLETE"
    assert "BOOKING_COMPLETED" in result["transitions"]
    assert "BOOKING_COMPLETED" not in _recompute(journey)["transitions"]  # once


def test_duplicate_and_voided_receipts_do_not_count(journey):
    add_ready_document(journey, "booking_form", customer_name="A")
    add_ready_document(journey, "pan_card", pan_number="P")
    add_ready_document(journey, "aadhaar", aadhaar_number="1")
    add_receipt_payment(journey, amount="15000", receipt_number="R-9", receipt_date="2026-09-01")
    add_receipt_payment(journey, amount="15000", receipt_number="R-9", receipt_date="2026-09-01")  # duplicate
    voided = uuid4()
    add_evidence(journey, di_document_id=voided, document_type_key="dealer_receipt", status="VOIDED")
    add_receipt_payment(journey, amount="50000", receipt_number="R-X", receipt_date="2026-09-02",
                        di_document_id=voided)
    result = _recompute(journey)
    gate = result["gates"]["MINIMUM_BOOKING_PAYMENT"]
    assert gate["passed"] is False
    assert gate["receiptTotal"] == "15000.00"
    assert gate["duplicateReceiptsExcluded"] == 1
    assert gate["shortfall"] == "6000.00"


def test_low_confidence_fields_raise_tasks_but_do_not_hold_the_booking(journey):
    # Booking completion is document driven; a low-confidence value becomes a
    # PC manual verification task, which holds the Delivery instead.
    add_ready_document(journey, "booking_form", customer_name="A")
    add_ready_document(journey, "pan_card", pan_number="P")
    aadhaar = add_ready_document(journey, "aadhaar", aadhaar_number="1")
    add_receipt_payment(journey, amount="21000", receipt_number="R1", receipt_date="2026-09-01")
    add_extracted_field(journey, di_document_id=aadhaar, field_key="aadhaar_name", value="X",
                        confidence=72.0, document_type="aadhaar")
    result = _recompute(journey)
    assert result["stage"] == "BOOKING_COMPLETE"
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        pending = unreviewed_fields(connection, get_registry(), tenant_id=journey.tenant_id,
                                    journey_id=journey.journey_id)
    assert [p["fieldKey"] for p in pending] == ["aadhaar_name"]


def test_money_fields_share_the_ninety_percent_bar(journey):
    # One bar everywhere: a value at or above 90 needs no check, one below does.
    add_ready_document(journey, "dealer_receipt", confidence=95.0, amount_paid="21000")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        pending = unreviewed_fields(connection, get_registry(), tenant_id=journey.tenant_id,
                                    journey_id=journey.journey_id)
    assert pending == []

    receipt = add_ready_document(journey, "dealer_receipt", confidence=89.0, amount_paid="21000")
    with journey.engine.begin() as connection:
        set_tenant_context(connection, journey.tenant_id)
        pending = unreviewed_fields(connection, get_registry(), tenant_id=journey.tenant_id,
                                    journey_id=journey.journey_id)
    assert [(p["documentId"], p["fieldKey"], p["threshold"]) for p in pending] == [
        (str(receipt), "amount_paid", 90.0)
    ]


def test_delivery_readiness_lists_missing_required_documents(journey):
    result = _recompute(journey)
    missing = {item["key"] for item in result["delivery"]["missing"]}
    assert {"no_dues_certificate", "customer_invoice_dms", "tax_invoice_tally", "insurance_cover",
            "customer_ledger", "cost_sheet", "payment_receipt", "vehicle_proof"} <= missing
    assert "gate_pass" not in missing  # optional
    assert "gst_certificate" not in missing  # conditional, customer is not corporate
