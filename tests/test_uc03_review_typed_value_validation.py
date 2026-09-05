from uuid import uuid4

import pytest

from audit_core.errors import ConflictError
from audit_core.uc03_di_core_persistence import ReviewedDiField
from audit_core.uc03_review_typed_value_validation import (
    reviewed_value_is_typed_valid,
    validate_reviewed_field_types,
)


def _field(
    field_key: str,
    value,
    *,
    effective_value_is_set: bool = True,
) -> ReviewedDiField:
    return ReviewedDiField(
        document_id=uuid4(),
        field_key=field_key,
        source_fact_version=1,
        extracted_value=value,
        effective_value=value,
        source_canonical_field_id=str(uuid4()),
        source_document_type_key="booking_form",
        confidence_score=99.0,
        confidence_scale="PERCENT",
        effective_value_is_set=effective_value_is_set,
    )


def test_exchange_value_requires_numeric_effective_value() -> None:
    assert reviewed_value_is_typed_valid("exchange_value", "125000.00") is True
    assert reviewed_value_is_typed_valid("exchange_value", 125000) is True
    assert reviewed_value_is_typed_valid("exchange_value", "Not Applicable") is False


def test_known_date_and_boolean_types_use_core_parsers() -> None:
    assert reviewed_value_is_typed_valid("booking_date", "2026-09-05") is True
    assert reviewed_value_is_typed_valid("booking_date", "05/09/2026") is False
    assert reviewed_value_is_typed_valid("exchange_applicable", "Yes") is True
    assert reviewed_value_is_typed_valid("exchange_applicable", "Unknown") is False


def test_invalid_accepted_typed_value_fails_before_persistence() -> None:
    with pytest.raises(ConflictError) as caught:
        validate_reviewed_field_types([_field("exchange_value", "Not Applicable")])

    assert caught.value.error_code == "VAC-CONFLICT-012"
    assert "Exchange Value" in caught.value.detail
    assert "Correct" in caught.value.detail
    assert "Reject" in caught.value.detail


def test_rejected_invalid_typed_value_keeps_provenance_without_blocking() -> None:
    validate_reviewed_field_types(
        [
            _field(
                "exchange_value",
                "Not Applicable",
                effective_value_is_set=False,
            )
        ]
    )


def test_unknown_field_is_not_guessed_as_typed() -> None:
    assert reviewed_value_is_typed_valid("future_di_field", "anything") is True
