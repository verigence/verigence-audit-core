from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

from audit_core import uc03_booking_review_decisions as booking_review
from audit_core import uc03_delivery_review_confirm as delivery_review
from audit_core import uc03_document_review_v2 as review_v2
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.errors import ConflictError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_booking_commands import _aggregate_lock, _parse_if_match
from audit_core.uc03_delivery_commands import _append_delivery_event
from audit_core.uc03_delivery_review_materialization import (
    materialize_reviewed_delivery_business_values,
)
from audit_core.uc03_di_core_persistence import (
    ReviewedDiField,
    persist_reviewed_di_fields,
)
from audit_core.uc03_document_registry import is_receipt_document_type
from audit_core.uc03_review_confidence import has_value, requires_pc_review
from audit_core.uc03_v2_review_materialization import (
    receipt_document_ordinals,
    receipt_review_key,
    reviewed_field_core_owner,
)


class ReviewFieldCorrection(BaseModel):
    documentId: UUID
    canonicalFieldId: str = Field(min_length=1, max_length=160)
    fieldKey: str = Field(min_length=1, max_length=160)
    sourceFactVersion: int = Field(gt=0)
    effectiveValue: Any | None = Field(...)


class ReviewConfirmCommand(BaseModel):
    corrections: list[ReviewFieldCorrection] = Field(default_factory=list)


def _target(
    document_id: UUID,
    canonical_field_id: str,
    field_key: str,
    source_fact_version: int,
) -> tuple[UUID, str, str, int]:
    return (
        document_id,
        canonical_field_id.strip(),
        field_key.strip(),
        int(source_fact_version),
    )


def _field_target(
    document: review_v2.ReviewV2Document,
    field: review_v2.ReviewV2Field,
) -> tuple[UUID, str, str, int]:
    return _target(
        document.documentId,
        field.canonicalFieldId,
        field.fieldKey,
        field.sourceFactVersion,
    )


def _source_target(source: review_v2.ReviewV2SourceValue) -> tuple[UUID, str, str, int]:
    return _target(
        source.documentId,
        source.canonicalFieldId,
        source.fieldKey,
        source.sourceFactVersion,
    )


def _correction_map(
    documents: list[review_v2.ReviewV2Document],
    corrections: list[ReviewFieldCorrection],
) -> dict[tuple[UUID, str, str, int], ReviewFieldCorrection]:
    available = {
        _field_target(document, field): (document, field)
        for document in documents
        for field in document.fields
    }
    result: dict[tuple[UUID, str, str, int], ReviewFieldCorrection] = {}
    for correction in corrections:
        key = _target(
            correction.documentId,
            correction.canonicalFieldId,
            correction.fieldKey,
            correction.sourceFactVersion,
        )
        if key in result:
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Duplicate Review correction",
                detail=(
                    "The same DI source fact was corrected more than once in this "
                    "Review Confirm request."
                ),
            )
        current = available.get(key)
        if current is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Review correction is stale",
                detail=(
                    f"DI field '{correction.fieldKey}' no longer matches document/fact "
                    "identity loaded by Review. Refresh Review and try again."
                ),
            )
        document, field = current
        typed_owner = reviewed_field_core_owner(
            document_type_key=document.documentTypeKey,
            field_key=field.fieldKey,
            document_id=document.documentId,
        )
        value = correction.effectiveValue
        if typed_owner is not None and (
            value is None or (isinstance(value, str) and not value.strip())
        ):
            raise ConflictError(
                error_code="VAC-CONFLICT-013",
                title="Reviewed value cannot be projected",
                detail=(
                    f"DI field '{field.fieldKey}' has a typed Audit Core owner and "
                    "cannot be confirmed with an empty effective value."
                ),
            )
        result[key] = correction
    return result


def _validate_mapped_corrections(
    attributes: list[review_v2.ReviewV2Attribute],
    corrections: dict[tuple[UUID, str, str, int], ReviewFieldCorrection],
) -> None:
    for attribute in attributes:
        source = attribute.resolvedSource
        if source is None:
            continue
        correction = corrections.get(_source_target(source))
        if correction is None:
            continue
        value = correction.effectiveValue
        if value is None or (isinstance(value, str) and not value.strip()):
            raise ConflictError(
                error_code="VAC-CONFLICT-013",
                title="Reviewed value cannot be projected",
                detail=(
                    f"Mapped attribute '{attribute.attributeKey}' cannot be confirmed "
                    "with an empty effective value because its Audit Core projection "
                    "would be lost."
                ),
            )


