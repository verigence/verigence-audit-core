"""uc03_document_field_corrections.py — document field correction flow
(unified Documents review redesign, 2026-09-13; confidence split added
2026-09-13; reworked onto the Task Queue 2026-09-17, direct user
correction -- see below).

Booking/Delivery's own Confirm gates were relaxed so document completeness
alone finishes the stage (uc03_simplified_booking_flow.py,
uc03_confidence_review_policy.py, uc03_review_effective_values.py). This
module is the ONE place a PC/TL corrects a DI-extracted field's value from
the standalone Documents page, branching on confidence
(``uc03_review_confidence.requires_pc_review``, the same 90% threshold used
everywhere else):

  <90% (or missing confidence): the value is WRONG BY DEFINITION OF LOW
  TRUST -- apply it immediately (self-serve, exactly like a PC always could
  on the old Review page). Nothing is pending once this returns -- no
  finding, no task, just this module's own ``journey_document_field_
  correction_proposals`` row as the audit-trail record of what changed
  and when, searchable by journey_id like every other row here.

  >=90%: the extracted value is presumed trustworthy, so overwriting it
  needs a second pair of eyes -- creates an ordinary ``workflow_tasks`` row
  (task_type ``FIELD_CORRECTION_REVIEW``, assigned to TL) instead of
  applying anything, and defers the write until a Team Lead Completes it
  (`apply_confirmed_field_correction`, invoked from tasks_api.py's generic
  Complete-task action). Cancelling the task needs no equivalent hook --
  the original DI value simply stands.

Direct user correction of this module's first cut (2026-09-13), which
raised an ``audit_findings`` row for BOTH bands and adjudicated the >=90%
one through the finding-verdict machinery -- mirroring the exact mistake
``uc03_model_selection_corrections.py`` made and then fixed one migration
later (0102 -> 0103). A PC/TL-proposed field correction is a human review-
and-decide (or self-serve) workflow item, not a rule-detected violation or
a compliance gap: Audit Review stays reserved for what a rule actually
found wrong with the business process, and the Task Queue already gives
PC/TL/PM full workflow visibility for a given Journey. See migration
0106_field_correction_as_task for the schema side of this change --
historical >=90% findings raised before this fix keep resolving through
the existing CONFIRM_BREACH/MARK_FALSE_POSITIVE path
(uc03_audit_flags.py::act_on_flag's own hook, kept unchanged for exactly
that backward-compatibility reason).

Why this module does NOT call the stage-wide Confirm endpoints
(confirm_booking_review_v2_confidence_policy / confirm_delivery_review_v2_
effective_values, uc03_review_effective_values.py): those two exist for a
different job -- the ONE-TIME "PC finishes reviewing everything, stage
becomes VERIFIED" transition (pc_verification_status PENDING -> VERIFIED,
enforced exactly once). Routing a single-field, single-document correction
through them would (a) flip pc_verification_status to VERIFIED the first
time ANY field on ANY document got corrected -- silently declaring the
whole stage "PC verified" off the back of one field -- and (b) permanently
lock out every later correction on every OTHER document, since that
transition can only happen once (a second call fails with
VAC-CONFLICT-010, "Review is not pending"). A per-document correction is a
narrower, always-repeatable action; it needs its own narrow, always-
repeatable write path, which is exactly what `_apply_field_value` below is.
It reuses `persist_reviewed_di_fields()` -- the same durable write 5
existing Confirm handlers already use -- plus the same conditional typed-
attribute projection those handlers use (`apply_supported_operational_
attribute` / `record_attribute_resolution`), deliberately NOT the heavier
`materialize_reviewed_di_business_values` journey-wide re-materialization
pass (disproportionate to one field on one document either way).

Every correction, whichever band, gets its own ``journey_document_field_
correction_proposals`` row recording old/new value -- keyed by
``workflow_task_id`` for a new >=90% row (NULL for a new <90% row, since
nothing is pending for it), or by the legacy ``audit_finding_id`` for a
row created before this fix. The ONLY difference between the two live
bands is whether `applied_at_utc` is stamped at creation (<90%, self-serve)
or left NULL until a TL Completes the task (>=90%, adjudicated).
"""
from __future__ import annotations

