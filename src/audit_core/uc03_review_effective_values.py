from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from fastapi.routing import APIRoute
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
from audit_core.uc03_di_core_persistence import ReviewedDiField, persist_reviewed_di_fields
from audit_core.uc03_v2_review_materialization import (
    materialize_reviewed_di_business_values,
    receipt_document_ordinals,
    receipt_review_key,
    reviewed_field_core_owner,
)

_RECEIPT_DOCUMENT_TYPE = "dealer_receipt"


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


def _duplicate_raw_field_keys(
    documents: list[review_v2.ReviewV2Document],
) -> set[str]:
    document_ids: dict[str, set[UUID]] = {}
    for document in documents:
        if str(document.documentTypeKey or "").strip().lower() == _RECEIPT_DOCUMENT_TYPE:
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
        if str(field.documentTypeKey or "").strip().lower() == _RECEIPT_DOCUMENT_TYPE:
            continue
        documents_by_field.setdefault(field.fieldKey, set()).add(field.documentId)
    duplicate_keys = {
        field_key
        for field_key, document_ids in documents_by_field.items()
        if len(document_ids) > 1
    }

    for field in unmapped:
        if str(field.documentTypeKey or "").strip().lower() == _RECEIPT_DOCUMENT_TYPE:
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
    if str(document.documentTypeKey or "").strip().lower() == _RECEIPT_DOCUMENT_TYPE:
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
            if str(document.documentTypeKey or "").strip().lower()
            == _RECEIPT_DOCUMENT_TYPE
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


