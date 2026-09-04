from uuid import uuid4

from audit_core.uc03_booking_review_decisions import (
    _current_review_items,
    _source_set_ref,
)
from audit_core.uc03_document_review_v2 import (
    ReviewV2Attribute,
    ReviewV2SourceValue,
    ReviewV2UnmappedField,
)


def _source(
    *,
    field_key: str,
    value: str,
    confidence: float,
    version: int = 1,
) -> ReviewV2SourceValue:
    return ReviewV2SourceValue(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=version,
        reviewState="READY" if confidence >= 92 else "NEEDS_REVIEW",
        documentId=uuid4(),
        documentLabel="Test Document",
        originalFilename="test.pdf",
    )


def test_mapped_needs_review_creates_required_decision_item() -> None:
    source = _source(field_key="customer_name", value="A B", confidence=91)
    attribute = ReviewV2Attribute(
        attributeKey="customer_name",
        label="Customer Name",
        mappingStatus="SUPPORTED",
        resolvedValue="A B",
        confidenceScore=91,
        reviewState="NEEDS_REVIEW",
        comparisonState="SINGLE_SOURCE",
        resolvedSource=source,
        sources=[source],
    )

    items = _current_review_items([attribute], [])

    assert items["attribute:customer_name"].decision_required is True


def test_same_raw_field_across_documents_is_not_a_false_mismatch() -> None:
    document_a = uuid4()
    document_b = uuid4()
    fields = [
        ReviewV2UnmappedField(
            canonicalFieldId=str(uuid4()),
            fieldKey="dealer_name",
            value="Dealer A",
            confidenceScore=98,
            sourceFactVersion=1,
            documentId=document_a,
            documentLabel="Booking Form",
            originalFilename="booking.pdf",
        ),
        ReviewV2UnmappedField(
            canonicalFieldId=str(uuid4()),
            fieldKey="dealer_name",
            value="Dealer B",
            confidenceScore=99,
            sourceFactVersion=1,
            documentId=document_b,
            documentLabel="Receipt",
            originalFilename="receipt.pdf",
        ),
    ]

    items = _current_review_items([], fields)

    assert set(items) == {
        f"raw:{document_a}:dealer_name",
        f"raw:{document_b}:dealer_name",
    }
    assert all(item.decision_required is False for item in items.values())


def test_source_set_ref_changes_when_any_source_fact_version_changes() -> None:
    first = _source(field_key="customer_name", value="A B", confidence=98, version=1)
    second = _source(field_key="pan_name", value="A B", confidence=99, version=1)
    before = _source_set_ref([first, second])
    second.sourceFactVersion = 2

    assert _source_set_ref([first, second]) != before