import json
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
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
from audit_core.uc03_audit_flags import _scope
from audit_core.uc03_di_core_persistence import (
    ReviewedDiField,
    persist_reviewed_di_fields,
)
from audit_core.uc03_review_confidence import requires_pc_review
from audit_core.workflow import create_workflow_task

router = APIRouter(
    prefix="/v2/tenants/{tenant_id}/journeys/{journey_id}/uc03/documents",
    tags=["uc03-document-field-corrections"],
)

StageCode = Literal["BOOKING", "DELIVERY"]

TASK_TYPE = "FIELD_CORRECTION_REVIEW"
_WORKFLOW_TYPE = "UC03_FIELD_CORRECTION"


class FieldCorrectionCommand(BaseModel):
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
    newValue: Any | None = Field(...)
    # Required only for a >=90% correction (adjudicated -- a TL needs to know
    # why a trusted value is being challenged). Optional for <90%, where the
    # correction applies immediately and a remark would just add friction to
    # something a PC could always do directly on the old Review page.
    remarks: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_remarks_for_adjudicated_corrections(self):
        if requires_pc_review(self.confidenceScore):
            return self
        if not (self.remarks or "").strip():
            raise ValueError(
                "Remarks are required when correcting a value at or above "
                "90% confidence -- a Team Lead needs to know why."
            )
        return self


class FieldCorrectionResult(BaseModel):
    documentId: UUID
    fieldKey: str
    # True: applied immediately (<90% confidence), nothing pending. False:
    # a FIELD_CORRECTION_REVIEW task was raised instead (taskId set) -- the
    # value is not yet written anywhere.
    applied: bool
    taskId: UUID | None = None


def _json(value: Any) -> str | None:
    return json.dumps(value, default=str)


