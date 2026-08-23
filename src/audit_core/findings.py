from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["findings"])


class FindingEvidenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    evidenceId: UUID
    evidenceFactId: UUID | None = None
    linkagePurpose: str | None = Field(default=None, max_length=160)


class FindingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    auditEvaluationId: UUID | None = None
    findingTypeCode: str | None = Field(default=None, max_length=100)
    severity: str = Field(default="MEDIUM", max_length=30)
    title: str = Field(max_length=300)
    description: str | None = None
    expectedSummary: str | None = None
    observedSummary: str | None = None
    evidence: list[FindingEvidenceInput] = Field(default_factory=list)


class FindingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findingStatus: Literal["OPEN", "ACKNOWLEDGED", "RESOLVED", "VOIDED"] | None = None
    severity: str | None = Field(default=None, max_length=30)
    title: str | None = Field(default=None, max_length=300)
    description: str | None = None
    expectedSummary: str | None = None
    observedSummary: str | None = None
    resolutionReason: str | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not self.model_fields_set:
            raise ValueError("At least one finding field is required")
        return self


class FindingEvidenceResponse(BaseModel):
    findingEvidenceId: UUID
    evidenceId: UUID
    evidenceFactId: UUID | None
    linkagePurpose: str | None


class FindingResponse(BaseModel):
    auditFindingId: UUID
    journeyId: UUID
    auditEvaluationId: UUID | None
    findingTypeCode: str | None
    severity: str
    findingStatus: str
    title: str
    description: str | None
    expectedSummary: str | None
    observedSummary: str | None
    resolutionReason: str | None
    versionNo: int
    evidence: list[FindingEvidenceResponse]


def _journey_scope(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found for the requested tenant.",
        )
    return row


def _scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> None:
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )


def _validate_evaluation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evaluation_id: UUID,
) -> None:
    exists = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.audit_evaluations
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND audit_evaluation_id = :evaluation_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evaluation_id": evaluation_id,
        },
    ).scalar_one_or_none()
    if exists is None:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Invalid audit evaluation reference",
            detail="Finding evaluation must belong to the same Journey.",
        )


