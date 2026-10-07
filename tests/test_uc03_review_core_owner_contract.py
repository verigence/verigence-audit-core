from uuid import uuid4

from audit_core import uc03_document_review_v2 as review_v2
from audit_core import uc03_v2_review_materialization as materialization
from audit_core.uc03_booking_commercial_components import (
    install_uc03_booking_commercial_components,
)
from audit_core.uc03_booking_review_decisions import (
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
