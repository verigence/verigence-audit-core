from __future__ import annotations

import inspect
from uuid import uuid4

from fastapi.routing import APIRoute

from audit_core import uc03_document_review_v2 as review_v2
from audit_core.uc03_delivery_review_confirm import (
    DeliveryReviewV2ConfirmResponse,
    _lossless_delivery_fields,
    confirm_delivery_review_v2,
    install_uc03_delivery_review_confirm,
)


def _delivery_document(*, value="delivery-value") -> review_v2.ReviewV2Document:
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
                confidenceScore=97.0,
                sourceFactVersion=3,
                reviewState="READY",
            )
        ],
    )


def test_delivery_review_confirm_route_is_explicit_post_not_get_side_effect() -> None:
    install_uc03_delivery_review_confirm()
    routes = [
        route
        for route in review_v2.router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/delivery/review/confirm")
    ]

    assert len(routes) == 1
    assert routes[0].methods == {"POST"}
    assert routes[0].response_model is DeliveryReviewV2ConfirmResponse

    comparison_routes = [
        route
        for route in review_v2.router.routes
        if isinstance(route, APIRoute)
        and route.path.endswith("/audit/source-comparison")
    ]
    assert len(comparison_routes) == 1
    assert comparison_routes[0].methods == {"GET"}


def test_delivery_review_fields_keep_di_identity_and_effective_value() -> None:
    document = _delivery_document(value=False)
    fields = _lossless_delivery_fields([document])

    assert len(fields) == 1
    field = fields[0]
    assert field.document_id == document.documentId
    assert field.evidence_id is None
    assert field.source_canonical_field_id == document.fields[0].canonicalFieldId
    assert field.source_fact_version == 3
    assert field.field_key == "future_delivery_field"
    assert field.extracted_value is False
    assert field.effective_value is False
    assert field.confidence_score == 97.0
    assert field.confidence_scale == "PERCENT"
    assert field.is_modified is False


def test_delivery_confirm_persists_before_verified_state_transition() -> None:
    source = inspect.getsource(confirm_delivery_review_v2)

    assert source.index("persist_reviewed_di_fields(") < source.index(
        "SET pc_verification_status='VERIFIED'"
    )
    assert 'stage_code="DELIVERY"' in source
    assert 'event_type="PC_DELIVERY_REVIEW_CONFIRMED"' in source
    assert '"rawDiValuesCopied": True' in source
    assert "FOR UPDATE" in source
    assert "execute_idempotent_json_command(" in source
    assert "expected_version = _parse_if_match(if_match)" in source


def test_delivery_confirm_refuses_pending_or_failed_extraction() -> None:
    source = inspect.getsource(confirm_delivery_review_v2)

    assert 'document.extractionState == "PENDING"' in source
    assert 'document.extractionState == "FAILED"' in source
    assert 'title="Documents are not ready for review"' in source
    assert 'title="Document processing requires follow-up"' in source
