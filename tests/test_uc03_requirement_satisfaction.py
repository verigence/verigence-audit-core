from __future__ import annotations

from uuid import uuid4

from audit_core.uc03_requirement_satisfaction import (
    first_linked_document_ids,
    resolve_requirement_satisfaction,
)


def _requirement(
    key: str,
    *,
    level: str = "REQUIRED",
    status: str = "PENDING",
    doc_type: str | None = None,
) -> dict:
    return {
        "requirement_key": key,
        "requirement_level": level,
        "requirement_status": status,
        "display_label": key.replace("_", " ").title(),
        "document_type_key": doc_type or key,
        "condition_key": None,
    }


def _document(
    requirement_key: str | None,
    *,
    di_document_id=None,
    capture_status: str = "CLASSIFIED",
    created_at_utc: str = "2026-09-24T10:00:00Z",
) -> dict:
    return {
        "di_document_id": di_document_id or uuid4(),
        "client_upload_id": "client-1",
        "requirement_key": requirement_key,
        "classified_document_type_key": requirement_key,
        "capture_status": capture_status,
        "original_filename": "doc.pdf",
        "content_type": "application/pdf",
        "created_at_utc": created_at_utc,
    }


def test_satisfied_when_a_classified_document_is_linked() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("booking_form")],
        documents=[_document("booking_form")],
    )
    assert result["booking_form"].satisfied is True
    assert result["booking_form"].reason == "SATISFIED"
    assert result["booking_form"].active_document is not None


def test_no_document_reason_when_nothing_uploaded() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("customer_kyc")],
        documents=[],
    )
    assert result["customer_kyc"].satisfied is False
    assert result["customer_kyc"].reason == "NO_DOCUMENT"


def test_not_yet_classified_reason_when_document_uploaded_but_unclassified() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("customer_kyc")],
        documents=[_document("customer_kyc", capture_status="RECEIVING")],
    )
    assert result["customer_kyc"].satisfied is False
    assert result["customer_kyc"].reason == "NOT_YET_CLASSIFIED"


def test_not_applicable_requirements_are_always_satisfied() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="DELIVERY",
        requirements=[_requirement("gst_certificate", status="NOT_APPLICABLE")],
        documents=[],
    )
    assert result["gst_certificate"].satisfied is True
    assert result["gst_certificate"].reason == "NOT_APPLICABLE"


def test_first_classified_document_by_created_at_wins_the_slot() -> None:
    # Confirmed live: the exact rule already proven in
    # _build_capture_response/_build_delivery_capture_response (ordered
    # query + dict.setdefault) -- must not be re-derived differently here.
    first_id = uuid4()
    second_id = uuid4()
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("booking_form")],
        documents=[
            _document("booking_form", di_document_id=first_id, created_at_utc="2026-09-24T10:00:00Z"),
            _document("booking_form", di_document_id=second_id, created_at_utc="2026-09-24T10:05:00Z"),
        ],
    )
    assert result["booking_form"].active_document_id == first_id


def test_first_linked_document_wins_the_slot_even_if_still_unclassified() -> None:
    # Distinct from satisfaction's own "active document" rule: a single
    # not-yet-classified upload still owns its slot, it just isn't
    # satisfied yet -- must not be nulled just because it's not CLASSIFIED.
    doc_id = uuid4()
    result = first_linked_document_ids(
        [_document("booking_form", di_document_id=doc_id, capture_status="RECEIVING")],
        is_repeatable=lambda key: False,
    )
    assert result == {"booking_form": doc_id}


def test_first_linked_document_ids_ignores_repeatable_requirements() -> None:
    result = first_linked_document_ids(
        [_document("minimum_booking_payment_proof"), _document("minimum_booking_payment_proof")],
        is_repeatable=lambda key: True,
    )
    assert result == {}


def test_first_linked_document_ids_keeps_the_earliest_by_iteration_order() -> None:
    first_id, second_id = uuid4(), uuid4()
    result = first_linked_document_ids(
        [_document("booking_form", di_document_id=first_id), _document("booking_form", di_document_id=second_id)],
        is_repeatable=lambda key: False,
    )
    assert result == {"booking_form": first_id}


def test_requirement_with_a_document_present_but_wrong_slot_does_not_satisfy_it() -> None:
    result = resolve_requirement_satisfaction(
        object(),
        tenant_id="tenant-a",
        journey_id=uuid4(),
        stage_code="BOOKING",
        requirements=[_requirement("customer_kyc"), _requirement("booking_form")],
        documents=[_document("booking_form")],
    )
    assert result["booking_form"].satisfied is True
    assert result["customer_kyc"].satisfied is False
    assert result["customer_kyc"].reason == "NO_DOCUMENT"