def _duplicate_raw_field_keys(
    documents: list[review_v2.ReviewV2Document],
) -> set[str]:
    document_ids: dict[str, set[UUID]] = {}
    for document in documents:
        if is_receipt_document_type(document.documentTypeKey):
            continue
        for field in document.fields:
            if review_v2.spec_for_field(field.fieldKey) is not None:
                continue
            key = field.fieldKey.strip()
            document_ids.setdefault(key, set()).add(document.documentId)
    return {key for key, ids in document_ids.items() if len(ids) > 1}


def _general_raw_review_items(
    unmapped: list[review_v2.ReviewV2UnmappedField],
) -> list[Any]:
    grouped: dict[str, list[review_v2.ReviewV2UnmappedField]] = {}
    document_grouped: dict[
        tuple[UUID, str], list[review_v2.ReviewV2UnmappedField]
    ] = {}
    receipt_grouped: dict[
        tuple[UUID, str], list[review_v2.ReviewV2UnmappedField]
    ] = {}
    receipt_document_ids: list[UUID] = []

    documents_by_field: dict[str, set[UUID]] = {}
    for field in unmapped:
        if is_receipt_document_type(field.documentTypeKey):
            continue
        documents_by_field.setdefault(field.fieldKey, set()).add(field.documentId)
    duplicate_keys = {
        field_key
        for field_key, document_ids in documents_by_field.items()
        if len(document_ids) > 1
    }

    for field in unmapped:
        if is_receipt_document_type(field.documentTypeKey):
            receipt_grouped.setdefault((field.documentId, field.fieldKey), []).append(field)
            receipt_document_ids.append(field.documentId)
        elif field.fieldKey in duplicate_keys:
            document_grouped.setdefault((field.documentId, field.fieldKey), []).append(field)
        else:
            grouped.setdefault(field.fieldKey, []).append(field)

    items: list[Any] = []
    for field_key, sources in grouped.items():
        item = booking_review._build_raw_review_item(f"raw:{field_key}", sources)
        if item is not None:
            items.append(item)

    for (document_id, field_key), sources in document_grouped.items():
        item = booking_review._build_raw_review_item(
            f"raw:{document_id}:{field_key}",
            sources,
        )
        if item is not None:
            items.append(item)

    ordinals = receipt_document_ordinals(receipt_document_ids)
    for (document_id, field_key), sources in receipt_grouped.items():
        item = booking_review._build_raw_review_item(
            receipt_review_key(ordinals[document_id], field_key),
            sources,
        )
        if item is not None:
            items.append(item)
    return items


def _booking_review_key(
    document: review_v2.ReviewV2Document,
    field: review_v2.ReviewV2Field,
    *,
    receipt_ordinals: dict[UUID, int],
    duplicate_raw_keys: set[str],
) -> str:
    spec = review_v2.spec_for_field(field.fieldKey)
    if spec is not None:
        return f"attribute:{spec.attribute_key}"
    if is_receipt_document_type(document.documentTypeKey):
        return receipt_review_key(receipt_ordinals[document.documentId], field.fieldKey)
    if field.fieldKey.strip() in duplicate_raw_keys:
        return f"raw:{document.documentId}:{field.fieldKey.strip()}"
    return f"raw:{field.fieldKey.strip()}"


def _reviewed_fields(
    documents: list[review_v2.ReviewV2Document],
    corrections: dict[tuple[UUID, str, str, int], ReviewFieldCorrection],
    *,
    rejected_keys: set[str] | None = None,
) -> list[ReviewedDiField]:
    rejected = rejected_keys or set()
    receipt_ordinals = receipt_document_ordinals(
        [
            document.documentId
            for document in documents
            if is_receipt_document_type(document.documentTypeKey)
        ]
    )
    duplicate_raw_keys = _duplicate_raw_field_keys(documents)
    reviewed: list[ReviewedDiField] = []
    for document in documents:
        for field in document.fields:
            key = _field_target(document, field)
            correction = corrections.get(key)
            review_key = _booking_review_key(
                document,
                field,
                receipt_ordinals=receipt_ordinals,
                duplicate_raw_keys=duplicate_raw_keys,
            )
            field_rejected = review_key in rejected
            if field_rejected and correction is not None:
                raise ConflictError(
                    error_code="VAC-CONFLICT-010",
                    title="Rejected field cannot be corrected",
                    detail=(
                        f"DI field '{field.fieldKey}' is rejected in Review and cannot "
                        "also be submitted with an effective-value correction."
                    ),
                )
            reviewed.append(
                ReviewedDiField(
                    document_id=document.documentId,
                    evidence_id=document.evidenceId,
                    source_canonical_field_id=field.canonicalFieldId,
                    source_document_type_key=document.documentTypeKey,
                    field_key=field.fieldKey,
                    source_fact_version=field.sourceFactVersion,
                    extracted_value=field.value,
                    modified_value=(correction.effectiveValue if correction else None),
                    effective_value=(
                        correction.effectiveValue if correction else field.value
                    ),
                    effective_value_is_set=not field_rejected,
                    confidence_score=field.confidenceScore,
                    confidence_scale=(
                        "PERCENT" if field.confidenceScore is not None else None
                    ),
                    is_modified=correction is not None,
                )
            )
    return reviewed


