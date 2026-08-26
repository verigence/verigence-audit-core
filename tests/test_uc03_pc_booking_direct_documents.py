from __future__ import annotations

import inspect
from uuid import uuid4

import pytest
from pydantic import ValidationError

from audit_core.errors import AuditCoreError
from audit_core.uc03_pc_booking_documents import (
    BookingExtractionDecisionCommand,
    BookingExtractionFieldDecision,
    _capture_eligible_field_keys,
    _current_linked_evidence,
    _is_repeatable_requirement,
    _validate_unique_decisions,
    acknowledge_booking_document_link,
)


def _field(*, field_key: str = "customer_name", confidence: float | None = 0.91):
    return BookingExtractionFieldDecision(
        fieldKey=field_key,
        sourceFactRef=uuid4(),
        sourceFactVersion=1,
        sourceConfidence=confidence,
        decision="APPROVED",
        approvedValue="Approved value",
    )


def test_booking_context_exposes_only_fields_with_existing_typed_owner() -> None:
    booking = _capture_eligible_field_keys("booking_form")
    assert "customer_name" in booking
    assert "booking_date" in booking
    assert "vehicle_model" not in booking


def test_dealer_receipt_reuses_existing_payment_capture_mapping() -> None:
    receipt = _capture_eligible_field_keys("dealer_receipt")
    assert "receipt_number" in receipt
    assert "amount_paid" in receipt
    assert "payment_reference_no" in receipt


def test_booking_payment_receipt_is_repeatable_but_identity_documents_are_not() -> None:
    assert _is_repeatable_requirement("booking_payment_receipt") is True
    assert _is_repeatable_requirement("booking_docket") is False
    assert _is_repeatable_requirement("pan_card") is False
    assert _is_repeatable_requirement("aadhaar") is False


def test_repeatable_callback_does_not_supersede_prior_receipts() -> None:
    source = inspect.getsource(acknowledge_booking_document_link)
    assert "if not repeatable:" in source
    assert "association_status='SUPERSEDED'" in source
    assert 'supersedes_evidence_id=NULL' in source


def test_repeatable_review_accepts_any_active_document_for_requirement() -> None:
    source = inspect.getsource(_current_linked_evidence)
    assert "_is_repeatable_requirement" in source
    assert "di_document_id=:document_id" in source
    assert "association_status='ACTIVE'" in source


def test_confidence_is_transport_provenance_and_accepts_native_di_scale() -> None:
    field = _field(confidence=0.82)
    assert field.sourceConfidence == 0.82


def test_confidence_rejects_out_of_contract_value() -> None:
    with pytest.raises(ValidationError):
        _field(confidence=100.01)


def test_document_batch_requires_one_document_identity() -> None:
    requirement_ref = uuid4()
    document_id = uuid4()
    command = BookingExtractionDecisionCommand(
        requirementRef=requirement_ref,
        documentId=document_id,
        fields=[_field()],
    )
    assert command.requirementRef == requirement_ref
    assert command.documentId == document_id
    assert len(command.fields) == 1


def test_duplicate_field_decision_is_rejected_before_any_typed_write() -> None:
    fact_ref = uuid4()
    first = BookingExtractionFieldDecision(
        fieldKey="customer_name",
        sourceFactRef=fact_ref,
        sourceFactVersion=1,
        sourceConfidence=0.9,
        decision="APPROVED",
        approvedValue="A",
    )
    duplicate = BookingExtractionFieldDecision(
        fieldKey="CUSTOMER_NAME",
        sourceFactRef=uuid4(),
        sourceFactVersion=1,
        sourceConfidence=0.9,
        decision="CORRECTED",
        approvedValue="B",
    )

    with pytest.raises(AuditCoreError, match="Duplicate extraction decision"):
        _validate_unique_decisions([first, duplicate])
