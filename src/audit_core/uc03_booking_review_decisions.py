from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import Depends, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import Connection, Engine, text

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
from audit_core.uc03_attribute_resolution import (
    apply_supported_operational_attribute,
    record_attribute_resolution,
)
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
)
from audit_core.uc03_di_core_persistence import (
    ReviewedDiField,
    persist_reviewed_di_fields,
)
from audit_core.uc03_di_core_persistence import (
    scope_with_actor_context as _scope,
)
from audit_core.uc03_document_registry import is_receipt_document_type
from audit_core.uc03_review_confidence import requires_pc_review
from audit_core.uc03_v2_review_materialization import (
    materialize_reviewed_di_business_values,
    receipt_document_ordinals,
    receipt_review_key,
    reviewed_field_core_owner,
)

DecisionValue = Literal["ACCEPTED", "REJECTED"]
ReviewKind = Literal["ATTRIBUTE", "RAW_FIELD"]


class BookingReviewDecisionCommand(BaseModel):
    reviewKey: str = Field(min_length=3, max_length=240)
    decision: DecisionValue


class BookingReviewDecision(BaseModel):
    reviewKey: str
    reviewKind: ReviewKind
    decision: DecisionValue
    sourceSetRef: str
    sourceDocumentId: UUID
    sourceCanonicalFieldId: str | None = None
    sourceFieldKey: str
    sourceFactVersion: int
    decidedByActorId: str


class BookingReviewDecisionsResponse(BaseModel):
    journeyId: UUID
    decisions: list[BookingReviewDecision]


class BookingReviewV2ConfirmWithDecisionsResponse(BaseModel):
    journeyId: UUID
    pcVerificationStatus: Literal["VERIFIED"] = "VERIFIED"
    aggregateVersion: int
    resolvedAttributeCount: int
    appliedAttributes: list[str]
    conflictAttributes: list[str]
    rejectedAttributes: list[str]


@dataclass(frozen=True)
class _ReviewItem:
    review_key: str
    review_kind: ReviewKind
    decision_required: bool
    source_set_ref: str
    source_document_id: UUID
    source_canonical_field_id: str | None
    source_field_key: str
    source_fact_version: int


def _has_value(value: Any) -> bool:
    return value is not None and value != ""


def _normalized_value(value: Any) -> str:
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _source_set_ref(sources: list[Any]) -> str:
    refs = sorted(
        [
            [
                str(source.documentId),
                str(source.canonicalFieldId or ""),
                str(source.fieldKey),
                int(source.sourceFactVersion),
            ]
            for source in sources
        ]
    )
    return json.dumps(refs, separators=(",", ":"))


def _mapped_review_items(
    attributes: list[review_v2.ReviewV2Attribute],
) -> list[_ReviewItem]:
    items: list[_ReviewItem] = []
    for attribute in attributes:
        source = attribute.resolvedSource
        if source is None or not _has_value(attribute.resolvedValue):
            continue
        items.append(
            _ReviewItem(
                review_key=f"attribute:{attribute.attributeKey}",
                review_kind="ATTRIBUTE",
                decision_required=attribute.reviewState == "NEEDS_REVIEW",
                source_set_ref=_source_set_ref(attribute.sources),
                source_document_id=source.documentId,
                source_canonical_field_id=source.canonicalFieldId,
                source_field_key=source.fieldKey,
                source_fact_version=source.sourceFactVersion,
            )
        )
    return items


def _build_raw_review_item(
    review_key: str,
    sources: list[review_v2.ReviewV2UnmappedField],
) -> _ReviewItem | None:
    populated = [source for source in sources if _has_value(source.value)]
    if not populated:
        return None
    selected = min(
        populated,
        key=lambda source: (
            -(source.confidenceScore if source.confidenceScore is not None else -1.0),
            source.documentLabel.casefold(),
            str(source.documentId),
            source.canonicalFieldId,
            source.sourceFactVersion,
        ),
    )
    distinct_values = {_normalized_value(source.value) for source in populated}
    low_confidence = requires_pc_review(selected.confidenceScore)
    return _ReviewItem(
        review_key=review_key,
        review_kind="RAW_FIELD",
        decision_required=len(distinct_values) > 1 or low_confidence,
        source_set_ref=_source_set_ref(sources),
        source_document_id=selected.documentId,
        source_canonical_field_id=selected.canonicalFieldId,
        source_field_key=selected.fieldKey,
        source_fact_version=selected.sourceFactVersion,
    )


