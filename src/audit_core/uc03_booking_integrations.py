from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Response
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import ConflictError, DependencyUnavailableError, NotFoundError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError
from audit_core.uc03_booking_capture import (
    _TERMINAL_PROCESSING_STATUSES,
    ExtractionRefreshResponse,
    _scope,
    _stage_state,
)
from audit_core.uc03_booking_capture import get_booking_workspace as _base_workspace

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-booking-integration"],
)

_DI_AUDIENCE = "di"
_SUPPORTED_UC03_FIELDS: dict[str, frozenset[str]] = {
    "booking_form": frozenset(
        {
            "customer_name",
            "customer_phone",
            "vehicle_model",
            "vehicle_variant",
            "vehicle_color",
        }
    ),
    "booking_docket": frozenset(
        {
            "customer_name",
            "customer_phone",
            "vehicle_model",
            "vehicle_variant",
            "vehicle_color",
        }
    ),
    "pan_card": frozenset({"pan_number", "pan_name"}),
    "pan": frozenset({"pan_number", "pan_name"}),
}


def _active_booking(state: Any) -> bool:
    return state is not None and state["business_status"] in {
        "BOOKING_STARTED",
        "BOOKING_IN_PROGRESS",
    }


def _proposal_payload(fact: Any) -> dict[str, Any]:
    """Preserve the machine value and add optional DI source localization."""
    payload: dict[str, Any] = {"value": fact.value}
    if fact.page_no is not None or fact.evidence_region is not None:
        payload["sourceLocalization"] = {
            "pageNo": fact.page_no,
            "evidenceRegion": fact.evidence_region,
        }
    return payload


