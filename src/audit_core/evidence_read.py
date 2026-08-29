from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_engine, get_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.di_lineage import DiLineageFact, get_document_facts_with_lineage
from audit_core.errors import NotFoundError
from audit_core.evidence import (
    EvidenceResponse,
    _dependency_error,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.security import Principal
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["evidence"])
_DI_AUDIENCE = "di"


class EvidenceFactResponse(BaseModel):
    evidenceFactId: UUID
    fieldKey: str
    factRole: str
    valueType: str
    value: Any
    normalizedValue: str | None
    confidenceScore: float | None
    verificationStatus: str | None
    fetchedAtUtc: str


class EvidenceDetailResponse(EvidenceResponse):
    facts: list[EvidenceFactResponse]


def _evidence_row(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
):
    row = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.journey_id, e.customer_id,
                   e.di_subject_id, e.di_document_id,
                   e.document_type_key, e.evidence_purpose,
                   e.processing_status_cache, e.verification_status_cache,
                   e.confirmation_status_cache, e.linked_at_utc,
                   j.dealer_id, j.outlet_id
            FROM auditcore.evidence e
            JOIN auditcore.journeys j
              ON j.tenant_id = e.tenant_id AND j.journey_id = e.journey_id
            WHERE e.tenant_id = :tenant_id
              AND e.journey_id = :journey_id
              AND e.evidence_id = :evidence_id
              AND e.association_status = 'ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
        },
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Evidence not found",
            detail="Evidence not found for the requested Journey.",
        )
    return row


def _authorize_evidence_scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    dealer_id: UUID,
    outlet_id: UUID,
) -> None:
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=dealer_id,
        outlet_id=outlet_id,
    )


def _public_evidence(row) -> EvidenceResponse:
    return EvidenceResponse(
        evidenceId=row["evidence_id"],
        journeyId=row["journey_id"],
        documentTypeKey=row["document_type_key"],
        evidencePurpose=row["evidence_purpose"],
        processingStatus=row["processing_status_cache"] or "UNKNOWN",
        verificationStatus=row["verification_status_cache"],
        createdAtUtc=row["linked_at_utc"].isoformat(),
    )


def _fact_rows(
    connection: Connection,
    *,
    tenant_id: str,
    evidence_id: UUID,
) -> list[EvidenceFactResponse]:
    rows = connection.execute(
        text(
            """
            SELECT evidence_fact_id, field_key, fact_role,
                   value_type, value_json, normalized_value,
                   confidence_score, verification_status, fetched_at_utc
            FROM auditcore.evidence_facts
            WHERE tenant_id = :tenant_id
              AND evidence_id = :evidence_id
              AND superseded_at_utc IS NULL
            ORDER BY field_key, fact_role, evidence_fact_id
            """
        ),
        {"tenant_id": tenant_id, "evidence_id": evidence_id},
    ).mappings().all()
    return [
        EvidenceFactResponse(
            evidenceFactId=row["evidence_fact_id"],
            fieldKey=row["field_key"],
            factRole=row["fact_role"],
            valueType=row["value_type"],
            value=row["value_json"],
            normalizedValue=row["normalized_value"],
            confidenceScore=(
                float(row["confidence_score"])
                if row["confidence_score"] is not None
                else None
            ),
            verificationStatus=row["verification_status"],
            fetchedAtUtc=row["fetched_at_utc"].isoformat(),
        )
        for row in rows
    ]


def _value_projection(value: Any) -> tuple[str, Any, str | None]:
    if isinstance(value, bool):
        return "BOOLEAN", value, "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return "NUMBER", value, str(value)
    if isinstance(value, str):
        return "TEXT", value, value
    return "JSON", value, None


