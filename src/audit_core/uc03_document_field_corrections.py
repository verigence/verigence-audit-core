"""uc03_document_field_corrections.py — >=90%-confidence field correction
approval flow (unified Documents review redesign, 2026-09-13).

Booking/Delivery's own Confirm gates were relaxed so document completeness
alone finishes the stage (uc03_simplified_booking_flow.py,
uc03_confidence_review_policy.py, uc03_review_effective_values.py). A DI
field extracted below the 90% confidence threshold still goes through the
existing Review Confirm's ``corrections`` payload (direct edit + confirm --
see uc03_review_effective_values.py). A field extracted AT OR ABOVE 90%
confidence that is nonetheless wrong is deliberately NOT directly editable:
the design calls for a structured, TL-adjudicated correction instead of
letting a PC silently overwrite a high-confidence DI value.

This module raises that correction as a VIOLATION finding (routed through
the existing classification/routing machinery -- see the
"DI_VALUE_CORRECTION_PROPOSED" entry in uc03_finding_routing.py's
_VIOLATION_RULE_PREFIXES) and records the proposed replacement in
``journey_document_field_correction_proposals``, keyed 1:1 by the finding's
own audit_finding_id. A Team Lead then adjudicates it exactly like any other
VIOLATION -- CONFIRM_BREACH or MARK_FALSE_POSITIVE via the existing
``POST /flags/{flag_id}/actions`` endpoint (uc03_audit_flags.py::act_on_flag)
-- no new action codes. ``apply_confirmed_field_correction`` below is called
from that handler's own execute() on CONFIRM_BREACH; MARK_FALSE_POSITIVE
needs no extra step here -- the finding simply resolves as a false positive
and the original DI value is left untouched (the proposal row's
applied_at_utc stays NULL).

Applying an approved correction reuses ``persist_reviewed_di_fields()`` --
the same write path 5 existing Confirm handlers already use -- plus the
same conditional typed-attribute projection those handlers use
(``apply_supported_operational_attribute`` / ``record_attribute_resolution``
when the field maps to a SUPPORTED attribute). It deliberately does NOT call
the heavier ``materialize_reviewed_di_business_values`` journey-wide
re-materialization pass -- disproportionate to approving one field on one
document, and not needed: the typed projection above already carries the
one business-owner write a single field's correction can require.
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import NotFoundError
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_attribute_mapping import spec_for_field
from audit_core.uc03_attribute_resolution import (
    apply_supported_operational_attribute,
    record_attribute_resolution,
)
from audit_core.uc03_audit_flags import (
    FlagMutationResponse,
    FlagView,
    _append_finding_event,
    _finding,
    _flag_view,
    _scope,
    _set_etag,
    _view_context,
)
from audit_core.uc03_di_core_persistence import (
    ReviewedDiField,
    persist_reviewed_di_fields,
)
from audit_core.uc03_finding_classification import resolve_classification

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/uc03/documents",
    tags=["uc03-document-field-corrections"],
)

StageCode = Literal["BOOKING", "DELIVERY"]

# Matched against uc03_finding_routing._VIOLATION_RULE_PREFIXES's stem so this
# resolves to VIOLATION/ADJUDICATED with zero new routing plumbing.
FINDING_TYPE_CODE = "DI_VALUE_CORRECTION_PROPOSED"


class ProposeFieldCorrectionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: StageCode
    documentId: UUID
    documentTypeKey: str = Field(min_length=1, max_length=120)
    fieldKey: str = Field(min_length=1, max_length=160)
    canonicalFieldId: str = Field(min_length=1, max_length=160)
    sourceFactVersion: int = Field(gt=0)
    confidenceScore: float | None = None
    evidenceId: UUID | None = None
    originalValue: Any | None = None
    proposedValue: Any | None = Field(...)
    # Same discipline as a human-raised flag (uc03_audit_flags.FlagCreateCommand):
    # a proposal with no remark explaining why the DI value is wrong is exactly
    # the "no one should be able to say the system has issues" gap this
    # redesign exists to close.
    remarks: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def require_non_blank_remarks(self):
        if not self.remarks.strip():
            raise ValueError("Remarks are required and cannot be blank.")
        return self


def _insert_proposal(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    audit_finding_id: UUID,
    command: ProposeFieldCorrectionCommand,
    actor_id: str,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_field_correction_proposals (
                tenant_id, audit_finding_id, journey_id, stage_code,
                document_id, evidence_id, document_type_key, field_key,
                canonical_field_id, source_fact_version, confidence_score,
                original_value, proposed_value, proposed_by_actor_id
            ) VALUES (
                :tenant_id, :audit_finding_id, :journey_id, :stage_code,
                :document_id, :evidence_id, :document_type_key, :field_key,
                :canonical_field_id, :source_fact_version, :confidence_score,
                CAST(:original_value AS jsonb), CAST(:proposed_value AS jsonb),
                :actor_id
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "audit_finding_id": audit_finding_id,
            "journey_id": journey_id,
            "stage_code": command.stage,
            "document_id": command.documentId,
            "evidence_id": command.evidenceId,
            "document_type_key": command.documentTypeKey,
            "field_key": command.fieldKey,
            "canonical_field_id": command.canonicalFieldId,
            "source_fact_version": command.sourceFactVersion,
            "confidence_score": command.confidenceScore,
            "original_value": _json(command.originalValue),
            "proposed_value": _json(command.proposedValue),
            "actor_id": actor_id,
        },
    )


def _json(value: Any) -> str | None:
    return json.dumps(value, default=str)


@router.post(
    "/{document_id}/field-corrections",
    response_model=FlagMutationResponse,
)
def propose_field_correction(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    payload: ProposeFieldCorrectionCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FlagMutationResponse:
    if payload.documentId != document_id:
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Document not found",
            detail="The document in the URL does not match the correction payload.",
        )
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        operation="RAISE",
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    row = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.document_capture_v2_documents
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND di_document_id=:document_id AND stage_code=:stage_code
              AND capture_status <> 'SUPERSEDED'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "document_id": document_id,
            "stage_code": payload.stage,
        },
    ).scalar_one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Document not found",
            detail="This document is not on this journey's Booking/Delivery capture.",
        )
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        routing = resolve_classification(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            rule_key=f"{FINDING_TYPE_CODE}:{payload.fieldKey}",
            finding_type_code=FINDING_TYPE_CODE,
            severity="MEDIUM",
        )
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, description, created_by_actor_id,
                    correlation_id, stage_code, origin_kind, origin_actor_id,
                    origin_role_snapshot, rule_key, blocking_completion,
                    finding_class, owner_role_code, sla_due_at_utc
                ) VALUES (
                    :tenant_id, :journey_id, :finding_type_code, 'MEDIUM',
                    'OPEN', :title, :description, :actor_id,
                    :correlation_id, :stage_code, 'HUMAN', :actor_id,
                    :actor_role, :rule_key, false,
                    :finding_class, :owner_role_code, :sla_due_at_utc
                ) RETURNING audit_finding_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "finding_type_code": FINDING_TYPE_CODE,
                "title": f"Proposed correction: {payload.fieldKey} on {payload.documentTypeKey}",
                "description": payload.remarks.strip(),
                "actor_id": human_principal.subject,
                "correlation_id": correlation_id,
                "stage_code": payload.stage,
                "actor_role": context["operating_role"],
                "rule_key": f"{FINDING_TYPE_CODE}:{payload.fieldKey}",
                **routing,
            },
        ).scalar_one()
        _insert_proposal(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            audit_finding_id=flag_id,
            command=payload,
            actor_id=human_principal.subject,
        )
        event_id = _append_finding_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
            stage_code=payload.stage,
            event_type="RAISED",
            actor_id=human_principal.subject,
            actor_role=context["operating_role"],
            reason=payload.remarks.strip(),
            correlation_id=correlation_id,
            safe_payload={
                "originKind": "HUMAN",
                "category": FINDING_TYPE_CODE,
                "fieldKey": payload.fieldKey,
                "documentId": str(payload.documentId),
            },
        )
        row = _finding(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            flag_id=flag_id,
        )
        role, policy = _view_context(context)
        return {
            "flag": _flag_view(
                connection, tenant_id=tenant_id, row=row, role=role, policy=policy
            ).model_dump(mode="json"),
            "eventId": str(event_id),
        }

    body, replay = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.document-field-correction.propose:{journey_id}:{document_id}:{payload.fieldKey}:{payload.sourceFactVersion}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    flag = FlagView.model_validate(body["flag"])
    _set_etag(response, flag.version)
    return FlagMutationResponse(flag=flag, eventId=UUID(body["eventId"]), idempotent=replay)


def apply_confirmed_field_correction(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    audit_finding_id: UUID,
    actor_id: str,
) -> None:
    """Write the proposed value as the field's new reviewed/effective value.

    Called from uc03_audit_flags.py::act_on_flag's execute() on
    CONFIRM_BREACH for a DI_VALUE_CORRECTION_PROPOSED finding, inside the
    same transaction as the finding's own status update.
    """

    proposal = connection.execute(
        text(
            """
            SELECT stage_code, document_id, evidence_id, document_type_key,
                   field_key, canonical_field_id, source_fact_version,
                   confidence_score, original_value, proposed_value
            FROM auditcore.journey_document_field_correction_proposals
            WHERE tenant_id=:tenant_id AND audit_finding_id=:audit_finding_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "audit_finding_id": audit_finding_id},
    ).mappings().one_or_none()
    if proposal is None:
        # Defensive only: every DI_VALUE_CORRECTION_PROPOSED finding is created
        # with its proposal row in the same transaction (propose_field_
        # correction above) -- this should be unreachable in practice.
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Correction proposal not found",
            detail="No proposed correction is recorded for this finding.",
        )

    reviewed_field = ReviewedDiField(
        document_id=proposal["document_id"],
        evidence_id=proposal["evidence_id"],
        source_canonical_field_id=proposal["canonical_field_id"],
        source_document_type_key=proposal["document_type_key"],
        field_key=proposal["field_key"],
        source_fact_version=proposal["source_fact_version"],
        extracted_value=proposal["original_value"],
        modified_value=proposal["proposed_value"],
        effective_value=proposal["proposed_value"],
        is_modified=True,
        confidence_score=proposal["confidence_score"],
        confidence_scale="PERCENT" if proposal["confidence_score"] is not None else None,
    )
    persist_reviewed_di_fields(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=proposal["stage_code"],
        actor_id=actor_id,
        fields=[reviewed_field],
    )

    spec = spec_for_field(proposal["field_key"])
    if spec is not None and spec.mapping_status == "SUPPORTED":
        application = apply_supported_operational_attribute(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            spec=spec,
            value=proposal["proposed_value"],
            actor_id=actor_id,
            source_document_type_key=proposal["document_type_key"],
            source_field_key=proposal["field_key"],
            source_evidence_id=proposal["evidence_id"],
        )
        if application is not None:
            owning_domain_key, owning_record_reference, _status = application
            record_attribute_resolution(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=proposal["stage_code"],
                spec=spec,
                source_di_document_id=proposal["document_id"],
                source_evidence_id=proposal["evidence_id"],
                source_canonical_field_id=proposal["canonical_field_id"],
                source_field_key=proposal["field_key"],
                source_fact_version=proposal["source_fact_version"],
                source_document_type_key=proposal["document_type_key"],
                actor_id=actor_id,
                owning_domain_key=owning_domain_key,
                owning_record_reference=owning_record_reference,
            )

    connection.execute(
        text(
            """
            UPDATE auditcore.journey_document_field_correction_proposals
            SET applied_at_utc=now(), updated_at_utc=now(), version_no=version_no+1
            WHERE tenant_id=:tenant_id AND audit_finding_id=:audit_finding_id
            """
        ),
        {"tenant_id": tenant_id, "audit_finding_id": audit_finding_id},
    )
