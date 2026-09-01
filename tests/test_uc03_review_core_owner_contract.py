import inspect
from uuid import uuid4

from fastapi.routing import APIRoute

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.uc03_booking_review_decisions import (
    BookingReviewV2ConfirmWithDecisionsResponse,
    _lossless_reviewed_fields,
    confirm_booking_review_v2_with_decisions,
    install_uc03_booking_review_decisions,
)
from audit_core.uc03_v2_review_materialization import (
    _AADHAAR_FIELDS,
    _BOOKING_FORM_FIELDS,
    _PAN_FIELDS,
    _RECEIPT_FIELDS,
    reviewed_field_core_owner,
)


def _document(
    field_key: str,
    *,
    value="accepted-value",
    document_type: str = "booking_form",
) -> review_v2.ReviewV2Document:
    return review_v2.ReviewV2Document(
        documentId=uuid4(),
        evidenceId=uuid4(),
        requirementKey="booking_form",
        label=document_type,
        documentTypeKey=document_type,
        originalFilename="source.pdf",
        processingStatus="COMPLETED",
        extractionState="READY",
        fields=[
            review_v2.ReviewV2Field(
                canonicalFieldId=str(uuid4()),
                fieldKey=field_key,
                value=value,
                confidenceScore=99.0,
                sourceFactVersion=1,
                reviewState="READY",
            )
        ],
    )


def test_active_review_confirm_contract_is_decision_aware() -> None:
    install_uc03_booking_review_decisions()
    confirm_routes = [
        route
        for route in review_v2.router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/booking/review/confirm")
        and "POST" in route.methods
    ]

    assert len(confirm_routes) == 1
    assert confirm_routes[0].response_model is BookingReviewV2ConfirmWithDecisionsResponse


def test_every_supported_review_field_has_typed_core_owner() -> None:
    document_id = uuid4()

    for field_key in _BOOKING_FORM_FIELDS:
        assert reviewed_field_core_owner(
            document_type_key="booking_form",
            field_key=field_key,
            document_id=document_id,
        ) is not None
    for field_key in _PAN_FIELDS:
        assert reviewed_field_core_owner(
            document_type_key="pan",
            field_key=field_key,
            document_id=document_id,
        ) is not None
    for field_key in _AADHAAR_FIELDS:
        assert reviewed_field_core_owner(
            document_type_key="aadhaar",
            field_key=field_key,
            document_id=document_id,
        ) is not None
    for field_key in _RECEIPT_FIELDS:
        assert reviewed_field_core_owner(
            document_type_key="dealer_receipt",
            field_key=field_key,
            document_id=document_id,
        ) is not None


def test_unknown_review_field_has_no_typed_core_owner_but_is_generically_persistable() -> None:
    document = _document("future_unowned_business_field")
    document_id = document.documentId

    assert reviewed_field_core_owner(
        document_type_key="booking_form",
        field_key="future_unowned_business_field",
        document_id=document_id,
    ) is None

    reviewed = _lossless_reviewed_fields([document], rejected_keys=set())
    assert len(reviewed) == 1
    field = reviewed[0]
    assert field.document_id == document_id
    assert field.field_key == "future_unowned_business_field"
    assert field.extracted_value == "accepted-value"
    assert field.effective_value == "accepted-value"
    assert field.effective_value_is_set is True
    assert field.confidence_scale == "PERCENT"


def test_rejected_unknown_field_keeps_original_without_effective_value() -> None:
    document = _document("future_unowned_business_field")

    reviewed = _lossless_reviewed_fields(
        [document],
        rejected_keys={"raw:future_unowned_business_field"},
    )

    assert len(reviewed) == 1
    field = reviewed[0]
    assert field.extracted_value == "accepted-value"
    assert field.effective_value_is_set is False


def test_rejected_mapped_attribute_keeps_source_without_effective_value() -> None:
    document = _document("customer_name")

    reviewed = _lossless_reviewed_fields(
        [document],
        rejected_keys={"attribute:customer_name"},
    )

    assert len(reviewed) == 1
    assert reviewed[0].extracted_value == "accepted-value"
    assert reviewed[0].effective_value_is_set is False


def test_booking_confirm_no_longer_requires_typed_owner_for_every_accepted_field() -> None:
    source = inspect.getsource(confirm_booking_review_v2_with_decisions)
    assert "_assert_accepted_raw_fields_have_core_owner" not in source
    assert "persist_reviewed_di_fields(" in source
    assert 'stage_code="BOOKING"' in source
    assert '"rawDiValuesCopied": True' in source
    assert "if typed_owner is not None" in source
