from __future__ import annotations

import inspect
from uuid import uuid4

from audit_core.uc03_pc_generic_review import (
    DirectDocumentFieldReviewCommand,
    DirectExtractedField,
    _project_known_field,
    _store_fields,
    submit_direct_document_field_review,
)


def test_generic_review_field_has_no_decision_or_role() -> None:
    field = DirectExtractedField(
        fieldKey="future_di_field",
        sourceFactRef=uuid4(),
        sourceFactVersion=1,
        extractedValue="DI value",
        confidenceScore=0.91,
    )
    payload = field.model_dump()
    assert "decision" not in payload
    assert "reviewedByRole" not in payload
    assert payload["modifiedValue"] is None


def test_generic_review_accepts_unmapped_fields() -> None:
    command = DirectDocumentFieldReviewCommand(
        requirementRef=uuid4(),
        documentId=uuid4(),
        fields=[
            DirectExtractedField(
                fieldKey="brand_new_di_field",
                sourceFactRef=uuid4(),
                sourceFactVersion=4,
                extractedValue={"value": "anything"},
                confidenceScore=0.77,
            )
        ],
    )
    assert command.fields[0].fieldKey == "brand_new_di_field"


def test_generic_review_does_not_use_source_field_allowlist() -> None:
    source = inspect.getsource(submit_direct_document_field_review)
    assert "_SUPPORTED_PROPOSAL_FIELDS" not in source
    assert "Unsupported extraction field" not in source
    assert "_store_fields" in source


def test_unmapped_projection_is_a_noop_through_common_mapping() -> None:
    source = inspect.getsource(_project_known_field)
    assert "spec_for_field" in source
    assert "if spec is None" in source
    assert "return None" in source


def test_generic_review_stores_only_human_corrections_not_raw_di_values() -> None:
    source = inspect.getsource(_store_fields)
    assert "corrected_fields" in source
    assert "field.modifiedValue is not None" in source
    assert "extracted_value=NULL" in source
    assert "confidence_score=NULL" in source


def test_correction_persistence_precedes_best_effort_typed_projection() -> None:
    source = inspect.getsource(submit_direct_document_field_review)
    assert source.index("_store_fields(") < source.index("_project_known_field(")
    assert "with connection.begin_nested()" in source
    assert "projection_failure_count += 1" in source


def test_only_modified_fields_emit_correction_events() -> None:
    source = inspect.getsource(submit_direct_document_field_review)
    assert "if field.modifiedValue is not None" in source
    assert 'event_type="BOOKING_EXTRACTION_CORRECTED"' in source
    assert "BOOKING_EXTRACTION_APPROVED" not in source
    assert '"rawDiValuesCopied": False' in source
