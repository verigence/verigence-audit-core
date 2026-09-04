from __future__ import annotations

from uuid import uuid4

import pytest

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.errors import ConflictError
from audit_core.uc03_review_effective_values import (
    ReviewFieldCorrection,
    _correction_map,
    _general_raw_review_items,
    _reviewed_fields,
)


def _document(
    *,
    document_id=None,
    document_type="future_invoice",
    field_key="future_field",
    value="DI value",
    version=1,
):
    document_id = document_id or uuid4()
    return review_v2.ReviewV2Document(
        documentId=document_id,
        evidenceId=uuid4(),
        requirementKey="future_requirement",
        label="Future document",
        documentTypeKey=document_type,
        originalFilename=f"{document_id}.pdf",
        processingStatus="COMPLETED",
        extractionState="READY",
        fields=[
            review_v2.ReviewV2Field(
                canonicalFieldId=str(uuid4()),
                fieldKey=field_key,
                value=value,
                confidenceScore=98.0,
                sourceFactVersion=version,
                reviewState="READY",
            )
        ],
    )


def _correction(document, *, value):
    field = document.fields[0]
    return ReviewFieldCorrection(
        documentId=document.documentId,
        canonicalFieldId=field.canonicalFieldId,
        fieldKey=field.fieldKey,
        sourceFactVersion=field.sourceFactVersion,
        effectiveValue=value,
    )


def test_pc_correction_keeps_original_di_value_and_changes_only_effective_value() -> None:
    changed = _document(field_key="future_changed", value="DI original")
    unchanged = _document(field_key="future_unchanged", value=False)
    corrections = _correction_map(
        [changed, unchanged],
        [_correction(changed, value="PC confirmed")],
    )

    fields = _reviewed_fields([changed, unchanged], corrections)
    by_key = {field.field_key: field for field in fields}

    changed_field = by_key["future_changed"]
    assert changed_field.extracted_value == "DI original"
    assert changed_field.modified_value == "PC confirmed"
    assert changed_field.effective_value == "PC confirmed"
    assert changed_field.is_modified is True

    unchanged_field = by_key["future_unchanged"]
    assert unchanged_field.extracted_value is False
    assert unchanged_field.modified_value is None
    assert unchanged_field.effective_value is False
    assert unchanged_field.is_modified is False


def test_n_extracted_m_modified_contract_is_lossless() -> None:
    documents = [
        _document(field_key="field_a", value="A"),
        _document(field_key="field_b", value=0),
        _document(field_key="field_c", value=False),
    ]
    corrections = _correction_map(
        documents,
        [_correction(documents[1], value=125)],
    )

    reviewed = _reviewed_fields(documents, corrections)

    assert len(reviewed) == 3
    assert sum(field.is_modified for field in reviewed) == 1
    by_key = {field.field_key: field for field in reviewed}
    assert by_key["field_a"].extracted_value == "A"
    assert by_key["field_a"].effective_value == "A"
    assert by_key["field_b"].extracted_value == 0
    assert by_key["field_b"].modified_value == 125
    assert by_key["field_b"].effective_value == 125
    assert by_key["field_c"].extracted_value is False
    assert by_key["field_c"].effective_value is False
    assert all(field.source_canonical_field_id for field in reviewed)
    assert all(field.source_fact_version == 1 for field in reviewed)


def test_correction_must_match_current_document_canonical_field_and_fact_version() -> None:
    document = _document(field_key="future_field", value="DI")
    field = document.fields[0]
    stale = ReviewFieldCorrection(
        documentId=document.documentId,
        canonicalFieldId=field.canonicalFieldId,
        fieldKey=field.fieldKey,
        sourceFactVersion=field.sourceFactVersion + 1,
        effectiveValue="PC",
    )

    with pytest.raises(ConflictError):
        _correction_map([document], [stale])


def test_duplicate_correction_for_same_source_fact_is_rejected() -> None:
    document = _document()
    first = _correction(document, value="first")
    second = _correction(document, value="second")

    with pytest.raises(ConflictError):
        _correction_map([document], [first, second])


def _unmapped(document_id, *, field_key="invoice_number", value="INV-1"):
    return review_v2.ReviewV2UnmappedField(
        canonicalFieldId=str(uuid4()),
        fieldKey=field_key,
        value=value,
        confidenceScore=99.0,
        sourceFactVersion=1,
        documentId=document_id,
        documentTypeKey="future_invoice",
        documentLabel="Future invoice",
        originalFilename=f"{document_id}.pdf",
    )


def test_same_raw_field_in_multiple_documents_has_distinct_review_identity() -> None:
    first_document = uuid4()
    second_document = uuid4()

    items = _general_raw_review_items(
        [
            _unmapped(first_document, value="INV-1"),
            _unmapped(second_document, value="INV-2"),
        ]
    )

    keys = {item.review_key for item in items}
    assert keys == {
        f"raw:{first_document}:invoice_number",
        f"raw:{second_document}:invoice_number",
    }
    assert all(item.decision_required is False for item in items)


def test_single_raw_field_keeps_legacy_review_key_for_compatibility() -> None:
    document_id = uuid4()

    items = _general_raw_review_items([_unmapped(document_id)])

    assert len(items) == 1
    assert items[0].review_key == "raw:invoice_number"