def _insert_proposal(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    command: FieldCorrectionCommand,
    actor_id: str,
    workflow_task_id: UUID | None,
    applied_now: bool,
) -> None:
    connection.execute(
        text(
            """
            INSERT INTO auditcore.journey_document_field_correction_proposals (
                tenant_id, workflow_task_id, journey_id, stage_code,
                document_id, evidence_id, document_type_key, field_key,
                canonical_field_id, source_fact_version, confidence_score,
                original_value, proposed_value, proposed_by_actor_id,
                applied_at_utc
            ) VALUES (
                :tenant_id, :workflow_task_id, :journey_id, :stage_code,
                :document_id, :evidence_id, :document_type_key, :field_key,
                :canonical_field_id, :source_fact_version, :confidence_score,
                CAST(:original_value AS jsonb), CAST(:proposed_value AS jsonb),
                :actor_id,
                CASE WHEN :applied_now THEN now() ELSE NULL END
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "workflow_task_id": workflow_task_id,
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
            "proposed_value": _json(command.newValue),
            "actor_id": actor_id,
            "applied_now": applied_now,
        },
    )


def _apply_field_value(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    stage_code: str,
    document_id: UUID,
    evidence_id: UUID | None,
    document_type_key: str | None,
    field_key: str,
    canonical_field_id: str | None,
    source_fact_version: int,
    confidence_score: float | None,
    original_value: Any,
    new_value: Any,
    actor_id: str,
) -> None:
    """Write ``new_value`` as the field's new reviewed/effective value, plus
    the same conditional typed-attribute projection the stage-wide Confirm
    handlers use. Shared by both the immediate <90% apply path and the
    >=90% Complete-task apply path -- identical write, different caller."""

    reviewed_field = ReviewedDiField(
        document_id=document_id,
        evidence_id=evidence_id,
        source_canonical_field_id=canonical_field_id,
        source_document_type_key=document_type_key,
        field_key=field_key,
        source_fact_version=source_fact_version,
        extracted_value=original_value,
        modified_value=new_value,
        effective_value=new_value,
        is_modified=True,
        confidence_score=confidence_score,
        confidence_scale="PERCENT" if confidence_score is not None else None,
    )
    persist_reviewed_di_fields(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=stage_code,
        actor_id=actor_id,
        fields=[reviewed_field],
    )

    spec = spec_for_field(field_key)
    if spec is not None and spec.mapping_status == "SUPPORTED":
        application = apply_supported_operational_attribute(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            spec=spec,
            value=new_value,
            actor_id=actor_id,
            source_document_type_key=document_type_key,
            source_field_key=field_key,
            source_evidence_id=evidence_id,
        )
        if application is not None:
            owning_domain_key, owning_record_reference, _status = application
            record_attribute_resolution(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                stage_code=stage_code,
                spec=spec,
                source_di_document_id=document_id,
                source_evidence_id=evidence_id,
                source_canonical_field_id=canonical_field_id,
                source_field_key=field_key,
                source_fact_version=source_fact_version,
                source_document_type_key=document_type_key,
                actor_id=actor_id,
                owning_domain_key=owning_domain_key,
                owning_record_reference=owning_record_reference,
            )


@router.post(
    "/{document_id}/field-corrections",
    response_model=FieldCorrectionResult,
)
def submit_field_correction(
    tenant_id: str,
    journey_id: UUID,
    document_id: UUID,
    payload: FieldCorrectionCommand,
    request: Request,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=200)
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FieldCorrectionResult:
    if payload.documentId != document_id:
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Document not found",
            detail="The document in the URL does not match the correction payload.",
        )
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        # Kept from before this module moved off Audit Review -- still the
        # right role policy (PC/TL/PM/EXECUTIVE may propose a correction),
        # just no longer tied to "create a finding" in what it actually does.
        operation="PROPOSE_CORRECTION",
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
    # requires_pc_review returns True for a LOW-confidence field (self-serve,
    # apply immediately -- see module docstring); a >=90% field needs the
    # opposite, TL-adjudicated treatment.
    needs_adjudication = requires_pc_review(payload.confidenceScore) is False

    def execute() -> dict[str, Any]:
        if needs_adjudication:
            task_id = create_workflow_task(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                workflow_type=_WORKFLOW_TYPE,
                process_area=payload.stage,
                task_type=TASK_TYPE,
                assigned_role_code="TL",
                task_payload={
                    "fieldKey": payload.fieldKey,
                    "documentTypeKey": payload.documentTypeKey,
                    "originalValue": payload.originalValue,
                    "proposedValue": payload.newValue,
                    "comment": (payload.remarks or "").strip(),
                },
                correlation_id=get_correlation_id(request),
            )
            _insert_proposal(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                command=payload,
                actor_id=human_principal.subject,
                workflow_task_id=task_id,
                applied_now=False,
            )
            return FieldCorrectionResult(
                documentId=payload.documentId,
                fieldKey=payload.fieldKey,
                applied=False,
                taskId=task_id,
            ).model_dump(mode="json")

        # <90% confidence (or missing): self-serve, apply immediately -- see
        # module docstring for why this bypasses the stage-wide Confirm path.
        _apply_field_value(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            stage_code=payload.stage,
            document_id=payload.documentId,
            evidence_id=payload.evidenceId,
            document_type_key=payload.documentTypeKey,
            field_key=payload.fieldKey,
            canonical_field_id=payload.canonicalFieldId,
            source_fact_version=payload.sourceFactVersion,
            confidence_score=payload.confidenceScore,
            original_value=payload.originalValue,
            new_value=payload.newValue,
            actor_id=human_principal.subject,
        )
        _insert_proposal(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            command=payload,
            actor_id=human_principal.subject,
            workflow_task_id=None,
            applied_now=True,
        )
        return FieldCorrectionResult(
            documentId=payload.documentId,
            fieldKey=payload.fieldKey,
            applied=True,
            taskId=None,
        ).model_dump(mode="json")

    body, _replay = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.document-field-correction.submit:{journey_id}:{document_id}:{payload.fieldKey}:{payload.sourceFactVersion}",
        idempotency_key=idempotency_key,
        request_payload=payload.model_dump(mode="json"),
        execute=execute,
    )
    return FieldCorrectionResult.model_validate(body)


def _apply_proposal(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
    proposal: dict[str, Any],
) -> None:
    _apply_field_value(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        stage_code=proposal["stage_code"],
        document_id=proposal["document_id"],
        evidence_id=proposal["evidence_id"],
        document_type_key=proposal["document_type_key"],
        field_key=proposal["field_key"],
        canonical_field_id=proposal["canonical_field_id"],
        source_fact_version=proposal["source_fact_version"],
        confidence_score=proposal["confidence_score"],
        original_value=proposal["original_value"],
        new_value=proposal["proposed_value"],
        actor_id=actor_id,
    )


_PROPOSAL_COLUMNS = """
    stage_code, document_id, evidence_id, document_type_key,
    field_key, canonical_field_id, source_fact_version,
    confidence_score, original_value, proposed_value
"""


def apply_confirmed_field_correction(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    workflow_task_id: UUID,
    actor_id: str,
) -> None:
    """Write the proposed value as the field's new reviewed/effective value.

    Called from tasks_api.py's generic Complete-task action when the task
    being completed is a FIELD_CORRECTION_REVIEW -- the ordinary "TL
    completes a task" gesture is what actually applies the correction here,
    exactly matching uc03_model_selection_corrections.py's own Complete
    hook. Cancelling the task (the ordinary "reject" gesture) needs no
    equivalent hook: the original value simply stands. The <90% path never
    reaches here -- its value was already applied at submit_field_
    correction time.
    """
    proposal = connection.execute(
        text(
            f"""
            SELECT {_PROPOSAL_COLUMNS}
            FROM auditcore.journey_document_field_correction_proposals
            WHERE tenant_id=:tenant_id AND workflow_task_id=:workflow_task_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "workflow_task_id": workflow_task_id},
    ).mappings().one_or_none()
    if proposal is None:
        # Defensive only: every FIELD_CORRECTION_REVIEW task is created with
        # its proposal row in the same transaction (submit_field_correction
        # above) -- this should be unreachable in practice.
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Correction proposal not found",
            detail="No proposed correction is recorded for this task.",
        )
    _apply_proposal(connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=actor_id, proposal=proposal)
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_document_field_correction_proposals
            SET applied_at_utc=now(), updated_at_utc=now(), version_no=version_no+1
            WHERE tenant_id=:tenant_id AND workflow_task_id=:workflow_task_id
            """
        ),
        {"tenant_id": tenant_id, "workflow_task_id": workflow_task_id},
    )


def apply_confirmed_field_correction_legacy(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    audit_finding_id: UUID,
    actor_id: str,
) -> None:
    """Same write as apply_confirmed_field_correction, keyed by
    audit_finding_id instead of workflow_task_id -- for a
    DI_VALUE_CORRECTION_PROPOSED finding raised before this module moved
    onto the Task Queue (migration 0106_field_correction_as_task). Called
    only from uc03_audit_flags.py::act_on_flag's own CONFIRM_BREACH hook,
    kept exactly for this backward-compatibility case. Safe to delete,
    along with that hook, once no tenant has one of these findings open
    any longer.
    """
    proposal = connection.execute(
        text(
            f"""
            SELECT {_PROPOSAL_COLUMNS}
            FROM auditcore.journey_document_field_correction_proposals
            WHERE tenant_id=:tenant_id AND audit_finding_id=:audit_finding_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "audit_finding_id": audit_finding_id},
    ).mappings().one_or_none()
    if proposal is None:
        raise NotFoundError(
            error_code="VAC-NF-014",
            status_code=404,
            title="Correction proposal not found",
            detail="No proposed correction is recorded for this finding.",
        )
    _apply_proposal(connection, tenant_id=tenant_id, journey_id=journey_id, actor_id=actor_id, proposal=proposal)
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


__all__ = [
    "TASK_TYPE",
    "apply_confirmed_field_correction",
    "apply_confirmed_field_correction_legacy",
    "submit_field_correction",
]