def _enrich_workspace_localization(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    body: dict[str, Any],
) -> None:
    """Expose optional DI source localization without changing proposal persistence."""
    rows = connection.execute(
        text(
            """
            SELECT capture_proposal_id, proposed_value
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    localization_by_id: dict[str, dict[str, Any]] = {}
    for row in rows:
        proposed = row["proposed_value"]
        if not isinstance(proposed, dict):
            continue
        localization = proposed.get("sourceLocalization")
        if isinstance(localization, dict):
            localization_by_id[str(row["capture_proposal_id"])] = localization

    proposals = body.get("proposals")
    if not isinstance(proposals, list):
        return
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        localization = localization_by_id.get(str(proposal.get("proposalId") or ""))
        if localization is None:
            proposal["pageNo"] = None
            proposal["evidenceRegion"] = None
            continue
        page_no = localization.get("pageNo")
        region = localization.get("evidenceRegion")
        proposal["pageNo"] = page_no if isinstance(page_no, int) and not isinstance(page_no, bool) else None
        proposal["evidenceRegion"] = region if isinstance(region, dict) else None


@router.post("/booking/extraction/refresh", response_model=ExtractionRefreshResponse)
def refresh_booking_extraction_strict(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ExtractionRefreshResponse:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if not _active_booking(state):
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking state conflict",
            detail="The Booking must be active before extraction can refresh.",
        )

    evidence_rows = connection.execute(
        text(
            """
            SELECT e.evidence_id, e.di_subject_id, e.di_document_id,
                   e.document_type_key
            FROM auditcore.evidence e
            LEFT JOIN auditcore.journey_document_requirements jdr
              ON jdr.tenant_id=e.tenant_id
             AND jdr.journey_document_requirement_id=e.journey_document_requirement_id
            WHERE e.tenant_id=:tenant_id AND e.journey_id=:journey_id
              AND e.association_status='ACTIVE'
              AND (jdr.process_area IS NULL OR upper(jdr.process_area)='BOOKING')
            ORDER BY e.linked_at_utc, e.evidence_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    refreshed = 0
    created = 0
    failed = 0
    try:
        token = security_client.get_service_token(audience=_DI_AUDIENCE)
    except SecurityTokenError as exc:
        raise DependencyUnavailableError(
            detail="Document processing is temporarily unavailable. Please try again."
        ) from exc

    for evidence in evidence_rows:
        if evidence["di_subject_id"] is None or evidence["di_document_id"] is None:
            continue
        try:
            document = di_client.get_document(
                token=token,
                tenant_id=tenant_id,
                subject_id=str(evidence["di_subject_id"]),
                document_id=str(evidence["di_document_id"]),
            )
            processing = (document.processing_status or "PENDING").upper()
            connection.execute(
                text(
                    """
                    UPDATE auditcore.evidence
                    SET processing_status_cache=:processing,
                        verification_status_cache=:verification,
                        confirmation_status_cache=:confirmation,
                        cache_updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "evidence_id": evidence["evidence_id"],
                    "processing": processing,
                    "verification": document.verification_state,
                    "confirmation": document.confirmation_status,
                },
            )
            refreshed += 1
            if processing not in _TERMINAL_PROCESSING_STATUSES:
                continue

            facts = di_client.get_document_facts(
                token=token,
                tenant_id=tenant_id,
                subject_id=str(evidence["di_subject_id"]),
                document_id=str(evidence["di_document_id"]),
            )
            document_type = (
                document.document_type_key or evidence["document_type_key"] or ""
            ).strip().lower()
            supported = _SUPPORTED_UC03_FIELDS.get(document_type, frozenset())
            for fact in facts:
                if fact.field_key not in supported:
                    continue
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.journey_capture_proposals
                        SET proposal_status='SUPERSEDED', updated_at_utc=now()
                        WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                          AND stage_code='BOOKING'
                          AND source_evidence_id=:evidence_id
                          AND field_key=:field_key
                          AND proposal_status='PENDING'
                          AND source_fact_version < :fact_version
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "journey_id": journey_id,
                        "evidence_id": evidence["evidence_id"],
                        "field_key": fact.field_key,
                        "fact_version": fact.version_no,
                    },
                )
                result = connection.execute(
                    text(
                        """
                        INSERT INTO auditcore.journey_capture_proposals (
                            tenant_id, journey_id, stage_code, field_key,
                            source_evidence_id, source_evidence_fact_id,
                            source_fact_version, source_document_type_key,
                            value_source, proposed_value, confidence_score
                        ) VALUES (
                            :tenant_id, :journey_id, 'BOOKING', :field_key,
                            :evidence_id, :fact_id, :fact_version,
                            :document_type, :value_source,
                            CAST(:proposed_value AS jsonb), :confidence
                        )
                        ON CONFLICT (
                            tenant_id, source_evidence_id, source_evidence_fact_id,
                            source_fact_version
                        ) DO NOTHING
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "journey_id": journey_id,
                        "field_key": fact.field_key,
                        "evidence_id": evidence["evidence_id"],
                        "fact_id": fact.canonical_field_id,
                        "fact_version": fact.version_no,
                        "document_type": document_type,
                        "value_source": fact.value_source,
                        "proposed_value": json.dumps(_proposal_payload(fact), default=str),
                        "confidence": fact.confidence_score,
                    },
                )
                if result.rowcount:
                    created += 1
        except DiClientError:
            failed += 1
            connection.execute(
                text(
                    """
                    UPDATE auditcore.evidence
                    SET processing_status_cache='FAILED', cache_updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND evidence_id=:evidence_id
                    """
                ),
                {"tenant_id": tenant_id, "evidence_id": evidence["evidence_id"]},
            )

    return ExtractionRefreshResponse(
        journeyId=journey_id,
        refreshedDocuments=refreshed,
        createdProposals=created,
        failedDocuments=failed,
        aggregateVersion=int(state["version_no"]),
    )


@router.get("/evidence/{evidence_id}/review-content")
def get_booking_evidence_review_content(
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    security_client: Annotated[SecurityOAuthClient, Depends(get_security_oauth_client)],
    di_client: Annotated[DiClient, Depends(get_di_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> Response:
    """Stream the original DI document to an authorized UC03 human reviewer."""
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    row = connection.execute(
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
    if row is None or row["di_subject_id"] is None or row["di_document_id"] is None:
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Evidence not found",
            detail="The source document was not found for this Journey evidence.",
        )

    try:
        token = security_client.get_service_token(audience=_DI_AUDIENCE)
        content, mime_type, content_disposition = di_client.get_document_content(
            token=token,
            tenant_id=tenant_id,
            subject_id=str(row["di_subject_id"]),
            document_id=str(row["di_document_id"]),
        )
    except (DiClientError, SecurityTokenError) as exc:
        raise DependencyUnavailableError(
            detail="The source document is temporarily unavailable. Please try again."
        ) from exc

    filename = None
    if isinstance(content_disposition, str) and "filename=" in content_disposition:
        filename = content_disposition.split("filename=", 1)[1].strip().strip('"')
    headers = {
        "Content-Disposition": f'inline; filename="{filename}"' if filename else "inline",
        "Cache-Control": "private, no-store",
    }
    return Response(content=content, media_type=mime_type, headers=headers)


@router.get("/uc03-workspace")
def get_booking_workspace_with_typed_exchange(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    body = _base_workspace(
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )
    details = connection.execute(
        text(
            """
            SELECT details
            FROM auditcore.trade_in_cases
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if isinstance(details, dict) and "exchangeTaken" in details:
        body.setdefault("capture", {})["EXCHANGE_TAKEN"] = bool(details["exchangeTaken"])
    _enrich_workspace_localization(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        body=body,
    )
    return body
