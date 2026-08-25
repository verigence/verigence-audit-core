from __future__ import annotations

import json
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.di_client import DiClient, DiClientError, DiFact
from audit_core.errors import DependencyUnavailableError, NotFoundError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError
from audit_core.uc03_booking_capture import _scope
from audit_core.uc03_booking_integrations import _audit_context_ref

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/evidence",
    tags=["uc03-booking-evidence-details"],
)

_DI_AUDIENCE = "di"


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
            SELECT customer_id, di_subject_id, di_document_id
            FROM auditcore.evidence
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND evidence_id=:evidence_id
              AND association_status='ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
        },
    ).mappings().one_or_none()
    if (
        row is None
        or row["customer_id"] is None
        or row["di_subject_id"] is None
        or row["di_document_id"] is None
    ):
        raise NotFoundError(
            error_code="VAC-NF-006",
            title="Evidence not found",
            detail="The Booking evidence was not found for this Journey.",
        )
    return row


def _value_projection(value: Any) -> tuple[str, Any, str | None]:
    if isinstance(value, bool):
        return "BOOLEAN", value, "true" if value else "false"
    if isinstance(value, int | float) and not isinstance(value, bool):
        return "NUMBER", value, str(value)
    if isinstance(value, str):
        return "TEXT", value, value
    return "JSON", value, None


def _fact_rows(
    connection: Connection,
    *,
    tenant_id: str,
    evidence_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT evidence_fact_id, field_key, value_type, value_json,
                   normalized_value, confidence_score, verification_status,
                   fetched_at_utc
            FROM auditcore.evidence_facts
            WHERE tenant_id=:tenant_id
              AND evidence_id=:evidence_id
              AND superseded_at_utc IS NULL
            ORDER BY field_key, evidence_fact_id
            """
        ),
        {"tenant_id": tenant_id, "evidence_id": evidence_id},
    ).mappings().all()
    return [
        {
            "evidenceFactId": row["evidence_fact_id"],
            "fieldKey": row["field_key"],
            "valueType": row["value_type"],
            "value": row["value_json"],
            "normalizedValue": row["normalized_value"],
            "confidenceScore": (
                float(row["confidence_score"])
                if row["confidence_score"] is not None
                else None
            ),
            "verificationStatus": row["verification_status"],
            "fetchedAtUtc": row["fetched_at_utc"].isoformat(),
        }
        for row in rows
    ]


def _update_evidence_cache(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    processing_status: str,
    verification_status: str | None,
    confirmation_status: str | None,
) -> None:
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence
            SET processing_status_cache=:processing_status,
                verification_status_cache=:verification_status,
                confirmation_status_cache=:confirmation_status,
                cache_updated_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND journey_id=:journey_id
              AND evidence_id=:evidence_id
              AND association_status='ACTIVE'
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "evidence_id": evidence_id,
            "processing_status": processing_status,
            "verification_status": verification_status,
            "confirmation_status": confirmation_status,
        },
    )


def _persist_facts(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    processing_status: str,
    verification_status: str | None,
    confirmation_status: str | None,
    facts: tuple[DiFact, ...],
) -> list[dict[str, Any]]:
    _update_evidence_cache(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
        processing_status=processing_status,
        verification_status=verification_status,
        confirmation_status=confirmation_status,
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.evidence_facts
            SET superseded_at_utc=now()
            WHERE tenant_id=:tenant_id
              AND evidence_id=:evidence_id
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
                    field_key, value_type, value_json, normalized_value,
                    confidence_score, di_field_reference, verification_status
                ) VALUES (
                    :tenant_id, :evidence_id, :journey_id,
                    :field_key, :value_type, CAST(:value_json AS jsonb), :normalized_value,
                    :confidence_score, :di_field_reference, :verification_status
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "evidence_id": evidence_id,
                "journey_id": journey_id,
                "field_key": fact.field_key,
                "value_type": value_type,
                "value_json": json.dumps(value_json, default=str),
                "normalized_value": normalized_value,
                "confidence_score": fact.confidence_score,
                "di_field_reference": fact.canonical_field_id,
                "verification_status": verification_status,
            },
        )
    return _fact_rows(connection, tenant_id=tenant_id, evidence_id=evidence_id)


@router.post("/{evidence_id}/refresh")
def refresh_booking_evidence_details(
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
) -> list[dict[str, Any]]:
    """Refresh one UC03 Booking document with the authenticated human-token model."""
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    row = _evidence_row(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
    )
    context_ref = _audit_context_ref(journey_id, row["customer_id"])
    try:
        service_token = security_client.get_service_token(audience=_DI_AUDIENCE)
        document = di_client.get_audit_document(
            token=service_token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(row["di_document_id"]),
        )
    except (DiClientError, SecurityTokenError) as exc:
        raise DependencyUnavailableError(
            detail="Document processing is temporarily unavailable. Please try again."
        ) from exc

    processing_status = (document.processing_status or "PENDING").upper()
    confirmation_status = (document.confirmation_status or "").upper()
    if confirmation_status != "CONFIRMED":
        _update_evidence_cache(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=evidence_id,
            processing_status=processing_status,
            verification_status=document.verification_state,
            confirmation_status=document.confirmation_status,
        )
        return _fact_rows(
            connection,
            tenant_id=tenant_id,
            evidence_id=evidence_id,
        )

    try:
        facts = di_client.get_audit_document_facts(
            token=service_token,
            tenant_id=tenant_id,
            external_context_ref=context_ref,
            document_id=str(row["di_document_id"]),
        )
    except DiClientError as exc:
        raise DependencyUnavailableError(
            detail="Document processing is temporarily unavailable. Please try again."
        ) from exc

    return _persist_facts(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
        processing_status=processing_status,
        verification_status=document.verification_state,
        confirmation_status=document.confirmation_status,
        facts=facts,
    )


@router.get("/{evidence_id}/facts")
def get_booking_evidence_facts(
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[dict[str, Any]]:
    """Return persisted UC03 Booking facts using the same human authorization model."""
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    _evidence_row(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=evidence_id,
    )
    return _fact_rows(connection, tenant_id=tenant_id, evidence_id=evidence_id)
