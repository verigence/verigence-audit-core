from __future__ import annotations

from audit_core.uc03_p2_blueprint import (
    P2_DEDICATED_DI_TYPES,
    P2_DOCUMENT_BLUEPRINT,
    P2_DOCUMENT_TYPES,
    P2_FALLBACK_DOCUMENT_TYPES,
    P2_GENERALIZED_INVOICE_TYPES,
)


_EXPECTED_DOCUMENTS = {
    "booking_form",
    "pan_card",
    "aadhaar",
    "dealer_receipt",
    "customer_kyc",
    "gst_certificate",
    "corporate_id",
    "vehicle_rc",
    "transfer_letter",
    "authorization_letter",
    "wholesale_invoice",
    "customer_invoice_dms",
    "tax_invoice_tally",
    "insurance_cover",
    "accessory_invoice_dms",
    "accessory_invoice_tally",
    "rto_challan",
    "customer_ledger",
    "cost_sheet",
    "gate_pass",
    "ew_invoice",
    "rsa_invoice",
    "value_added_service_document",
    "no_dues_certificate",
    "payment_receipt",
    "bank_statement_extract",
    "credit_note",
    "gst_declaration",
    "scrappage_certificate_of_deposit",
}

_EXPECTED_FALLBACK = {
    "vehicle_rc",
    "transfer_letter",
    "authorization_letter",
    "cost_sheet",
    "value_added_service_document",
    "no_dues_certificate",
}

_EXPECTED_GENERALIZED_INVOICE = {
    "wholesale_invoice",
    "customer_invoice_dms",
    "tax_invoice_tally",
    "accessory_invoice_dms",
    "accessory_invoice_tally",
    "ew_invoice",
    "rsa_invoice",
    "credit_note",
}


def test_p2_blueprint_covers_exact_approved_29_document_universe() -> None:
    assert len(P2_DOCUMENT_BLUEPRINT) == 29
    assert P2_DOCUMENT_TYPES == _EXPECTED_DOCUMENTS


def test_p2_blueprint_di_schema_modes_match_verified_baseline() -> None:
    assert P2_FALLBACK_DOCUMENT_TYPES == _EXPECTED_FALLBACK
    assert P2_GENERALIZED_INVOICE_TYPES == _EXPECTED_GENERALIZED_INVOICE
    assert (
        P2_DEDICATED_DI_TYPES
        | P2_GENERALIZED_INVOICE_TYPES
        | P2_FALLBACK_DOCUMENT_TYPES
    ) == _EXPECTED_DOCUMENTS
    assert not (P2_DEDICATED_DI_TYPES & P2_GENERALIZED_INVOICE_TYPES)
    assert not (P2_DEDICATED_DI_TYPES & P2_FALLBACK_DOCUMENT_TYPES)
    assert not (P2_GENERALIZED_INVOICE_TYPES & P2_FALLBACK_DOCUMENT_TYPES)


def test_p2_blueprint_booking_scope_matches_approved_gates_and_capture_set() -> None:
    booking = {
        key: entry
        for key, entry in P2_DOCUMENT_BLUEPRINT.items()
        if entry.stage == "BOOKING"
    }
    assert set(booking) == {
        "booking_form", "pan_card", "aadhaar", "dealer_receipt", "customer_kyc"
    }
    assert booking["booking_form"].requirement_level == "REQUIRED"
    assert booking["dealer_receipt"].requirement_level == "REQUIRED"
    assert booking["customer_kyc"].requirement_level == "REQUIRED"
    assert booking["pan_card"].requirement_level == "OPTIONAL"
    assert booking["aadhaar"].requirement_level == "OPTIONAL"


def test_every_p2_document_declares_persistence_and_journey_or_task_use() -> None:
    for key, entry in P2_DOCUMENT_BLUEPRINT.items():
        assert entry.typed_store, key
        assert entry.processors, key
        assert entry.journey_360_sections or entry.task_effects, key
