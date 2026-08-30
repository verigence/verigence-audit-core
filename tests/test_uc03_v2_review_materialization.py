from uuid import uuid4

from audit_core.uc03_document_review_v2 import (
    ReviewV2Document,
    ReviewV2Field,
    ReviewV2UnmappedField,
)
from audit_core.uc03_v2_review_materialization import (
    _reviewed_receipt_values,
    receipt_review_key,
)
from audit_core.uc03_v2_review_materialization_install import (
    _receipt_raw_review_items,
)


def _field(field_key: str, value, *, confidence: float = 99.0) -> ReviewV2Field:
    return ReviewV2Field(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=1,
        reviewState="READY" if confidence >= 92.0 else "NEEDS_REVIEW",
    )


def _receipt(document_id, *, amount: str, confidence: float = 99.0) -> ReviewV2Document:
    return ReviewV2Document(
        documentId=document_id,
        label="Dealer Receipt",
        documentTypeKey="dealer_receipt",
        originalFilename=f"{document_id}.pdf",
        processingStatus="PROCESSED",
        extractionState="READY",
        fields=[
            _field("receipt_number", f"R-{str(document_id)[:6]}"),
            _field("receipt_date", "2026-08-30"),
            _field("amount_paid", amount, confidence=confidence),
            _field("payment_mode", "UPI"),
            _field("payment_reference_no", "UTR-123"),
        ],
    )


def test_receipt_review_key_is_document_scoped() -> None:
    first = uuid4()
    second = uuid4()

    assert receipt_review_key(first, "amount_paid") != receipt_review_key(
        second, "amount_paid"
    )


def test_two_receipts_with_different_amounts_are_not_cross_source_mismatch() -> None:
    first = uuid4()
    second = uuid4()
    fields = [
        ReviewV2UnmappedField(
            canonicalFieldId=str(uuid4()),
            fieldKey="amount_paid",
            value="20000",
            confidenceScore=99.0,
            sourceFactVersion=1,
            documentId=first,
            documentTypeKey="dealer_receipt",
            documentLabel="Receipt 1",
            originalFilename="r1.pdf",
        ),
        ReviewV2UnmappedField(
            canonicalFieldId=str(uuid4()),
            fieldKey="amount_paid",
            value="30000",
            confidenceScore=99.0,
            sourceFactVersion=1,
            documentId=second,
            documentTypeKey="dealer_receipt",
            documentLabel="Receipt 2",
            originalFilename="r2.pdf",
        ),
    ]

    items = _receipt_raw_review_items(fields)

    assert len(items) == 2
    assert {item.review_key for item in items} == {
        receipt_review_key(first, "amount_paid"),
        receipt_review_key(second, "amount_paid"),
    }
    assert all(item.decision_required is False for item in items)


def test_low_confidence_receipt_field_requires_only_its_own_decision() -> None:
    first = uuid4()
    second = uuid4()
    fields = [
        ReviewV2UnmappedField(
            canonicalFieldId=str(uuid4()),
            fieldKey="amount_paid",
            value="20000",
            confidenceScore=80.0,
            sourceFactVersion=1,
            documentId=first,
            documentTypeKey="dealer_receipt",
            documentLabel="Receipt 1",
            originalFilename="r1.pdf",
        ),
        ReviewV2UnmappedField(
            canonicalFieldId=str(uuid4()),
            fieldKey="amount_paid",
            value="30000",
            confidenceScore=99.0,
            sourceFactVersion=1,
            documentId=second,
            documentTypeKey="dealer_receipt",
            documentLabel="Receipt 2",
            originalFilename="r2.pdf",
        ),
    ]

    items = {item.review_key: item for item in _receipt_raw_review_items(fields)}

    assert items[receipt_review_key(first, "amount_paid")].decision_required is True
    assert items[receipt_review_key(second, "amount_paid")].decision_required is False


def test_rejected_receipt_amount_is_not_materialized_as_zero_payment() -> None:
    document_id = uuid4()
    document = _receipt(document_id, amount="50000")

    values = _reviewed_receipt_values(
        document,
        rejected_review_keys={receipt_review_key(document_id, "amount_paid")},
    )

    assert "amount" not in values
    assert values["receipt_number"] is not None


def test_reviewed_receipt_fields_are_collected_once_in_memory() -> None:
    document_id = uuid4()
    document = _receipt(document_id, amount="50000")

    values = _reviewed_receipt_values(document, rejected_review_keys=set())

    assert str(values["amount"]) == "50000"
    assert str(values["receipt_date"]) == "2026-08-30"
    assert values["payment_method_code"] == "UPI"
    assert values["payment_reference"] == "UTR-123"
