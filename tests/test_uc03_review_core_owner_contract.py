from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.errors import ConflictError
from audit_core.uc03_booking_review_decisions import (
    BookingReviewV2ConfirmWithDecisionsResponse,
    _assert_accepted_raw_fields_have_core_owner,
    install_uc03_booking_review_decisions,
)
from audit_core.uc03_v2_review_materialization import (
    _AADHAAR_FIELDS,
    _BOOKING_FORM_FIELDS,
    _PAN_FIELDS,
    _RECEIPT_FIELDS,
    reviewed_field_core_owner,
)


def _unmapped(field_key: str, *, document_type: str = "booking_form"):
    return review_v2.ReviewV2UnmappedField(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value="accepted-value",
        confidenceScore=99.0,
        sourceFactVersion=1,
        documentId=uuid4(),
        documentTypeKey=document_type,
        documentLabel=document_type,
        originalFilename="source.pdf",
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


def test_unknown_review_field_has_no_core_owner() -> None:
    assert reviewed_field_core_owner(
        document_type_key="booking_form",
        field_key="future_unowned_business_field",
        document_id=uuid4(),
    ) is None


def test_accepted_unmapped_field_without_core_owner_blocks_confirm() -> None:
    with pytest.raises(ConflictError):
        _assert_accepted_raw_fields_have_core_owner(
            [_unmapped("future_unowned_business_field")],
            rejected_keys=set(),
        )


def test_rejected_unmapped_field_does_not_require_core_owner() -> None:
    _assert_accepted_raw_fields_have_core_owner(
        [_unmapped("future_unowned_business_field")],
        rejected_keys={"raw:future_unowned_business_field"},
    )
