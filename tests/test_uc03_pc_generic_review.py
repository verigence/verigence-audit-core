from __future__ import annotations

import inspect
from uuid import uuid4

from audit_core import uc03_pc_generic_review as generic_review
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


def test_generic_review_forwards_all_fields_to_lossless_persistence(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_persist(connection, **kwargs):
        captured["connection"] = connection
        captured.update(kwargs)
        return 2

    monkeypatch.setattr(generic_review, "persist_reviewed_di_fields", fake_persist)
    connection = object()
    document_id = uuid4()
    evidence_id = uuid4()
    journey_id = uuid4()
    unchanged = DirectExtractedField(
        fieldKey="future_di_field",
        sourceFactRef=uuid4(),
        sourceFactVersion=1,
        extractedValue="DI value",
        confidenceScore=0.91,
    )
    corrected = DirectExtractedField(
        fieldKey="customer_name",
        sourceFactRef=uuid4(),
        sourceFactVersion=2,
        extractedValue="Wrong Name",
        modifiedValue="Correct Name",
        confidenceScore=0.73,
    )

    stored = _store_fields(
        connection,
        tenant_id="tenant-a",
        journey_id=journey_id,
        evidence_id=evidence_id,
        document_id=document_id,
        document_type_key="booking_form",
        actor_id="pc-user",
        fields=[unchanged, corrected],
    )

    assert stored == 2
    assert captured["connection"] is connection
    assert captured["stage_code"] == "BOOKING"
    assert captured["actor_id"] == "pc-user"
    reviewed = captured["fields"]
    assert isinstance(reviewed, list) and len(reviewed) == 2

    unchanged_row = reviewed[0]
    assert unchanged_row.extracted_value == "DI value"
    assert unchanged_row.effective_value == "DI value"
    assert unchanged_row.modified_value is None
    assert unchanged_row.is_modified is False
    assert unchanged_row.confidence_scale == "UNIT_INTERVAL"
    assert unchanged_row.source_document_type_key == "booking_form"

    corrected_row = reviewed[1]
    assert corrected_row.extracted_value == "Wrong Name"
    assert corrected_row.modified_value == "Correct Name"
    assert corrected_row.effective_value == "Correct Name"
    assert corrected_row.is_modified is True


def test_lossless_persistence_precedes_best_effort_typed_projection() -> None:
    source = inspect.getsource(submit_direct_document_field_review)
    assert source.index("_store_fields(") < source.index("_project_known_field(")
    assert "with connection.begin_nested()" in source
    assert "projection_failure_count += 1" in source
    assert '"storedFieldCount": stored_count' in source


def test_only_modified_fields_emit_correction_events() -> None:
    source = inspect.getsource(submit_direct_document_field_review)
    assert "if field.modifiedValue is not None" in source
    assert 'event_type="BOOKING_EXTRACTION_CORRECTED"' in source
    assert "BOOKING_EXTRACTION_APPROVED" not in source
    assert '"rawDiValuesCopied": True' in source