def _link_evidence(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    finding_id: UUID,
    link: FindingEvidenceInput,
) -> None:
    evidence = connection.execute(
        text(
            """
            SELECT 1 FROM auditcore.evidence
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND evidence_id = :evidence_id
              AND association_status = 'ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": link.evidenceId,
        },
    ).scalar_one_or_none()
    if evidence is None:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Invalid evidence reference",
            detail="Finding evidence must belong to the same Journey.",
        )
    if link.evidenceFactId is not None:
        fact = connection.execute(
            text(
                """
                SELECT 1 FROM auditcore.evidence_facts
                WHERE tenant_id = :tenant_id
                  AND evidence_id = :evidence_id
                  AND evidence_fact_id = :fact_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "evidence_id": link.evidenceId,
                "fact_id": link.evidenceFactId,
            },
        ).scalar_one_or_none()
        if fact is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Invalid evidence fact reference",
                detail="Finding evidence fact must belong to the linked evidence.",
            )
    connection.execute(
        text(
            """
            INSERT INTO auditcore.finding_evidence (
                tenant_id, audit_finding_id, evidence_id,
                evidence_fact_id, linkage_purpose
            ) VALUES (
                :tenant_id, :finding_id, :evidence_id,
                :fact_id, :purpose
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "finding_id": finding_id,
            "evidence_id": link.evidenceId,
            "fact_id": link.evidenceFactId,
            "purpose": link.linkagePurpose,
        },
    )


def _response(connection: Connection, tenant_id: str, finding_id: UUID) -> FindingResponse:
    row = connection.execute(
        text(
            """
            SELECT audit_finding_id, journey_id, audit_evaluation_id,
                   finding_type_code, severity, finding_status, title,
                   description, expected_summary, observed_summary,
                   resolution_reason, version_no
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id
            """
        ),
        {"tenant_id": tenant_id, "finding_id": finding_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-014",
            title="Finding not found",
            detail="Finding not found for the requested tenant.",
        )
    links = connection.execute(
        text(
            """
            SELECT finding_evidence_id, evidence_id,
                   evidence_fact_id, linkage_purpose
            FROM auditcore.finding_evidence
            WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id
            ORDER BY created_at_utc, finding_evidence_id
            """
        ),
        {"tenant_id": tenant_id, "finding_id": finding_id},
    ).mappings().all()
    return FindingResponse(
        auditFindingId=row["audit_finding_id"],
        journeyId=row["journey_id"],
        auditEvaluationId=row["audit_evaluation_id"],
        findingTypeCode=row["finding_type_code"],
        severity=row["severity"],
        findingStatus=row["finding_status"],
        title=row["title"],
        description=row["description"],
        expectedSummary=row["expected_summary"],
        observedSummary=row["observed_summary"],
        resolutionReason=row["resolution_reason"],
        versionNo=row["version_no"],
        evidence=[
            FindingEvidenceResponse(
                findingEvidenceId=link["finding_evidence_id"],
                evidenceId=link["evidence_id"],
                evidenceFactId=link["evidence_fact_id"],
                linkagePurpose=link["linkage_purpose"],
            )
            for link in links
        ],
    )


@router.get("/journeys/{journey_id}/findings", response_model=list[FindingResponse])
def list_findings(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[FindingResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.finding.read")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    ids = connection.execute(
        text(
            """
            SELECT audit_finding_id
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc, audit_finding_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    return [_response(connection, tenant_id, finding_id) for finding_id in ids]


@router.post("/journeys/{journey_id}/findings", response_model=FindingResponse, status_code=201)
def create_finding(
    tenant_id: str,
    journey_id: UUID,
    payload: FindingCreate,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FindingResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.finding.create")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    if payload.auditEvaluationId is not None:
        _validate_evaluation(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evaluation_id=payload.auditEvaluationId,
        )
    finding_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.audit_findings (
                tenant_id, journey_id, audit_evaluation_id, finding_type_code,
                severity, title, description, expected_summary,
                observed_summary, created_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, :evaluation_id, :finding_type_code,
                :severity, :title, :description, :expected_summary,
                :observed_summary, :actor_id
            ) RETURNING audit_finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evaluation_id": payload.auditEvaluationId,
            "finding_type_code": payload.findingTypeCode,
            "severity": payload.severity,
            "title": payload.title,
            "description": payload.description,
            "expected_summary": payload.expectedSummary,
            "observed_summary": payload.observedSummary,
            "actor_id": principal.subject,
        },
    ).scalar_one()
    for link in payload.evidence:
        _link_evidence(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            finding_id=finding_id,
            link=link,
        )
    return _response(connection, tenant_id, finding_id)


@router.patch(
    "/journeys/{journey_id}/findings/{finding_id}",
    response_model=FindingResponse,
)
def patch_finding(
    tenant_id: str,
    journey_id: UUID,
    finding_id: UUID,
    payload: FindingPatch,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FindingResponse:
    permission = (
        "audit.finding.resolve"
        if payload.findingStatus in {"RESOLVED", "VOIDED"}
        else "audit.finding.update"
    )
    authorize(principal, tenant_id=tenant_id, permission=permission)
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    finding = connection.execute(
        text(
            """
            SELECT stage_code
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND audit_finding_id = :finding_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "finding_id": finding_id,
        },
    ).mappings().one_or_none()
    if finding is None:
        raise NotFoundError(
            error_code="VAC-NF-014",
            title="Finding not found",
            detail="Finding not found for the requested Journey.",
        )
    if finding["stage_code"] in {"BOOKING", "DELIVERY"}:
        raise ConflictError(
            error_code="VAC-CONFLICT-010",
            title="UC03 audit flag lifecycle required",
            detail="Booking and Delivery audit flags must be changed through the auditable UC03 review actions.",
        )

    column_by_field = {
        "findingStatus": "finding_status",
        "severity": "severity",
        "title": "title",
        "description": "description",
        "expectedSummary": "expected_summary",
        "observedSummary": "observed_summary",
        "resolutionReason": "resolution_reason",
    }
    assignments = [f"{column_by_field[field]} = :{field}" for field in payload.model_fields_set]
    assignments.extend(["updated_at_utc = now()", "version_no = version_no + 1"])
    params = {field: getattr(payload, field) for field in payload.model_fields_set}
    params.update({"tenant_id": tenant_id, "finding_id": finding_id})
    connection.execute(
        text(
            "UPDATE auditcore.audit_findings SET "
            + ", ".join(assignments)
            + " WHERE tenant_id = :tenant_id AND audit_finding_id = :finding_id"
        ),
        params,
    )
    return _response(connection, tenant_id, finding_id)
