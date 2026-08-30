from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from fastapi.routing import APIRoute
from sqlalchemy import Connection, Engine, text

from audit_core import uc03_booking_review_decisions as review_decisions
from audit_core import uc03_document_review_v2 as review_v2
from audit_core.dependencies import get_connection, get_engine, get_human_principal
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient
from audit_core.uc03_v2_review_materialization import (
    materialize_reviewed_booking_receipts,
    receipt_document_ordinals,
    receipt_review_key,
)

_BASE_RAW_REVIEW_ITEMS = review_decisions._raw_review_items
_RECEIPT_DOCUMENT_TYPE = "dealer_receipt"


def _receipt_raw_review_items(
    unmapped: list[review_v2.ReviewV2UnmappedField],
) -> list[review_decisions._ReviewItem]:
    grouped: dict[tuple[UUID, str], list[review_v2.ReviewV2UnmappedField]] = {}
    for field in unmapped:
        if str(field.documentTypeKey or "").strip().lower() != _RECEIPT_DOCUMENT_TYPE:
            continue
        if not review_decisions._has_value(field.value):
            continue
        grouped.setdefault((field.documentId, field.fieldKey), []).append(field)

    ordinals = receipt_document_ordinals([document_id for document_id, _ in grouped])
    items: list[review_decisions._ReviewItem] = []
    for (document_id, field_key), sources in grouped.items():
        selected = sorted(
            sources,
            key=lambda source: (
                -(source.confidenceScore if source.confidenceScore is not None else -1.0),
                source.canonicalFieldId,
                source.sourceFactVersion,
            ),
        )[0]
        distinct_values = {
            review_decisions._normalized_value(source.value) for source in sources
        }
        low_confidence = (
            selected.confidenceScore is None
            or selected.confidenceScore < review_v2._REVIEW_THRESHOLD
        )
        items.append(
            review_decisions._ReviewItem(
                review_key=receipt_review_key(ordinals[document_id], field_key),
                review_kind="RAW_FIELD",
                decision_required=len(distinct_values) > 1 or low_confidence,
                source_set_ref=review_decisions._source_set_ref(sources),
                source_document_id=selected.documentId,
                source_canonical_field_id=selected.canonicalFieldId,
                source_field_key=selected.fieldKey,
                source_fact_version=selected.sourceFactVersion,
            )
        )
    return items


def _raw_review_items_with_receipts(
    unmapped: list[review_v2.ReviewV2UnmappedField],
) -> list[review_decisions._ReviewItem]:
    non_receipt = [
        field
        for field in unmapped
        if str(field.documentTypeKey or "").strip().lower() != _RECEIPT_DOCUMENT_TYPE
    ]
    return _BASE_RAW_REVIEW_ITEMS(non_receipt) + _receipt_raw_review_items(unmapped)


def confirm_booking_review_v2_materialized(
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
) -> review_decisions.BookingReviewV2ConfirmWithDecisionsResponse:
    context = review_decisions._scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = review_decisions._parse_if_match(if_match)
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
        raise review_decisions.ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Documents are not ready for review",
            detail="Document Intelligence is still preparing one or more Booking documents.",
        )
    if any(document.extractionState == "FAILED" for document in documents):
        raise review_decisions.ConflictError(
            error_code="VAC-CONFLICT-011",
            title="Document processing requires follow-up",
            detail="One or more Booking documents failed processing and require follow-up.",
        )

    items = review_decisions._current_review_items(attributes, unmapped)
    decisions = review_decisions._current_decisions(
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
        raise review_decisions.ConflictError(
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
    correlation_id = review_decisions.get_correlation_id(request)

    def execute() -> dict[str, Any]:
        review_decisions._aggregate_lock(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
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
            raise review_decisions.ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking capture has not been submitted",
                detail="Submit Booking before completing Review.",
            )
        if int(state["version_no"]) != expected_version:
            raise review_decisions.ConflictError(
                error_code="VAC-CONFLICT-005",
                title="Booking version conflict",
                detail="Booking changed since Review was loaded. Refresh Review and try again.",
            )
        if str(state["pc_verification_status"] or "PENDING") != "PENDING":
            raise review_decisions.ConflictError(
                error_code="VAC-CONFLICT-010",
                title="Booking Review is not pending",
                detail="This Booking Review has already been completed.",
            )

        applied: list[str] = []
        review_only: list[str] = []
        conflicts: list[str] = []
        rejected_attributes: list[str] = []
        resolved_count = 0

        for attribute in attributes:
            source = attribute.resolvedSource
            if source is None or attribute.resolvedValue is None:
                continue
            review_key = f"attribute:{attribute.attributeKey}"
            if review_key in rejected_keys:
                rejected_attributes.append(attribute.attributeKey)
                continue
            spec = review_v2.spec_for_field(source.fieldKey)
            if spec is None or spec.attribute_key != attribute.attributeKey:
                continue
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
                review_only.append(attribute.attributeKey)
            else:
                owning_domain_key, owning_record_reference, application_status = application
                applied.append(attribute.attributeKey)
                if application_status == "CONFLICT":
                    conflicts.append(attribute.attributeKey)

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

        receipt_result = materialize_reviewed_booking_receipts(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            documents=documents,
            rejected_review_keys=rejected_keys,
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
        review_decisions._append_workflow_event(
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
                "appliedAttributeKeys": sorted(applied),
                "reviewOnlyAttributeKeys": sorted(review_only),
                "conflictAttributeKeys": sorted(conflicts),
                "rejectedReviewKeys": sorted(rejected_keys),
                "receiptPaymentsCreated": receipt_result["created"],
                "receiptPaymentsUpdated": receipt_result["updated"],
                "receiptPaymentsUnchanged": receipt_result["unchanged"],
                "receiptPaymentsSkippedWithoutAmount": receipt_result[
                    "skippedWithoutAmount"
                ],
                "rawDiValuesCopied": False,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "pcVerificationStatus": "VERIFIED",
            "aggregateVersion": next_version,
            "resolvedAttributeCount": resolved_count,
            "appliedAttributes": sorted(applied),
            "reviewOnlyAttributes": sorted(review_only),
            "conflictAttributes": sorted(conflicts),
            "rejectedAttributes": sorted(rejected_attributes),
        }

    body, _ = review_decisions.execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.attribute-review.confirm:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return review_decisions.BookingReviewV2ConfirmWithDecisionsResponse.model_validate(body)


def _replace_confirm_route() -> None:
    retained = []
    for route in review_v2.router.routes:
        if (
            isinstance(route, APIRoute)
            and route.path.endswith("/booking/review/confirm")
            and "POST" in route.methods
        ):
            continue
        retained.append(route)
    review_v2.router.routes[:] = retained
    review_v2.router.add_api_route(
        "/booking/review/confirm",
        confirm_booking_review_v2_materialized,
        methods=["POST"],
        response_model=review_decisions.BookingReviewV2ConfirmWithDecisionsResponse,
    )


def install_uc03_v2_review_materialization() -> None:
    if getattr(review_v2, "_v2_review_materialization_installed", False):
        return
    review_decisions._raw_review_items = _raw_review_items_with_receipts  # type: ignore[assignment]
    _replace_confirm_route()
    review_v2._v2_review_materialization_installed = True
