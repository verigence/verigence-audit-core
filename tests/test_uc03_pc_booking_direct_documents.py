from __future__ import annotations

import inspect

from audit_core.uc03_pc_booking_documents import (
    _capture_eligible_field_keys,
    _is_repeatable_requirement,
    acknowledge_booking_document_link,
)


def test_booking_context_exposes_only_fields_with_existing_typed_owner() -> None:
    booking = _capture_eligible_field_keys("booking_form")
    # Existing UC03 Legal Name handling deliberately excludes Booking Form
    # customer_name from PC acceptance; identity evidence owns legal-name review.
    assert "customer_name" not in booking
    assert "customer_phone" in booking
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
    # A second receipt against a repeatable requirement is a further
    # document, never a replacement: the supersede path (decision
    # 2026-10-01, see test_uc03_pc_booking_documents.py) is gated behind
    # `if not repeatable:` only.
    source = inspect.getsource(acknowledge_booking_document_link)
    assert "if not repeatable:" in source
    assert "'VOIDED', 'DUPLICATE_UPLOAD'" not in source
    assert source.index("if not repeatable:") < source.index("SET association_status='SUPERSEDED'")
    assert 'supersedes_evidence_id=NULL' in source


def test_delivery_payment_receipt_is_also_repeatable() -> None:
    # DI's document-link callback is not Booking-specific -- it now accepts
    # Delivery requirements too (see 0068), so Delivery's own payment-receipt
    # requirement key needs the same "don't supersede" treatment Booking's does.
    assert _is_repeatable_requirement("payment_receipt") is True


def test_document_link_callback_is_not_hardcoded_to_booking() -> None:
    # The requirement row says which process area a document belongs to; the
    # callback must not assume Booking. Asserts against the actual SQL rather
    # than behaviour so a future refactor can't silently reintroduce the
    # hardcode without touching this string.
    source = inspect.getsource(acknowledge_booking_document_link).replace(" ", "").replace("\n", "")
    assert "process_area)IN('BOOKING','DELIVERY')" in source
    assert "process_area)='BOOKING'" not in source

