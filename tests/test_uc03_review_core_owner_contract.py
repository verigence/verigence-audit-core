from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from audit_core import uc03_booking_review_decisions as decisions
from audit_core import uc03_document_review_v2 as review_v2
from audit_core import uc03_v2_review_materialization as materialization
from audit_core.errors import ConflictError
from audit_core.uc03_booking_commercial_components import (
    install_uc03_booking_commercial_components,
)
from audit_core.uc03_booking_review_decisions import (
    BookingReviewV2ConfirmWithDecisionsResponse,
    _lossless_reviewed_fields,
    install_uc03_booking_review_decisions,
)
from audit_core.uc03_strict_review_core_ownership import (
    install_uc03_strict_review_core_ownership,
)


def _install_contract() -> None:
    install_uc03_booking_commercial_components()
    install_uc03_booking_review_decisions()
    install_uc03_strict_review_core_ownership()


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
    _install_contract()
    confirm_routes = [
        route
        for route in review_v2.router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/booking/review/confirm")
        and "POST" in route.methods
    ]

    assert len(confirm_routes) == 1
    assert confirm_routes[0].response_model is BookingReviewV2ConfirmWithDecisionsResponse


def test_every_supported_booking_source_field_has_typed_core_owner() -> None:
    _install_contract()
    document_id = uuid4()

    # Booking Form and Booking Docket are alternate evidence for the same Booking
    # business owner. Every configured Booking field must be typed for both.
    for document_type in ("booking_form", "booking_docket"):
        for field_key in materialization._BOOKING_FORM_FIELDS:
            assert materialization.reviewed_field_core_owner(
                document_type_key=document_type,
                field_key=field_key,
                document_id=document_id,
            ) is not None

    for field_key in materialization._PAN_FIELDS:
        assert materialization.reviewed_field_core_owner(
            document_type_key="pan",
            field_key=field_key,
            document_id=document_id,
        ) is not None
    for field_key in materialization._AADHAAR_FIELDS:
        assert materialization.reviewed_field_core_owner(
            document_type_key="aadhaar",
            field_key=field_key,
            document_id=document_id,
        ) is not None
    for field_key in materialization._RECEIPT_FIELDS:
        assert materialization.reviewed_field_core_owner(
            document_type_key="dealer_receipt",
            field_key=field_key,
            document_id=document_id,
        ) is not None


def test_booking_docket_unique_fields_are_first_class_review_attributes() -> None:
    _install_contract()
    for field_key in (
        "deal_type",
        "out_of_scope_reasons",
        "dsa_commission_amount",
    ):
        spec = review_v2.spec_for_field(field_key)
        assert spec is not None
        assert spec.mapping_status == "SUPPORTED"
        assert "BOOKING" in spec.stages


def test_unknown_accepted_booking_field_fails_before_generic_provenance_copy() -> None:
    _install_contract()
    document = _document("future_unowned_business_field")
    reviewed = _lossless_reviewed_fields([document], rejected_keys=set())

    assert len(reviewed) == 1
    assert reviewed[0].effective_value_is_set is True
    assert materialization.reviewed_field_core_owner(
        document_type_key="booking_form",
        field_key="future_unowned_business_field",
        document_id=document.documentId,
    ) is None

    with pytest.raises(ConflictError) as raised:
        decisions.persist_reviewed_di_fields(
            object(),
            tenant_id="tenant-1",
            journey_id=uuid4(),
            stage_code="BOOKING",
            actor_id="reviewer-1",
            fields=reviewed,
        )
    assert raised.value.error_code == "VAC-CONFLICT-013"


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


def test_booking_confirm_installs_strict_owner_guard() -> None:
    _install_contract()
    assert decisions.persist_reviewed_di_fields.__name__ == (
        "persist_reviewed_di_fields_with_owner_guard"
    )
