from __future__ import annotations

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_capture import _scope

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/booking/part1",
    tags=["uc03-booking-part1"],
)

_PART1_KINDS = {
    "booking_docket": "BOOKING_DOCKET",
    "pan_card": "PAN",
    "aadhaar": "AADHAAR",
    "booking_payment_receipt": "BOOKING_PAYMENT_RECEIPT",
    "minimum_booking_payment_proof": "BOOKING_PAYMENT_RECEIPT",
}


def _normalized(value: str | None) -> str | None:
    """Legacy normalization helper retained for compatibility tests only."""
    if value is None:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "", value.strip().lower())
    return normalized or None


def _requirement_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT journey_document_requirement_id, requirement_key,
                   document_type_key, requirement_level, requirement_status
            FROM auditcore.journey_document_requirements
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND upper(process_area)='BOOKING'
              AND requirement_key IN (
                  'booking_docket','pan_card','aadhaar',
                  'booking_payment_receipt','minimum_booking_payment_proof'
              )
            ORDER BY requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        kind = _PART1_KINDS[row["requirement_key"]]
        # Prefer the canonical payment requirement if an old and new snapshot both
        # exist during migration/reconciliation.
        if kind in seen and row["requirement_key"] != "booking_payment_receipt":
            continue
        if kind in seen:
            result = [item for item in result if item["kind"] != kind]
        evidence_rows = connection.execute(
            text(
                """
                SELECT evidence_id, document_type_key, processing_status_cache,
                       verification_status_cache, linked_at_utc
                FROM auditcore.evidence
                WHERE tenant_id=:tenant_id
                  AND journey_id=:journey_id
                  AND journey_document_requirement_id=:requirement_id
                  AND association_status='ACTIVE'
                ORDER BY linked_at_utc, evidence_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "requirement_id": row["journey_document_requirement_id"],
            },
        ).mappings().all()
        result.append(
            {
                "kind": kind,
                "requirementKey": row["requirement_key"],
                "documentTypeKey": (
                    "dealer_receipt"
                    if kind == "BOOKING_PAYMENT_RECEIPT"
                    else row["document_type_key"]
                ),
                "requirementLevel": row["requirement_level"],
                "requirementStatus": row["requirement_status"],
                "evidence": [
                    {
                        "evidenceId": str(evidence["evidence_id"]),
                        "documentTypeKey": evidence["document_type_key"],
                        "processingStatus": evidence["processing_status_cache"],
                        "verificationStatus": evidence["verification_status_cache"],
                        "linkedAtUtc": evidence["linked_at_utc"].isoformat(),
                    }
                    for evidence in evidence_rows
                ],
            }
        )
        seen.add(kind)
    return result


def _product_master_compatibility_placeholder() -> dict[str, Any]:
    """Preserve the existing response shape without doing Product Master work.

    Booking Part-1 is a document-capture read. Proposal extraction and Product
    Master matching belong outside this screen and must not delay Booking open.
    """
    return {
        "status": "PENDING_EXTRACTION",
        "extractedModel": None,
        "extractedVariant": None,
        "modelId": None,
        "modelName": None,
        "variantId": None,
        "variantName": None,
        "masterVersionIds": [],
        "message": "Product Master matching is not evaluated during Booking Part 1.",
    }


@router.get("")
def get_booking_part1(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    requirements = _requirement_rows(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    by_kind = {item["kind"]: item for item in requirements}
    pan_count = len(by_kind.get("PAN", {}).get("evidence", []))
    aadhaar_count = len(by_kind.get("AADHAAR", {}).get("evidence", []))
    docket_count = len(by_kind.get("BOOKING_DOCKET", {}).get("evidence", []))
    receipt_count = len(by_kind.get("BOOKING_PAYMENT_RECEIPT", {}).get("evidence", []))

    return {
        "journeyId": str(journey_id),
        "requirements": requirements,
        "mandatoryEvidence": {
            "bookingDocketComplete": docket_count > 0,
            "kycComplete": pan_count > 0 or aadhaar_count > 0,
            "kycBothProvided": pan_count > 0 and aadhaar_count > 0,
            "paymentReceiptComplete": receipt_count > 0,
            "paymentReceiptCount": receipt_count,
            "part1EvidenceComplete": docket_count > 0
            and (pan_count > 0 or aadhaar_count > 0)
            and receipt_count > 0,
        },
        # Compatibility only. No proposal or Product Master queries execute here.
        "productMaster": _product_master_compatibility_placeholder(),
    }
