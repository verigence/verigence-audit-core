from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_bearer_token, get_connection, get_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import Principal
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["evidence"])


class EvidenceResponse(BaseModel):
    evidenceId: UUID
    journeyId: UUID
    documentTypeKey: str | None
    evidencePurpose: str
    processingStatus: str
    verificationStatus: str | None
    createdAtUtc: str


def get_security_oauth_client() -> Iterator[SecurityOAuthClient]:
    base_url = os.environ.get("SECURITY_BASE_URL", "").strip()
    client_id = os.environ.get("SECURITY_CLIENT_ID", "").strip()
    client_secret = os.environ.get("SECURITY_CLIENT_SECRET", "")
    if not base_url or not client_id or not client_secret:
        raise RuntimeError("Security OAuth integration is not configured")
    with SecurityOAuthClient(
        base_url=base_url,
        client_id=client_id,
        client_secret=client_secret,
    ) as client:
        yield client


def get_di_client() -> Iterator[DiClient]:
    base_url = os.environ.get("DI_BASE_URL", "").strip()
    if not base_url:
        raise RuntimeError("DI integration is not configured")
    with DiClient(base_url=base_url) as client:
        yield client


def _journey_context(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT j.customer_id, j.dealer_id, j.outlet_id, c.display_name
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
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


def _requirement_id(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    requirement_key: str | None,
) -> UUID | None:
    if requirement_key is None:
        return None
    requirement_id = connection.execute(
        text(
            """
            SELECT journey_document_requirement_id
            FROM auditcore.journey_document_requirements
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND requirement_key = :requirement_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "requirement_key": requirement_key,
        },
    ).scalar_one_or_none()
    if requirement_id is None:
        raise AuditCoreError(
            error_code="VAC-VAL-003",
            status_code=400,
            title="Unsupported evidence",
            detail="The evidence requirement is not configured for this Journey.",
        )
    return requirement_id


def _resolve_di_subject(
    connection: Connection,
    *,
    tenant_id: str,
    customer_id: UUID,
    display_name: str,
    bearer_token: str,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
) -> UUID:
    existing = connection.execute(
        text(
            """
            SELECT di_subject_id
            FROM auditcore.di_subject_mappings
            WHERE tenant_id = :tenant_id
              AND customer_id = :customer_id
              AND mapping_status = 'ACTIVE'
            """
        ),
        {"tenant_id": tenant_id, "customer_id": customer_id},
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    token = security_client.exchange_user_token(
        subject_token=bearer_token,
        permissions=["di.subject.create"],
    )
    # Audit Core customer-type codes do not have an approved PERSON/ORGANIZATION
    # mapping. Use DI's generic OTHER type rather than guessing a business classification.
    subject = di_client.create_subject(
        token=token,
        tenant_id=tenant_id,
        subject_type="OTHER",
        display_name=display_name,
    )
    subject_id = UUID(subject.subject_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.di_subject_mappings (
                tenant_id, customer_id, di_subject_id, di_subject_type
            ) VALUES (
                :tenant_id, :customer_id, :di_subject_id, 'OTHER'
            )
            """
        ),
        {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
            "di_subject_id": subject_id,
        },
    )
    return subject_id


def _dependency_error(exc: Exception) -> AuditCoreError:
    if isinstance(exc, DiClientError):
        if exc.code == "DI_UNAVAILABLE" or exc.status_code >= 500 and exc.retryable:
            return AuditCoreError(
                error_code="VAC-DI-001",
                status_code=503,
                title="Document intelligence unavailable",
                detail="Document intelligence is temporarily unavailable.",
            )
        if 400 <= exc.status_code < 500:
            return AuditCoreError(
                error_code="VAC-DI-002",
                status_code=422,
                title="Document rejected",
                detail="Document intelligence rejected the supplied evidence.",
            )
        return AuditCoreError(
            error_code="VAC-DI-004",
            status_code=502,
            title="Document intelligence error",
            detail="Document intelligence returned an invalid or unsupported response.",
        )
    return AuditCoreError(
        error_code="VAC-SYS-002",
        status_code=503,
        title="Dependency unavailable",
        detail="Security could not authorize the downstream document operation.",
    )


@router.post(
    "/journeys/{journey_id}/evidence",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_journey_evidence(
    tenant_id: str,
    journey_id: UUID,
    file: Annotated[UploadFile, File()],
    evidence_purpose: Annotated[str, Form(alias="evidencePurpose", min_length=1, max_length=160)],
    principal: Annotated[Principal, Depends(get_principal)],
    bearer_token: Annotated[str, Depends(get_bearer_token)],
    connection: Annotated[Connection, Depends(get_connection)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    requirement_key: Annotated[
        str | None, Form(alias="requirementKey", max_length=120)
    ] = None,
    document_type_key: Annotated[
        str | None, Form(alias="documentTypeKey", max_length=120)
    ] = None,
) -> EvidenceResponse:
    del idempotency_key  # Durable replay/recovery is implemented in G-04.
    authorize(principal, tenant_id=tenant_id, permission="audit.evidence.upload")
    set_tenant_context(connection, tenant_id)
    journey = _journey_context(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )
    requirement_id = _requirement_id(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        requirement_key=requirement_key,
    )

    try:
        subject_id = _resolve_di_subject(
            connection,
            tenant_id=tenant_id,
            customer_id=journey["customer_id"],
            display_name=journey["display_name"],
            bearer_token=bearer_token,
            security_client=security_client,
            di_client=di_client,
        )
        upload_token = security_client.exchange_user_token(
            subject_token=bearer_token,
            permissions=["di.document.upload"],
        )
        content = file.file.read()
        document = di_client.upload_document(
            token=upload_token,
            tenant_id=tenant_id,
            subject_id=str(subject_id),
            filename=file.filename or "evidence",
            content=content,
            content_type=file.content_type or "application/octet-stream",
            source_channel="API",
            document_type_key=document_type_key,
        )
    except (DiClientError, SecurityTokenError) as exc:
        raise _dependency_error(exc) from exc

    row = connection.execute(
        text(
            """
            INSERT INTO auditcore.evidence (
                tenant_id, journey_id, customer_id,
                journey_document_requirement_id,
                di_subject_id, di_document_id,
                document_type_key, evidence_purpose,
                processing_status_cache, verification_status_cache,
                confirmation_status_cache, cache_updated_at_utc,
                linked_by_actor_id
            ) VALUES (
                :tenant_id, :journey_id, :customer_id,
                :requirement_id,
                :di_subject_id, :di_document_id,
                :document_type_key, :evidence_purpose,
                :processing_status, :verification_status,
                :confirmation_status, now(),
                :actor_id
            )
            RETURNING evidence_id, linked_at_utc
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "customer_id": journey["customer_id"],
            "requirement_id": requirement_id,
            "di_subject_id": subject_id,
            "di_document_id": UUID(document.document_id),
            "document_type_key": document_type_key,
            "evidence_purpose": evidence_purpose,
            "processing_status": document.processing_status,
            "verification_status": document.verification_state,
            "confirmation_status": document.confirmation_status,
            "actor_id": principal.subject,
        },
    ).mappings().one()

    return EvidenceResponse(
        evidenceId=row["evidence_id"],
        journeyId=journey_id,
        documentTypeKey=document_type_key,
        evidencePurpose=evidence_purpose,
        processingStatus=document.processing_status,
        verificationStatus=document.verification_state,
        createdAtUtc=row["linked_at_utc"].isoformat(),
    )
