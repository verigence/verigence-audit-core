from __future__ import annotations

from uuid import uuid4

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.uc03_confidence_review_policy import (
    REVIEW_THRESHOLD_PERCENT,
    _build_raw_review_item,
    _field_review_state,
    requires_pc_review,
)


def _raw(*, value: str, confidence: float | None, document_label: str):
    return review_v2.ReviewV2UnmappedField(
        canonicalFieldId=str(uuid4()),
        fieldKey="future_business_field",
        value=value,
        confidenceScore=confidence,
        sourceFactVersion=1,
        documentId=uuid4(),
        documentTypeKey="booking_form",
        documentLabel=document_label,
        originalFilename=f"{document_label}.pdf",
        pageNo=1,
        evidenceRegion=None,
    )


def test_review_threshold_is_exactly_ninety_percent() -> None:
    assert REVIEW_THRESHOLD_PERCENT == 90.0
    assert requires_pc_review(89.99) is True
    assert requires_pc_review(90.0) is False
    assert requires_pc_review(100.0) is False
    assert requires_pc_review(None) is True


def test_field_review_state_depends_on_confidence_not_value_presence() -> None:
    assert _field_review_state(value=None, confidence_score=95.0) == "READY"
    assert _field_review_state(value="value", confidence_score=89.0) == "NEEDS_REVIEW"


def test_conflicting_high_confidence_sources_do_not_create_pc_review_work() -> None:
    item = _build_raw_review_item(
        "raw:future_business_field",
        [
            _raw(value="A", confidence=96.0, document_label="Document A"),
            _raw(value="B", confidence=94.0, document_label="Document B"),
        ],
    )
    assert item is not None
    assert item.decision_required is False


def test_any_low_confidence_source_creates_pc_review_work() -> None:
    item = _build_raw_review_item(
        "raw:future_business_field",
        [
            _raw(value="A", confidence=96.0, document_label="Document A"),
            _raw(value="B", confidence=88.0, document_label="Document B"),
        ],
    )
    assert item is not None
    assert item.decision_required is True
