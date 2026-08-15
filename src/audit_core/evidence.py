from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, Request, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import Connection, Engine, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_bearer_token, get_engine, get_principal
from audit_core.di_client import DiClient, DiClientError, DiDocument
from audit_core.errors import AuditCoreError, ConflictError, NotFoundError
from audit_core.observability import get_correlation_id
from audit_core.security import Principal
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["evidence"])
_OPERATION_KEY = "UPLOAD_JOURNEY_EVIDENCE"


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


def _request_hash(
    *,
    journey_id: UUID,
    evidence_purpose: str,
    requirement_key: str | None,
    document_type_key: str | None,
    filename: str,
    content_type: str,
    content: bytes,
) -> str:
    fingerprint = {
        "journeyId": str(journey_id),
        "evidencePurpose": evidence_purpose,
        "requirementKey": requirement_key,
        "documentTypeKey": document_type_key,
        "filename": filename,
        "contentType": content_type,
        "contentSha256": hashlib.sha256(content).hexdigest(),
    }
    encoded = json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _load_or_create_operation(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    customer_id: UUID,
    idempotency_key: str,
    request_hash: str,
    evidence_purpose: str,
    requirement_key: str | None,
    document_type_key: str | None,
    correlation_id: str,
):
    idempotency = connection.execute(
        text(
            """
            SELECT request_hash, response_status, response_body
            FROM auditcore.idempotency_records
            WHERE tenant_id = :tenant_id
              AND operation_key = :operation_key
              AND idempotency_key = :idempotency_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "operation_key": _OPERATION_KEY,
            "idempotency_key": idempotency_key,
        },
    ).mappings().one_or_none()
    if idempotency is not None:
        if idempotency["request_hash"] != request_hash:
            raise ConflictError(
                error_code="VAC-CONFLICT-003",
                title="Idempotency conflict",
                detail="The Idempotency-Key was already used for a different evidence upload.",
            )
        if idempotency["response_status"] == 201 and isinstance(
            idempotency["response_body"], dict
        ):
            return None, EvidenceResponse.model_validate(idempotency["response_body"])
    else:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.idempotency_records (
                    tenant_id, operation_key, idempotency_key, request_hash
                ) VALUES (
                    :tenant_id, :operation_key, :idempotency_key, :request_hash
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "operation_key": _OPERATION_KEY,
                "idempotency_key": idempotency_key,
                "request_hash": request_hash,
            },
        )

    operation = connection.execute(
        text(
            """
            SELECT evidence_ingestion_operation_id, operation_status,
                   di_subject_id, di_document_id, evidence_id, attempt_count
            FROM auditcore.evidence_ingestion_operations
            WHERE tenant_id = :tenant_id AND idempotency_key = :idempotency_key
            """
        ),
        {"tenant_id": tenant_id, "idempotency_key": idempotency_key},
    ).mappings().one_or_none()
    if operation is None:
        operation = connection.execute(
            text(
                """
                INSERT INTO auditcore.evidence_ingestion_operations (
                    tenant_id, journey_id, customer_id, idempotency_key,
                    evidence_purpose, requirement_key, document_type_key,
                    correlation_id
                ) VALUES (
                    :tenant_id, :journey_id, :customer_id, :idempotency_key,
                    :evidence_purpose, :requirement_key, :document_type_key,
                    :correlation_id
                )
                RETURNING evidence_ingestion_operation_id, operation_status,
                          di_subject_id, di_document_id, evidence_id, attempt_count
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "customer_id": customer_id,
                "idempotency_key": idempotency_key,
                "evidence_purpose": evidence_purpose,
                "requirement_key": requirement_key,
                "document_type_key": document_type_key,
                "correlation_id": correlation_id,
            },
        ).mappings().one()
    return operation, None


def _existing_evidence_response(
    connection: Connection,
    *,
    tenant_id: str,
    evidence_id: UUID,
) -> EvidenceResponse | None:
    row = connection.execute(
        text(
            """
            SELECT evidence_id, journey_id, document_type_key, evidence_purpose,
                   processing_status_cache, verification_status_cache, linked_at_utc
            FROM auditcore.evidence
            WHERE tenant_id = :tenant_id AND evidence_id = :evidence_id
            """
        ),
        {"tenant_id": tenant_id, "evidence_id": evidence_id},
    ).mappings().one_or_none()
    if row is None:
        return None
    return EvidenceResponse(
        evidenceId=row["evidence_id"],
        journeyId=row["journey_id"],
        documentTypeKey=row["document_type_key"],
        evidencePurpose=row["evidence_purpose"],
        processingStatus=row["processing_status_cache"] or "UNKNOWN",
        verificationStatus=row["verification_status_cache"],
        createdAtUtc=row["linked_at_utc"].isoformat(),
    )


