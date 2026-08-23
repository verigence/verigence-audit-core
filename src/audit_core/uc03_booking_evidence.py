from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Header, Request, UploadFile, status
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_engine, get_human_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import AuditCoreError
from audit_core.evidence import (
    EvidenceResponse,
    _dependency_error,
    _external_context_ref,
    _link_evidence,
    _load_or_create_operation,
    _operation_failure_state,
    _persist_subject_mapping,
    _request_hash,
    _subject_mapping,
    _update_operation,
    get_di_client,
    get_security_oauth_client,
)
from audit_core.evidence import _journey_context as _evidence_journey_context
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _authorize_security,
    _journey_context as _booking_journey_context,
    _stage_state,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/stages/BOOKING/documents",
    tags=["uc03-booking-evidence"],
)

_DI_AUDIENCE = "di"


def _record_upload_activity(
    engine: Engine,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
    actor_role: str,
    idempotency_key: str,
    correlation_id: str,
    requirement_key: str,
    evidence_id: UUID,
) -> None:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        already_recorded = connection.execute(
            text(
                """
                SELECT 1
                FROM auditcore.journey_workflow_events
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                  AND event_type='BOOKING_EVIDENCE_LINKED'
                  AND idempotency_key=:idempotency_key
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "idempotency_key": idempotency_key,
            },
        ).scalar_one_or_none()
        if already_recorded is not None:
            return
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        if state is None or state["business_status"] not in {"BOOKING_STARTED", "BOOKING_IN_PROGRESS"}:
            return
        next_version = int(state["version_no"]) + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='BOOKING_IN_PROGRESS',
                    audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                    latest_activity_at_utc=now(), updated_at_utc=now(),
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
            event_type="BOOKING_EVIDENCE_LINKED",
            source_kind="HUMAN",
            actor_id=actor_id,
            actor_role_snapshot=actor_role,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "requirementKey": requirement_key,
                "evidenceId": str(evidence_id),
            },
            aggregate_version=next_version,
        )


@router.post(
    "/{requirement_key}/evidence",
    response_model=EvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_booking_document(
    request: Request,
    tenant_id: str,
    journey_id: UUID,
    requirement_key: str,
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    engine: Annotated[Engine, Depends(get_engine)],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
) -> EvidenceResponse:
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    correlation_id = get_correlation_id(request)
    content = file.file.read()
    filename = file.filename or "booking-evidence"
    content_type = file.content_type or "application/octet-stream"

    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        booking_context = _booking_journey_context(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            actor_id=human_principal.subject,
        )
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        if state is None or state["business_status"] not in {"BOOKING_STARTED", "BOOKING_IN_PROGRESS"}:
            raise AuditCoreError(
                error_code="VAC-CONFLICT-004",
                status_code=409,
                title="Booking state conflict",
                detail="Start the Booking before uploading Booking documents.",
            )
        journey = _evidence_journey_context(connection, tenant_id, journey_id)
        requirement = connection.execute(
            text(
                """
                SELECT journey_document_requirement_id, document_type_key,
                       requirement_status, condition_snapshot
                FROM auditcore.journey_document_requirements
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND requirement_key=:requirement_key
                  AND upper(process_area)='BOOKING'
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_key": requirement_key,
            },
        ).mappings().one_or_none()
        if requirement is None:
            raise AuditCoreError(
                error_code="VAC-VAL-003",
                status_code=400,
                title="Unsupported evidence",
                detail="The Booking document requirement is not configured for this case.",
            )
        if requirement["requirement_status"] == "NOT_APPLICABLE":
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="Evidence cannot be uploaded for a document resolved as not applicable.",
            )
        requirement_id = requirement["journey_document_requirement_id"]
        document_type_key = requirement["document_type_key"]
        evidence_purpose = f"UC03_BOOKING:{requirement_key}"
        request_hash = _request_hash(
            journey_id=journey_id,
            evidence_purpose=evidence_purpose,
            requirement_key=requirement_key,
            document_type_key=document_type_key,
            filename=filename,
            content_type=content_type,
            content=content,
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
            correlation_id=correlation_id,
        )
        if cached is not None:
            return cached
        if operation is None:
            raise RuntimeError("Evidence ingestion operation was not created")
        customer_id = journey["customer_id"]
        subject_id = operation["di_subject_id"] or _subject_mapping(
            connection,
            tenant_id=tenant_id,
            customer_id=customer_id,
        )
        document_id = operation["di_document_id"]
        context_ref = _external_context_ref(journey_id=journey_id, customer_id=customer_id)

    try:
        service_token = security_client.get_service_token(audience=_DI_AUDIENCE)
        if subject_id is None:
            subject = di_client.create_subject(
                token=service_token,
                tenant_id=tenant_id,
                subject_type="OTHER",
                display_name=journey["customer_name"],
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

        di_client.ensure_audit_storage_context(
            token=service_token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            subject_id=str(subject_id),
            dealer_id=str(journey["dealer_id"]),
            outlet_id=str(journey["outlet_id"]),
            customer_id=str(customer_id),
            project_name=journey["project_name"],
            dealer_name=journey["dealer_name"],
            outlet_name=journey["outlet_name"],
            customer_name=journey["customer_name"],
            idempotency_key=f"{idempotency_key}:context",
        )
        if document_id is not None:
            document = di_client.get_audit_document(
                token=service_token,
                tenant_id=tenant_id,
                external_context_ref=context_ref,
                document_id=str(document_id),
            )
        else:
            _update_operation(
                engine,
                tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                operation_status="DI_SUBMITTING",
                di_subject_id=subject_id,
                increment_attempt=True,
            )
            document = di_client.upload_audit_document(
                token=service_token,
                tenant_id=tenant_id,
                external_context_ref=context_ref,
                filename=filename,
                content=content,
                content_type=content_type,
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
        state_value, code = _operation_failure_state(exc)
        _update_operation(
            engine,
            tenant_id=tenant_id,
            idempotency_key=idempotency_key,
            operation_status=state_value,
            di_subject_id=subject_id,
            di_document_id=document_id,
            error_code=code,
            error_summary="Booking document processing could not be started",
            increment_attempt=True,
        )
        raise _dependency_error(exc) from exc

    response = _link_evidence(
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
        actor_id=human_principal.subject,
    )
    _record_upload_activity(
        engine,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
        actor_role=booking_context["operating_role"],
        idempotency_key=idempotency_key,
        correlation_id=correlation_id,
        requirement_key=requirement_key,
        evidence_id=response.evidenceId,
    )
    return response