def _persist_refresh(
    engine: Engine,
    *,
    tenant_id: str,
    evidence_id: UUID,
    journey_id: UUID,
    document,
    facts: tuple[DiLineageFact, ...],
) -> EvidenceDetailResponse:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        row = _evidence_row(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=evidence_id,
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence
                SET processing_status_cache = :processing_status,
                    verification_status_cache = :verification_status,
                    confirmation_status_cache = :confirmation_status,
                    cache_updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND evidence_id = :evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "evidence_id": evidence_id,
                "processing_status": document.processing_status,
                "verification_status": document.verification_state,
                "confirmation_status": document.confirmation_status,
            },
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence_facts
                SET superseded_at_utc = now()
                WHERE tenant_id = :tenant_id
                  AND evidence_id = :evidence_id
                  AND superseded_at_utc IS NULL
                """
            ),
            {"tenant_id": tenant_id, "evidence_id": evidence_id},
        )
        for fact in facts:
            value_type, value_json, normalized_value = _value_projection(fact.value)
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.evidence_facts (
                        tenant_id, evidence_id, journey_id,
                        field_key, fact_role,
                        value_type, value_json, normalized_value,
                        confidence_score, di_field_reference, verification_status,
                        di_value_version_no, di_extracted_fact_id,
                        di_processing_run_id, di_extraction_profile_id,
                        di_extraction_profile_version, di_invocation_id,
                        di_pipeline_version
                    ) VALUES (
                        :tenant_id, :evidence_id, :journey_id,
                        :field_key, :fact_role,
                        :value_type, CAST(:value_json AS jsonb), :normalized_value,
                        :confidence_score, :di_field_reference, :verification_status,
                        :di_value_version_no, :di_extracted_fact_id,
                        :di_processing_run_id, :di_extraction_profile_id,
                        :di_extraction_profile_version, :di_invocation_id,
                        :di_pipeline_version
                    )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "evidence_id": evidence_id,
                    "journey_id": journey_id,
                    "field_key": fact.field_key,
                    "fact_role": fact.fact_role,
                    "value_type": value_type,
                    "value_json": json.dumps(value_json),
                    "normalized_value": normalized_value,
                    "confidence_score": fact.confidence_score,
                    "di_field_reference": fact.canonical_field_id,
                    "verification_status": document.verification_state,
                    "di_value_version_no": fact.version_no,
                    "di_extracted_fact_id": fact.extracted_fact_id,
                    "di_processing_run_id": fact.processing_run_id,
                    "di_extraction_profile_id": fact.extraction_profile_id,
                    "di_extraction_profile_version": fact.extraction_profile_version,
                    "di_invocation_id": fact.invocation_id,
                    "di_pipeline_version": fact.pipeline_version,
                },
            )
        refreshed = dict(row)
        refreshed["processing_status_cache"] = document.processing_status
        refreshed["verification_status_cache"] = document.verification_state
        return EvidenceDetailResponse(
            **_public_evidence(refreshed).model_dump(),
            facts=_fact_rows(
                connection,
                tenant_id=tenant_id,
                evidence_id=evidence_id,
            ),
        )


@router.get(
    "/journeys/{journey_id}/evidence",
    response_model=list[EvidenceResponse],
)
def list_journey_evidence(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[EvidenceResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.evidence.read")
    set_tenant_context(connection, tenant_id)
    journey = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if journey is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found for the requested tenant.",
        )
    _authorize_evidence_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )
    rows = connection.execute(
        text(
            """
            SELECT evidence_id, journey_id, document_type_key, evidence_purpose,
                   processing_status_cache, verification_status_cache, linked_at_utc
            FROM auditcore.evidence
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND association_status = 'ACTIVE'
            ORDER BY linked_at_utc DESC, evidence_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [_public_evidence(row) for row in rows]


@router.get(
    "/journeys/{journey_id}/evidence/{evidence_id}",
    response_model=EvidenceDetailResponse,
)
def get_journey_evidence(
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> EvidenceDetailResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.evidence.read")
    set_tenant_context(connection, tenant_id)
    row = _evidence_row(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
    )
    _authorize_evidence_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=row["dealer_id"],
        outlet_id=row["outlet_id"],
    )
    return EvidenceDetailResponse(
        **_public_evidence(row).model_dump(),
        facts=_fact_rows(connection, tenant_id=tenant_id, evidence_id=evidence_id),
    )


@router.get(
    "/journeys/{journey_id}/evidence/{evidence_id}/facts",
    response_model=list[EvidenceFactResponse],
)
def get_journey_evidence_facts(
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[EvidenceFactResponse]:
    detail = get_journey_evidence(
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
        principal=principal,
        connection=connection,
    )
    return detail.facts


@router.post(
    "/journeys/{journey_id}/evidence/{evidence_id}/refresh",
    response_model=EvidenceDetailResponse,
)
def refresh_journey_evidence(
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[
        SecurityOAuthClient,
        Depends(get_security_oauth_client),
    ],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> EvidenceDetailResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.evidence.refresh")
    set_tenant_context(connection, tenant_id)
    row = _evidence_row(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
    )
    _authorize_evidence_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=row["dealer_id"],
        outlet_id=row["outlet_id"],
    )
    try:
        token = security_client.get_service_token(audience=_DI_AUDIENCE)
        document = di_client.get_document(
            token=token,
            tenant_id=tenant_id,
            subject_id=str(row["di_subject_id"]),
            document_id=str(row["di_document_id"]),
        )
        facts = get_document_facts_with_lineage(
            di_client,
            token=token,
            tenant_id=tenant_id,
            subject_id=str(row["di_subject_id"]),
            document_id=str(row["di_document_id"]),
        )
    except (DiClientError, SecurityTokenError) as exc:
        raise _dependency_error(exc) from exc

    return _persist_refresh(
        engine,
        tenant_id=tenant_id,
        evidence_id=evidence_id,
        journey_id=journey_id,
        document=document,
        facts=facts,
    )