def _unresolved_low_confidence_fields(
    documents: list[review_v2.ReviewV2Document],
    corrections: dict[tuple[UUID, str, str, int], ReviewFieldCorrection],
) -> list[str]:
    """Populated DI values below the 90% threshold that this confirm request
    doesn't account for -- Delivery's stand-in for Booking's decision_required/
    missing_keys gate. Delivery has no separate Accept/Reject decision table,
    so a correction submitted for the field (any effective value, including
    the extracted one resubmitted as-is) stands in for that decision here."""

    return sorted(
        f"{field.fieldKey}@{document.documentId}"
        for document in documents
        for field in document.fields
        if has_value(field.value)
        and requires_pc_review(field.confidenceScore)
        and _field_target(document, field) not in corrections
    )


def _corrected_documents(
    documents: list[review_v2.ReviewV2Document],
    corrections: dict[tuple[UUID, str, str, int], ReviewFieldCorrection],
) -> list[review_v2.ReviewV2Document]:
    corrected = [document.model_copy(deep=True) for document in documents]
    for document in corrected:
        for field in document.fields:
            correction = corrections.get(_field_target(document, field))
            if correction is not None:
                field.value = correction.effectiveValue
    return corrected


def _corrected_attributes(
    attributes: list[review_v2.ReviewV2Attribute],
    corrections: dict[tuple[UUID, str, str, int], ReviewFieldCorrection],
) -> list[review_v2.ReviewV2Attribute]:
    corrected = [attribute.model_copy(deep=True) for attribute in attributes]
    for attribute in corrected:
        for source in attribute.sources:
            correction = corrections.get(_source_target(source))
            if correction is not None:
                source.value = correction.effectiveValue
        if attribute.resolvedSource is not None:
            correction = corrections.get(_source_target(attribute.resolvedSource))
            if correction is not None:
                attribute.resolvedSource.value = correction.effectiveValue
                attribute.resolvedValue = correction.effectiveValue
    return corrected


# confirm_booking_review_v2_effective_values removed (Phase 0 monkeypatch
# removal): confirmed dead in production even before this change --
# install_uc03_confidence_review_policy ran after install_uc03_review_
# effective_values in the original chain, so its own confirm_booking_
# review_v2_confidence_policy always won this route in the end anyway.
# confirm_delivery_review_v2_effective_values below IS the live handler for
# POST /delivery/review/confirm -- confirmed by tracing install order
# (install_uc03_delivery_review_confirm ran first in the cascade, so this
# module's own _replace_confirm_route call always discarded its route in
# favor of this one). uc03_delivery_review_confirm.py's own confirm handler
# was dead code despite having its own passing test; it and its installer
# were removed once this was confirmed.


