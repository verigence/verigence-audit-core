from __future__ import annotations

from uuid import uuid4

from fastapi.routing import APIRoute

from audit_core import uc03_document_review_v2 as review_v2
from audit_core import uc03_review_effective_values as effective_values
from audit_core.uc03_delivery_review_confirm import DeliveryReviewV2ConfirmResponse


def _delivery_document(*, value="delivery-value", confidence=97.0) -> review_v2.ReviewV2Document:
    return review_v2.ReviewV2Document(
        documentId=uuid4(),
        evidenceId=None,
        requirementKey="customer_invoice",
        label="Customer Invoice",
        documentTypeKey="customer_invoice_dms",
        originalFilename="invoice.pdf",
        processingStatus="COMPLETED",
        extractionState="READY",
        fields=[
            review_v2.ReviewV2Field(
                canonicalFieldId=str(uuid4()),
                fieldKey="future_delivery_field",
                value=value,
                confidenceScore=confidence,
                sourceFactVersion=3,
                reviewState="READY",
            )
        ],
    )


def test_delivery_review_confirm_route_is_a_single_registration() -> None:
    # uc03_delivery_review_confirm.py's own confirm_delivery_review_v2 and
    # install_uc03_delivery_review_confirm were removed: install order in the
    # app-startup cascade meant install_uc03_review_effective_values() always
    # ran after it and discarded its route in favor of
    # confirm_delivery_review_v2_effective_values -- the dead handler had its
    # own passing "route exists" test despite never winning a single real
    # request. That function is now the only registration for this path,
    # decorated directly at its definition instead of installed at runtime.
    routes = [
        route
        for route in review_v2.router.routes
        if isinstance(route, APIRoute) and route.path.endswith("/delivery/review/confirm")
    ]
    assert len(routes) == 1
    assert routes[0].methods == {"POST"}
    assert routes[0].response_model is DeliveryReviewV2ConfirmResponse
    assert routes[0].endpoint is effective_values.confirm_delivery_review_v2_effective_values


def test_low_confidence_field_without_a_correction_is_unresolved() -> None:
    document = _delivery_document(confidence=85.0)
    unresolved = effective_values._unresolved_low_confidence_fields(
        [document], corrections={}
    )
    assert unresolved == [f"future_delivery_field@{document.documentId}"]


def test_low_confidence_field_with_any_correction_is_resolved() -> None:
    # Delivery has no separate Accept/Reject decision table -- resubmitting
    # the same extracted value as a "correction" is how a PC accepts a
    # low-confidence value as-is.
    document = _delivery_document(confidence=85.0)
    field = document.fields[0]
    corrections = effective_values._correction_map(
        [document],
        [
            effective_values.ReviewFieldCorrection(
                documentId=document.documentId,
                canonicalFieldId=field.canonicalFieldId,
                fieldKey=field.fieldKey,
                sourceFactVersion=field.sourceFactVersion,
                effectiveValue=field.value,
            )
        ],
    )
    assert effective_values._unresolved_low_confidence_fields([document], corrections) == []


def test_high_confidence_field_needs_no_correction() -> None:
    document = _delivery_document(confidence=97.0)
    assert effective_values._unresolved_low_confidence_fields([document], corrections={}) == []


def test_unpopulated_low_confidence_field_needs_no_correction() -> None:
    # Nothing was extracted for this field on this document -- there is
    # nothing for a PC to review.
    document = _delivery_document(value=None, confidence=None)
    assert effective_values._unresolved_low_confidence_fields([document], corrections={}) == []
