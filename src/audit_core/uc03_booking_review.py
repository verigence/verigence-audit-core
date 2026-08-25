from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import AuditCoreError, DependencyUnavailableError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError
from audit_core.uc03_booking_capture import _PROPOSAL_CAPTURE_MAP, _scope
from audit_core.uc03_booking_commands import (
    _aggregate_lock,
    _append_workflow_event,
    _parse_if_match,
    _require_expected_version,
    _stage_state,
)
from audit_core.uc03_booking_details import (
    _DI_AUDIENCE,
    DocumentApprovalResponse,
    _require_active,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/details",
    tags=["uc03-booking-review"],
)


@router.post(
    "/review/{evidence_id}/approve-editable",
    response_model=DocumentApprovalResponse,
)
def approve_review_document_after_editable_fields(
    request: Request,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    if_match: Annotated[str, Header(alias="If-Match")],
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> DocumentApprovalResponse:
    """Approve a reviewed Booking document once every editable DI field is decided.

    Product Model/Variant proposals are intentionally read-only until Product Master
    resolution is complete. They remain visible audit evidence but must not make a
    document impossible for the Process Consultant to approve.
    """
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    _require_active(state)
    _require_expected_version(state, _parse_if_match(if_match))

    evidence = connection.execute(
        text(
            """
            SELECT di_subject_id, di_document_id
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND evidence_id=:evidence_id AND association_status='ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
        },
    ).mappings().one_or_none()
    if evidence is None:
        raise AuditCoreError(
            error_code="VAC-NF-006",
            status_code=404,
            title="Evidence not found",
            detail="The Booking evidence was not found for this Journey.",
        )

    pending_editable = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND source_evidence_id=:evidence_id
              AND proposal_status='PENDING'
              AND field_key = ANY(:editable_fields)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
            "editable_fields": list(_PROPOSAL_CAPTURE_MAP),
        },
    ).scalar_one()
    if int(pending_editable) > 0:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Document review incomplete",
            detail="Review all editable extracted fields before approving this document.",
        )

    try:
        service_token = security_client.get_service_token(audience=_DI_AUDIENCE)
        di_client.verify_document(
            token=service_token,
            tenant_id=tenant_id,
            subject_id=str(evidence["di_subject_id"]),
            document_id=str(evidence["di_document_id"]),
            remarks="UC03 Process Consultant document review approved",
            field_corrections=[],
        )
    except (DiClientError, SecurityTokenError) as exc:
        raise DependencyUnavailableError(
            detail="Document verification is temporarily unavailable. Please try again."
        ) from exc

    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET verification_status_cache='VERIFIED', cache_updated_at_utc=now()
            WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
            """
        ),
        {"tenant_id": tenant_id, "evidence_id": evidence_id},
    )
    next_version = int(state["version_no"]) + 1
    connection.execute(
        text(
            """
            UPDATE auditcore.journey_stage_states
            SET latest_activity_at_utc=now(), updated_at_utc=now(), version_no=:version
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
        event_type="BOOKING_DOCUMENT_REVIEW_APPROVED",
        source_kind="HUMAN",
        actor_id=human_principal.subject,
        actor_role_snapshot=context["operating_role"],
        idempotency_key=idempotency_key,
        correlation_id=get_correlation_id(request),
        safe_payload={
            "evidenceId": str(evidence_id),
            "editableProposalGate": True,
        },
        aggregate_version=next_version,
    )
    return DocumentApprovalResponse(
        evidenceId=evidence_id,
        aggregateVersion=next_version,
        verificationStatus="VERIFIED",
    )