@review_v2.router.post(
    "/delivery/review/confirm",
    response_model=delivery_review.DeliveryReviewV2ConfirmResponse,
)
def confirm_delivery_review_v2_effective_values(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[review_v2.DiClient, Depends(get_di_client)],
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ],
    payload: ReviewConfirmCommand | None = None,
) -> delivery_review.DeliveryReviewV2ConfirmResponse:
    context = booking_review._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    submitted, verification_status, _ = review_v2._stage_submission_state(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code="DELIVERY",
    )
    if not submitted:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Delivery has not been submitted",
            detail="Submit Delivery document capture before completing Delivery Review.",
        )
    if verification_status != "PENDING":
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="Delivery Review is not pending",
            detail="This Delivery Review has already been completed.",
        )

    documents = delivery_review._delivery_review_documents(
        connection=connection,
        engine=engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        security_client=security_client,
        di_client=di_client,
        v2_client=v2_client,
    )
    if any(document.extractionState == "PENDING" for document in documents):
        raise ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Documents are not ready for review",
            detail="Document Intelligence is still preparing one or more Delivery documents.",
        )
    if any(document.extractionState == "FAILED" for document in documents):
        raise ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Document processing requires follow-up",
            detail="One or more Delivery documents failed processing and require follow-up.",
        )

    command = payload or ReviewConfirmCommand()
    corrections = _correction_map(documents, command.corrections)

    # Uniform confidence policy: a populated DI value below the 90% threshold
    # needs a PC's eyes before Delivery Review can complete, exactly like
    # Booking's confirm gate (missing_keys derived from decision_required).
    unresolved = _unresolved_low_confidence_fields(documents, corrections)
    if unresolved:
        raise ConflictError(
            error_code="VAC-CONFLICT-012",
            title="Review decisions are pending",
            detail=(
                f"{len(unresolved)} low-confidence extracted value"
                f"{'s' if len(unresolved) != 1 else ''} still require a reviewed "
                "correction before Delivery Review can be confirmed."
            ),
        )

    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = connection.execute(
            text(
                """
                SELECT capture_completed_at_utc, pc_verification_status, version_no
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY'
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        if state is None or state["capture_completed_at_utc"] is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Delivery has not been submitted",
                detail="Submit Delivery document capture before completing Delivery Review.",
            )
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Delivery version conflict",
                detail="Delivery changed since Review was loaded. Refresh Review and try again.",
            )
        if str(state["pc_verification_status"] or "PENDING") != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Delivery Review is not pending",
                detail="This Delivery Review has already been completed.",
            )

        stored_field_count = persist_reviewed_di_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="DELIVERY",
            actor_id=human_principal.subject,
            fields=_reviewed_fields(documents, corrections),
        )
        # Canonical materialization already runs async, per document, as each
        # Delivery document is confirmed by DI (uc03_delivery_post_extraction_
        # materialization.materialize_delivery_documents_from_durable_store,
        # itself durable-state-driven so it's safe to call redundantly). This
        # is the same synchronous safety net Booking's confirm/submit already
        # get: a PC correction applied at confirm time must be reflected in
        # Journey 360's business tables before Review can become VERIFIED,
        # not just left for the next async trigger.
        materialization = materialize_reviewed_delivery_business_values(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=_corrected_documents(documents, corrections),
            actor_id=human_principal.subject,
        )
        # Same safety-net treatment for the Delivery-invoice SKU fallback
        # (uc03_model_resolution.sync_model_resolution_from_invoice): a PC
        # correction to the invoice's model/SKU text at confirm time must
        # get its own chance to resolve here too, not wait for the next
        # async trigger. A no-op once a SKU is already pinned.
        from audit_core.uc03_async_sync_tasks import (
            sync_model_resolution_from_invoice_with_escalation,
        )

        sync_model_resolution_from_invoice_with_escalation(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            correlation_id="",
        )
        next_version = expected_version + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET pc_verification_status='VERIFIED',
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='DELIVERY'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "version": next_version,
            },
        )
        _append_delivery_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_DELIVERY_REVIEW_CONFIRMED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=f"{idempotency_key}:review-confirmed",
            correlation_id=correlation_id,
            safe_payload={
                "storedFieldCount": stored_field_count,
                "modifiedFieldCount": len(corrections),
                "rawDiValuesCopied": True,
                "canonicalMaterialization": materialization,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "pcVerificationStatus": "VERIFIED",
            "aggregateVersion": next_version,
            "storedFieldCount": stored_field_count,
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.delivery.review.confirm:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "corrections": command.model_dump(mode="json")["corrections"],
        },
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return delivery_review.DeliveryReviewV2ConfirmResponse.model_validate(body)


def install_uc03_review_effective_values() -> None:
    """Install V2 effective-value corrections and document-scoped raw review identity."""

    if getattr(review_v2, "_review_effective_values_installed", False):
        return
    booking_review._raw_review_items = _general_raw_review_items
    # POST /booking/review/confirm registration removed here (Phase 0
    # monkeypatch removal): see the removal note above
    # confirm_delivery_review_v2_effective_values -- confirm_booking_
    # review_v2_confidence_policy is decorated directly on this route in
    # uc03_confidence_review_policy.py instead.
    # POST /delivery/review/confirm's _replace_confirm_route call removed
    # (uniform-confidence-policy pass): confirm_delivery_review_v2_effective_
    # values is now decorated directly at its definition above instead.
    review_v2._review_effective_values_installed = True