def confirm_booking_review_v2_effective_values(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    payload: ReviewConfirmCommand | None = None,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ] = None,
    connection: Annotated[Connection, Depends(get_connection)] = None,
    engine: Annotated[Engine, Depends(get_engine)] = None,
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ] = None,
    di_client: Annotated[review_v2.DiClient, Depends(get_di_client)] = None,
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ] = None,
) -> booking_review.BookingReviewV2ConfirmWithDecisionsResponse:
    context = booking_review._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    _, documents, attributes, unmapped = review_v2._booking_review_data(
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
            detail="Document Intelligence is still preparing one or more Booking documents.",
        )
    if any(document.extractionState == "FAILED" for document in documents):
        raise ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Document processing requires follow-up",
            detail="One or more Booking documents failed processing and require follow-up.",
        )

    items = booking_review._current_review_items(attributes, unmapped)
    decisions = booking_review._current_decisions(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        items=items,
    )
    required_keys = sorted(
        item.review_key for item in items.values() if item.decision_required
    )
    missing_keys = [key for key in required_keys if key not in decisions]
    if missing_keys:
        raise ConflictError(
            error_code="VAC-CONFLICT-012",
            title="Review decisions are pending",
            detail=(
                f"{len(missing_keys)} extracted value"
                f"{'s' if len(missing_keys) != 1 else ''} still require Accept or Reject."
            ),
        )

    rejected_keys = {
        key for key, decision in decisions.items() if decision == "REJECTED"
    }
    command = payload or ReviewConfirmCommand()
    corrections = _correction_map(documents, command.corrections)
    corrected_documents = _corrected_documents(documents, corrections)
    corrected_attributes = _corrected_attributes(attributes, corrections)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = connection.execute(
            text(
                """
                SELECT capture_completed_at_utc, pc_verification_status, version_no
                FROM auditcore.journey_stage_states
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).mappings().one_or_none()
        if state is None or state["capture_completed_at_utc"] is None:
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking capture has not been submitted",
                detail="Submit Booking before completing Review.",
            )
        if int(state["version_no"]) != expected_version:
            raise ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since Review was loaded. Refresh Review and try again.",
            )
        if str(state["pc_verification_status"] or "PENDING") != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking Review is not pending",
                detail="This Booking Review has already been completed.",
            )

        stored_field_count = persist_reviewed_di_fields(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code="BOOKING",
            actor_id=human_principal.subject,
            fields=_reviewed_fields(
                documents,
                corrections,
                rejected_keys=rejected_keys,
            ),
        )

        applied: list[str] = []
        conflicts: list[str] = []
        rejected_attributes: list[str] = []
        resolved_count = 0
        for attribute in corrected_attributes:
            source = attribute.resolvedSource
            if source is None or attribute.resolvedValue is None:
                continue
            review_key = f"attribute:{attribute.attributeKey}"
            if review_key in rejected_keys:
                rejected_attributes.append(attribute.attributeKey)
                continue

            spec = review_v2.spec_for_field(source.fieldKey)
            if spec is None or spec.attribute_key != attribute.attributeKey:
                raise booking_review._missing_core_owner_error(
                    field_key=source.fieldKey,
                    document_type_key=source.documentTypeKey,
                    attribute_key=attribute.attributeKey,
                )

            resolved_count += 1
            application = review_v2.apply_supported_operational_attribute(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                spec=spec,
                value=attribute.resolvedValue,
                actor_id=human_principal.subject,
                source_document_type_key=source.documentTypeKey,
                source_field_key=source.fieldKey,
                source_evidence_id=source.evidenceId,
            )

            owning_domain_key: str | None = None
            owning_record_reference: str | None = None
            if application is None:
                typed_owner = reviewed_field_core_owner(
                    document_type_key=source.documentTypeKey,
                    field_key=source.fieldKey,
                    document_id=source.documentId,
                )
                if typed_owner is not None:
                    owning_domain_key, owning_record_reference = typed_owner
            else:
                owning_domain_key, owning_record_reference, application_status = application
                if application_status == "CONFLICT":
                    conflicts.append(attribute.attributeKey)

            applied.append(attribute.attributeKey)
            if spec.mapping_status == "SUPPORTED":
                review_v2.record_attribute_resolution(
                    connection,
                    tenant_id=tenant_id,
                    journey_id=journey_id,
                    stage_code="BOOKING",
                    spec=spec,
                    source_di_document_id=source.documentId,
                    source_evidence_id=source.evidenceId,
                    source_canonical_field_id=source.canonicalFieldId,
                    source_field_key=source.fieldKey,
                    source_fact_version=source.sourceFactVersion,
                    source_document_type_key=source.documentTypeKey,
                    actor_id=human_principal.subject,
                    owning_domain_key=owning_domain_key,
                    owning_record_reference=owning_record_reference,
                )

        materialization = materialize_reviewed_di_business_values(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=corrected_documents,
            rejected_review_keys=rejected_keys,
            actor_id=human_principal.subject,
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
                  AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        booking_review._append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="PC_BOOKING_ATTRIBUTE_REVIEW_CONFIRMED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=f"{idempotency_key}:review-confirmed",
            correlation_id=correlation_id,
            safe_payload={
                "resolvedAttributeCount": resolved_count,
                "storedFieldCount": stored_field_count,
                "modifiedFieldCount": len(corrections),
                "appliedAttributeKeys": sorted(applied),
                "conflictAttributeKeys": sorted(conflicts),
                "rejectedReviewKeys": sorted(rejected_keys),
                **materialization,
                "rawDiValuesCopied": True,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "pcVerificationStatus": "VERIFIED",
            "aggregateVersion": next_version,
            "resolvedAttributeCount": resolved_count,
            "appliedAttributes": sorted(applied),
            "conflictAttributes": sorted(conflicts),
            "rejectedAttributes": sorted(rejected_attributes),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.attribute-review.confirm:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "corrections": command.model_dump(mode="json")["corrections"],
        },
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return booking_review.BookingReviewV2ConfirmWithDecisionsResponse.model_validate(body)


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
    payload: ReviewConfirmCommand | None = None,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)] = None,
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ] = None,
    connection: Annotated[Connection, Depends(get_connection)] = None,
    engine: Annotated[Engine, Depends(get_engine)] = None,
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ] = None,
    di_client: Annotated[review_v2.DiClient, Depends(get_di_client)] = None,
    v2_client: Annotated[
        review_v2.DiCaptureV2Client,
        Depends(review_v2.get_di_capture_v2_client),
    ] = None,
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


def _replace_confirm_route(
    suffix: str,
    endpoint: Any,
    response_model: type[BaseModel],
) -> None:
    retained = []
    for route in review_v2.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path.endswith(suffix)
            and "POST" in route.methods
        ):
            continue
        retained.append(route)
    review_v2.router.routes[:] = retained
    review_v2.router.add_api_route(
        suffix,
        endpoint,
        methods=["POST"],
        response_model=response_model,
    )


def install_uc03_review_effective_values() -> None:
    """Install V2 effective-value corrections and document-scoped raw review identity."""

    if getattr(review_v2, "_review_effective_values_installed", False):
        return
    booking_review._raw_review_items = _general_raw_review_items
    _replace_confirm_route(
        "/booking/review/confirm",
        confirm_booking_review_v2_effective_values,
        booking_review.BookingReviewV2ConfirmWithDecisionsResponse,
    )
    _replace_confirm_route(
        "/delivery/review/confirm",
        confirm_delivery_review_v2_effective_values,
        delivery_review.DeliveryReviewV2ConfirmResponse,
    )
    review_v2._review_effective_values_installed = True