def _cache_response(
    connection: Connection,
    *,
    tenant_id: str,
    idempotency_key: str,
    response: EvidenceResponse,
) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.idempotency_records
            SET logical_result_id = :logical_result_id,
                response_status = 201,
                response_body = CAST(:response_body AS jsonb)
            WHERE tenant_id = :tenant_id
              AND operation_key = :operation_key
              AND idempotency_key = :idempotency_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "operation_key": _OPERATION_KEY,
            "idempotency_key": idempotency_key,
            "logical_result_id": str(response.evidenceId),
            "response_body": json.dumps(response.model_dump(mode="json")),
        },
    )


def _subject_mapping(
    connection: Connection,
    *,
    tenant_id: str,
    customer_id: UUID,
) -> UUID | None:
    return connection.execute(
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


def _persist_subject_mapping(
    engine: Engine,
    *,
    tenant_id: str,
    customer_id: UUID,
    subject_id: UUID,
) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        existing = _subject_mapping(
            connection,
            tenant_id=tenant_id,
            customer_id=customer_id,
        )
        if existing is None:
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


def _update_operation(
    engine: Engine,
    *,
    tenant_id: str,
    idempotency_key: str,
    operation_status: str,
    di_subject_id: UUID | None = None,
    di_document_id: UUID | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    increment_attempt: bool = False,
) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence_ingestion_operations
                SET operation_status = :operation_status,
                    di_subject_id = COALESCE(:di_subject_id, di_subject_id),
                    di_document_id = COALESCE(:di_document_id, di_document_id),
                    attempt_count = attempt_count + :attempt_increment,
                    last_error_code = :error_code,
                    last_error_summary = :error_summary,
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND idempotency_key = :idempotency_key
                """
            ),
            {
                "tenant_id": tenant_id,
                "idempotency_key": idempotency_key,
                "operation_status": operation_status,
                "di_subject_id": di_subject_id,
                "di_document_id": di_document_id,
                "attempt_increment": 1 if increment_attempt else 0,
                "error_code": error_code,
                "error_summary": error_summary,
            },
        )


def _dependency_error(exc: Exception) -> AuditCoreError:
    if isinstance(exc, DiClientError):
        if exc.code == "DI_UNAVAILABLE" or (exc.status_code >= 500 and exc.retryable):
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


def _operation_failure_state(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, DiClientError):
        return ("RETRY_WAIT" if exc.retryable else "FAILED", exc.code)
    return "FAILED", "SECURITY_TOKEN_DENIED"


def _document_for_recovery(
    *,
    bearer_token: str,
    security_client: SecurityOAuthClient,
    di_client: DiClient,
    tenant_id: str,
    subject_id: UUID,
    document_id: UUID,
) -> DiDocument:
    token = security_client.exchange_user_token(
        subject_token=bearer_token,
        permissions=["di.document.read"],
    )
    return di_client.get_document(
        token=token,
        tenant_id=tenant_id,
        subject_id=str(subject_id),
        document_id=str(document_id),
    )


def _link_evidence(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    customer_id: UUID,
    requirement_id: UUID | None,
    idempotency_key: str,
    subject_id: UUID,
    document: DiDocument,
    document_type_key: str | None,
    evidence_purpose: str,
    actor_id: str,
) -> EvidenceResponse:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        operation = connection.execute(
            text(
                """
                SELECT operation_status, evidence_id
                FROM auditcore.evidence_ingestion_operations
                WHERE tenant_id = :tenant_id AND idempotency_key = :idempotency_key
                FOR UPDATE
                """
            ),
            {"tenant_id": tenant_id, "idempotency_key": idempotency_key},
        ).mappings().one()
        if operation["evidence_id"] is not None:
            existing = _existing_evidence_response(
                connection,
                tenant_id=tenant_id,
                evidence_id=operation["evidence_id"],
            )
            if existing is None:
                raise RuntimeError("Linked evidence operation references missing evidence")
            _cache_response(
                connection,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                response=existing,
            )
            return existing

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
                "customer_id": customer_id,
                "requirement_id": requirement_id,
                "di_subject_id": subject_id,
                "di_document_id": UUID(document.document_id),
                "document_type_key": document_type_key,
                "evidence_purpose": evidence_purpose,
                "processing_status": document.processing_status,
                "verification_status": document.verification_state,
                "confirmation_status": document.confirmation_status,
                "actor_id": actor_id,
            },
        ).mappings().one()
        response = EvidenceResponse(
            evidenceId=row["evidence_id"],
            journeyId=journey_id,
            documentTypeKey=document_type_key,
            evidencePurpose=evidence_purpose,
            processingStatus=document.processing_status,
            verificationStatus=document.verification_state,
            createdAtUtc=row["linked_at_utc"].isoformat(),
        )
        connection.execute(
            text(
                """
                UPDATE auditcore.evidence_ingestion_operations
                SET operation_status = 'LINKED',
                    evidence_id = :evidence_id,
                    last_error_code = NULL,
                    last_error_summary = NULL,
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id AND idempotency_key = :idempotency_key
                """
            ),
            {
                "tenant_id": tenant_id,
                "idempotency_key": idempotency_key,
                "evidence_id": response.evidenceId,
            },
        )
        _cache_response(
            connection,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            response=response,
        )
        return response


@router.post(
    "/journeys/{journey_id}/evidence",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_journey_evidence(
    request: Request,
    tenant_id: str,
    journey_id: UUID,
    file: Annotated[UploadFile, File()],
    evidence_purpose: Annotated[str, Form(alias="evidencePurpose", min_length=1, max_length=160)],
    principal: Annotated[Principal, Depends(get_principal)],
    bearer_token: Annotated[str, Depends(get_bearer_token)],
    engine: Annotated[Engine, Depends(get_engine)],
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
    authorize(principal, tenant_id=tenant_id, permission="audit.evidence.upload")
    content = file.file.read()
    filename = file.filename or "evidence"
    content_type = file.content_type or "application/octet-stream"
    request_hash = _request_hash(
        journey_id=journey_id,
        evidence_purpose=evidence_purpose,
        requirement_key=requirement_key,
        document_type_key=document_type_key,
        filename=filename,
        content_type=content_type,
        content=content,
    )

    with engine.begin() as connection:
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
        operation, cached = _load_or_create_operation(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            customer_id=journey["customer_id"],
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            evidence_purpose=evidence_purpose,
            requirement_key=requirement_key,
            document_type_key=document_type_key,
            correlation_id=get_correlation_id(request),
        )
        if cached is not None:
            return cached
        if operation is None:
            raise RuntimeError("Evidence ingestion operation was not created")
        customer_id = journey["customer_id"]
        display_name = journey["display_name"]
        subject_id = operation["di_subject_id"] or _subject_mapping(
            connection,
            tenant_id=tenant_id,
            customer_id=customer_id,
        )
        document_id = operation["di_document_id"]

    if subject_id is None:
        try:
            subject_token = security_client.exchange_user_token(
                subject_token=bearer_token,
                permissions=["di.subject.create"],
            )
            subject = di_client.create_subject(
                token=subject_token,
                tenant_id=tenant_id,
                subject_type="OTHER",
                display_name=display_name,
            )
            subject_id = UUID(subject.subject_id)
            _persist_subject_mapping(
                engine,
                tenant_id=tenant_id,
                customer_id=customer_id,
                subject_id=subject_id,
            )
            _update_operation(
                engine,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                operation_status="RECEIVED",
                di_subject_id=subject_id,
            )
        except (DiClientError, SecurityTokenError) as exc:
            state, code = _operation_failure_state(exc)
            _update_operation(
                engine,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                operation_status=state,
                error_code=code,
                error_summary="DI subject resolution failed",
                increment_attempt=True,
            )
            raise _dependency_error(exc) from exc

    if document_id is not None:
        try:
            document = _document_for_recovery(
                bearer_token=bearer_token,
                security_client=security_client,
                di_client=di_client,
                tenant_id=tenant_id,
                subject_id=subject_id,
                document_id=document_id,
            )
        except (DiClientError, SecurityTokenError) as exc:
            state, code = _operation_failure_state(exc)
            _update_operation(
                engine,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                operation_status=state,
                di_subject_id=subject_id,
                di_document_id=document_id,
                error_code=code,
                error_summary="DI recovery status refresh failed",
                increment_attempt=True,
            )
            raise _dependency_error(exc) from exc
    else:
        _update_operation(
            engine,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            operation_status="DI_SUBMITTING",
            di_subject_id=subject_id,
            increment_attempt=True,
        )
        try:
            upload_token = security_client.exchange_user_token(
                subject_token=bearer_token,
                permissions=["di.document.upload"],
            )
            document = di_client.upload_document(
                token=upload_token,
                tenant_id=tenant_id,
                subject_id=str(subject_id),
                filename=filename,
                content=content,
                content_type=content_type,
                source_channel="API",
                document_type_key=document_type_key,
            )
            document_id = UUID(document.document_id)
            _update_operation(
                engine,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                operation_status="DI_ACCEPTED",
                di_subject_id=subject_id,
                di_document_id=document_id,
            )
        except (DiClientError, SecurityTokenError) as exc:
            state, code = _operation_failure_state(exc)
            _update_operation(
                engine,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                operation_status=state,
                di_subject_id=subject_id,
                error_code=code,
                error_summary="DI document submission failed",
            )
            raise _dependency_error(exc) from exc

    return _link_evidence(
        engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        customer_id=customer_id,
        requirement_id=requirement_id,
        idempotency_key=idempotency_key,
        subject_id=subject_id,
        document=document,
        document_type_key=document_type_key,
        evidence_purpose=evidence_purpose,
        actor_id=principal.subject,
    )