def _raw_review_items(
    unmapped: list[review_v2.ReviewV2UnmappedField],
) -> list[_ReviewItem]:
    grouped: dict[str, list[review_v2.ReviewV2UnmappedField]] = {}
    receipt_grouped: dict[
        tuple[UUID, str], list[review_v2.ReviewV2UnmappedField]
    ] = {}
    receipt_document_ids: list[UUID] = []

    for field in unmapped:
        if is_receipt_document_type(field.documentTypeKey):
            receipt_grouped.setdefault((field.documentId, field.fieldKey), []).append(field)
            receipt_document_ids.append(field.documentId)
        else:
            grouped.setdefault(field.fieldKey, []).append(field)

    items: list[_ReviewItem] = []
    for field_key, sources in grouped.items():
        item = _build_raw_review_item(f"raw:{field_key}", sources)
        if item is not None:
            items.append(item)

    ordinals = receipt_document_ordinals(receipt_document_ids)
    for (document_id, field_key), sources in receipt_grouped.items():
        item = _build_raw_review_item(
            receipt_review_key(ordinals[document_id], field_key),
            sources,
        )
        if item is not None:
            items.append(item)
    return items


def _current_review_items(
    attributes: list[review_v2.ReviewV2Attribute],
    unmapped: list[review_v2.ReviewV2UnmappedField],
) -> dict[str, _ReviewItem]:
    items = _mapped_review_items(attributes) + _raw_review_items(unmapped)
    return {item.review_key: item for item in items}


def _decision_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT review_key, review_kind, decision, source_set_ref,
                   source_di_document_id, source_canonical_field_id,
                   source_field_key, source_fact_version, decided_by_actor_id
            FROM auditcore.journey_attribute_review_decisions
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND stage_code='BOOKING'
            ORDER BY review_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def _decision_model(row: dict[str, Any]) -> BookingReviewDecision:
    return BookingReviewDecision(
        reviewKey=str(row["review_key"]),
        reviewKind=str(row["review_kind"]),
        decision=str(row["decision"]),
        sourceSetRef=str(row["source_set_ref"]),
        sourceDocumentId=UUID(str(row["source_di_document_id"])),
        sourceCanonicalFieldId=(
            str(row["source_canonical_field_id"])
            if row.get("source_canonical_field_id")
            else None
        ),
        sourceFieldKey=str(row["source_field_key"]),
        sourceFactVersion=int(row["source_fact_version"]),
        decidedByActorId=str(row["decided_by_actor_id"]),
    )


