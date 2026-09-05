"""Validate reviewed DI values against their typed Audit Core contracts.

DI deliberately preserves a raw machine value when normalization fails. Review must
therefore not mark a high-confidence value READY merely because OCR confidence is high
when the value cannot be persisted by its known typed Audit Core owner.

This module keeps the raw DI value visible and auditable, marks such values as
NEEDS_REVIEW for both Booking and Delivery, and fails Confirm before any persistence
when an invalid typed value is still accepted unchanged. The PC can then correct or
reject that value; no value is silently coerced or discarded.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from audit_core import uc03_booking_capture as booking_capture
from audit_core import uc03_booking_review_decisions as booking_review
from audit_core import uc03_delivery_review_confirm as delivery_review
from audit_core import uc03_document_review_v2 as review_v2
from audit_core import uc03_review_effective_values as effective_values
from audit_core import uc03_v2_review_materialization as materialization
from audit_core.errors import AuditCoreError, ConflictError
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_di_core_persistence import ReviewedDiField

_installed = False
_original_review_document: Callable[..., review_v2.ReviewV2Document] | None = None
_original_booking_persist: Callable[..., int] | None = None
_original_delivery_persist: Callable[..., int] | None = None
_original_effective_persist: Callable[..., int] | None = None


def _typed_kind(field_key: str) -> str | None:
    key = str(field_key).strip().lower()
    if (
        key in materialization._BOOKING_DECIMAL_FIELDS
        or key in materialization._RECEIPT_DECIMAL_FIELDS
    ):
        return "DECIMAL"
    if (
        key in materialization._BOOKING_DATE_FIELDS
        or key in materialization._RECEIPT_DATE_FIELDS
    ):
        return "DATE"
    if key in materialization._BOOKING_BOOLEAN_FIELDS:
        return "BOOLEAN"
    return None


def reviewed_value_is_typed_valid(field_key: str, value: Any) -> bool:
    """Return whether a populated reviewed value satisfies its known Core type.

    Fields without one of the established typed materialization contracts are not
    guessed here; their validation remains with their actual owner.
    """

    kind = _typed_kind(field_key)
    if kind is None or value is None or value == "":
        return True

    key = str(field_key).strip().upper()
    try:
        if kind == "DECIMAL":
            booking_capture._as_decimal(value, key)
        elif kind == "DATE":
            booking_capture._as_date(value, key)
        else:
            booking_capture._as_bool(value, key)
    except AuditCoreError:
        return False
    return True


def mark_typed_invalid_fields_for_review(
    document: review_v2.ReviewV2Document,
) -> review_v2.ReviewV2Document:
    """Keep raw DI evidence but require PC action for typed-invalid values."""

    for field in document.fields:
        if not reviewed_value_is_typed_valid(field.fieldKey, field.value):
            field.reviewState = "NEEDS_REVIEW"
    return document


def _review_document_with_typed_validation(
    *args: Any,
    **kwargs: Any,
) -> review_v2.ReviewV2Document:
    if _original_review_document is None:
        raise RuntimeError("UC03 typed Review validation installer is not initialized")
    return mark_typed_invalid_fields_for_review(_original_review_document(*args, **kwargs))


def _field_label(field_key: str) -> str:
    spec = spec_for_field(field_key)
    if spec is not None:
        return spec.label
    return str(field_key).replace("_", " ").strip().title()


def validate_reviewed_field_types(fields: list[ReviewedDiField]) -> None:
    """Fail before writes if an accepted effective value violates its known type."""

    for field in fields:
        if not field.effective_value_is_set:
            # Rejected DI values remain available as provenance but are not effective
            # business values and therefore do not require typed materialization.
            continue
        if reviewed_value_is_typed_valid(field.field_key, field.effective_value):
            continue
        label = _field_label(field.field_key)
        raise ConflictError(
            error_code="VAC-CONFLICT-012",
            title="Review correction is required",
            detail=(
                f"{label} cannot be stored as its required Audit Core value type. "
                "Correct the extracted value or Reject it before confirming Review."
            ),
        )


def _validated_booking_persist(*args: Any, **kwargs: Any) -> int:
    if _original_booking_persist is None:
        raise RuntimeError("UC03 typed Review validation installer is not initialized")
    validate_reviewed_field_types(list(kwargs.get("fields") or []))
    return _original_booking_persist(*args, **kwargs)


def _validated_delivery_persist(*args: Any, **kwargs: Any) -> int:
    if _original_delivery_persist is None:
        raise RuntimeError("UC03 typed Review validation installer is not initialized")
    validate_reviewed_field_types(list(kwargs.get("fields") or []))
    return _original_delivery_persist(*args, **kwargs)


def _validated_effective_persist(*args: Any, **kwargs: Any) -> int:
    if _original_effective_persist is None:
        raise RuntimeError("UC03 typed Review validation installer is not initialized")
    validate_reviewed_field_types(list(kwargs.get("fields") or []))
    return _original_effective_persist(*args, **kwargs)


def install_uc03_review_typed_value_validation() -> None:
    """Install shared Review read validation and pre-persistence type validation."""

    global _installed
    global _original_review_document
    global _original_booking_persist, _original_delivery_persist, _original_effective_persist
    if _installed:
        return

    _original_review_document = review_v2._review_document
    review_v2._review_document = _review_document_with_typed_validation  # type: ignore[assignment]

    # These modules keep direct bindings to the persistence function. Patch every
    # active Booking/Delivery Confirm path so validation occurs before any writes.
    _original_booking_persist = booking_review.persist_reviewed_di_fields
    booking_review.persist_reviewed_di_fields = _validated_booking_persist  # type: ignore[assignment]

    _original_delivery_persist = delivery_review.persist_reviewed_di_fields
    delivery_review.persist_reviewed_di_fields = _validated_delivery_persist  # type: ignore[assignment]

    _original_effective_persist = effective_values.persist_reviewed_di_fields
    effective_values.persist_reviewed_di_fields = _validated_effective_persist  # type: ignore[assignment]

    _installed = True