def get_booking_review_decisions(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingReviewDecisionsResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return BookingReviewDecisionsResponse(
        journeyId=journey_id,
        decisions=[
            _decision_model(row)
            for row in _decision_rows(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
            )
        ],
    )


# set_booking_review_decision removed (Phase 0 monkeypatch removal):
# confirmed dead (no callers besides its own registration) -- its route was
# always discarded by install_uc03_confidence_review_policy's later
# _replace_route call. set_booking_review_decision_confidence_policy
# (uc03_confidence_review_policy.py) is the live handler -- functionally
# equivalent (same journey_attribute_review_decisions write), now decorated
# directly on review_v2.router's POST /booking/review/decision.


def _current_decisions(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    items: dict[str, _ReviewItem],
) -> dict[str, DecisionValue]:
    current: dict[str, DecisionValue] = {}
    for row in _decision_rows(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    ):
        item = items.get(str(row["review_key"]))
        if item is None or item.source_set_ref != str(row["source_set_ref"]):
            continue
        current[item.review_key] = str(row["decision"])
    return current


def _missing_core_owner_error(
    *,
    field_key: str,
    document_type_key: str | None,
    attribute_key: str | None = None,
) -> ConflictError:
    document_type = str(document_type_key or "UNKNOWN")
    subject = f"attribute '{attribute_key}' / " if attribute_key else ""
    return ConflictError(
        error_code="VAC-CONFLICT-013",
        title="Reviewed value has no Audit Core owner",
        detail=(
            f"Accepted {subject}DI field '{field_key}' from document type "
            f"'{document_type}' has no Audit Core persistence owner. "
            "Add an explicit Core owner before confirming Review."
        ),
    )


def _raw_review_key(
    field: review_v2.ReviewV2UnmappedField,
    *,
    receipt_ordinals: dict[UUID, int],
) -> str:
    if is_receipt_document_type(field.documentTypeKey):
        return receipt_review_key(receipt_ordinals[field.documentId], field.fieldKey)
    return f"raw:{field.fieldKey}"


def _document_field_review_key(
    document: review_v2.ReviewV2Document,
    field: review_v2.ReviewV2Field,
    *,
    receipt_ordinals: dict[UUID, int],
) -> str:
    spec = review_v2.spec_for_field(field.fieldKey)
    if spec is not None:
        return f"attribute:{spec.attribute_key}"
    if is_receipt_document_type(document.documentTypeKey):
        return receipt_review_key(receipt_ordinals[document.documentId], field.fieldKey)
    return f"raw:{field.fieldKey}"


def _lossless_reviewed_fields(
    documents: list[review_v2.ReviewV2Document],
    *,
    rejected_keys: set[str],
) -> list[ReviewedDiField]:
    receipt_ordinals = receipt_document_ordinals(
        [
            document.documentId
            for document in documents
            if is_receipt_document_type(document.documentTypeKey)
        ]
    )
    reviewed: list[ReviewedDiField] = []
    for document in documents:
        for field in document.fields:
            review_key = _document_field_review_key(
                document,
                field,
                receipt_ordinals=receipt_ordinals,
            )
            rejected = review_key in rejected_keys
            reviewed.append(
                ReviewedDiField(
                    document_id=document.documentId,
                    evidence_id=document.evidenceId,
                    source_canonical_field_id=field.canonicalFieldId,
                    source_document_type_key=document.documentTypeKey,
                    field_key=field.fieldKey,
                    source_fact_version=field.sourceFactVersion,
                    extracted_value=field.value,
                    effective_value=field.value,
                    effective_value_is_set=not rejected,
                    confidence_score=field.confidenceScore,
                    confidence_scale=(
                        "PERCENT" if field.confidenceScore is not None else None
                    ),
                    is_modified=False,
                )
            )
    return reviewed


def confirm_booking_review_v2_with_decisions(
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
) -> BookingReviewV2ConfirmWithDecisionsResponse:
    context = _scope(
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

    items = _current_review_items(attributes, unmapped)
    decisions = _current_decisions(
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
            fields=_lossless_reviewed_fields(
                documents,
                rejected_keys=rejected_keys,
            ),
        )

        applied: list[str] = []
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
                raise _missing_core_owner_error(
                    field_key=source.fieldKey,
                    document_type_key=source.documentTypeKey,
                    attribute_key=attribute.attributeKey,
                )

            resolved_count += 1
            application = apply_supported_operational_attribute(
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
                record_attribute_resolution(
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
            documents=documents,
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
        _append_workflow_event(
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
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    response.headers["ETag"] = f'"{body["aggregateVersion"]}"'
    return BookingReviewV2ConfirmWithDecisionsResponse.model_validate(body)


def _install_mismatch_review_rule() -> None:
    if getattr(review_v2, "_mismatch_review_rule_installed", False):
        return
    original = review_v2._build_attributes

    def wrapped(*args: Any, **kwargs: Any):
        attributes, unmapped = original(*args, **kwargs)
        for attribute in attributes:
            if attribute.resolvedValue is not None and attribute.comparisonState == "MISMATCH":
                attribute.reviewState = "NEEDS_REVIEW"
        return attributes, unmapped

    review_v2._build_attributes = wrapped  # type: ignore[assignment]
    review_v2._mismatch_review_rule_installed = True


# _replace_confirm_route removed (Phase 0 monkeypatch removal): there was
# never a plain @router decorator for POST /booking/review/confirm anywhere
# in the codebase -- this route was assembled entirely through a chain of
# runtime add_api_route/_replace_route calls (this one, then uc03_review_
# effective_values.py's, then uc03_booking_rule_trigger.py's, each replacing
# the last, ending at uc03_confidence_review_policy.py's
# confirm_booking_review_v2_confidence_policy, which actually won in
# production). That function is now the first-ever plain @router decorator
# for this path. confirm_booking_review_v2_with_decisions stays defined
# here as plain library code -- test_uc03_booking_review_resolution_wiring.py
# inspects its bytecode directly -- just no longer registered as a route.


def install_uc03_booking_review_decisions() -> None:
    """Install V2 Booking Review exception decisions without altering V1 flows."""

    if getattr(review_v2, "_booking_review_decisions_installed", False):
        return
    _install_mismatch_review_rule()
    review_v2.router.add_api_route(
        "/booking/review/decisions",
        get_booking_review_decisions,
        methods=["GET"],
        response_model=BookingReviewDecisionsResponse,
    )
    # /booking/review/decision (POST) registration removed here (Phase 0
    # monkeypatch removal): set_booking_review_decision_confidence_policy
    # (uc03_confidence_review_policy.py) is decorated directly on this
    # router for that path instead.
    review_v2._booking_review_decisions_installed = True
