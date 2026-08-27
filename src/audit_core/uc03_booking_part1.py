from __future__ import annotations

import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import ConflictError, NotFoundError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import _authorize_security

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


def _part1_bootstrap_rows(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
) -> list[dict[str, Any]]:
    """Load all state needed to paint Booking Part-1 in one database round trip.

    Tenant RLS context is established inside this statement, so the hot read does
    not pay a separate set_tenant_context SQL round trip before loading the Journey,
    role, stage header, four document requirements and active Evidence.
    """

    rows = connection.execute(
        text(
            """
            WITH runtime_context AS MATERIALIZED (
                SELECT set_config('app.tenant_id', :tenant_id, true) AS tenant_context
            ),
            scoped_journey AS MATERIALIZED (
                SELECT
                    j.dealer_id,
                    j.outlet_id,
                    c.display_name,
                    s.business_status,
                    s.closure_disposition,
                    s.audit_state,
                    s.audit_status,
                    s.close_reason_code,
                    s.closure_remarks,
                    s.version_no,
                    (
                        SELECT array_agg(
                            DISTINCT ba.business_role_code
                            ORDER BY ba.business_role_code
                        )
                        FROM auditcore.business_assignments ba
                        WHERE ba.tenant_id = j.tenant_id
                          AND ba.security_actor_id = :actor_id
                          AND ba.assignment_status = 'ACTIVE'
                          AND ba.effective_from <= now()
                          AND (ba.effective_to IS NULL OR ba.effective_to >= now())
                          AND (
                                ba.dealer_id IS NULL
                                OR (
                                    ba.dealer_id = j.dealer_id
                                    AND (ba.outlet_id IS NULL OR ba.outlet_id = j.outlet_id)
                                )
                          )
                    ) AS operating_roles
                FROM runtime_context rc
                CROSS JOIN auditcore.journeys j
                JOIN auditcore.customers c
                  ON c.tenant_id = j.tenant_id
                 AND c.customer_id = j.customer_id
                LEFT JOIN auditcore.journey_stage_states s
                  ON s.tenant_id = j.tenant_id
                 AND s.journey_id = j.journey_id
                 AND s.stage_code = 'BOOKING'
                WHERE j.tenant_id = :tenant_id
                  AND j.journey_id = :journey_id
            ),
            requirements AS MATERIALIZED (
                SELECT
                    r.journey_document_requirement_id,
                    r.requirement_key,
                    r.document_type_key,
                    r.requirement_level,
                    r.requirement_status,
                    COALESCE(
                        jsonb_agg(
                            jsonb_build_object(
                                'evidenceId', e.evidence_id::text,
                                'documentTypeKey', e.document_type_key,
                                'processingStatus', e.processing_status_cache,
                                'verificationStatus', e.verification_status_cache,
                                'linkedAtUtc', e.linked_at_utc
                            )
                            ORDER BY e.linked_at_utc, e.evidence_id
                        ) FILTER (WHERE e.evidence_id IS NOT NULL),
                        '[]'::jsonb
                    ) AS evidence
                FROM runtime_context rc
                CROSS JOIN auditcore.journey_document_requirements r
                LEFT JOIN auditcore.evidence e
                  ON e.tenant_id = r.tenant_id
                 AND e.journey_id = r.journey_id
                 AND e.journey_document_requirement_id = r.journey_document_requirement_id
                 AND e.association_status = 'ACTIVE'
                WHERE r.tenant_id = :tenant_id
                  AND r.journey_id = :journey_id
                  AND upper(r.process_area) = 'BOOKING'
                  AND r.requirement_key IN (
                      'booking_docket','pan_card','aadhaar',
                      'booking_payment_receipt','minimum_booking_payment_proof'
                  )
                GROUP BY
                    r.journey_document_requirement_id,
                    r.requirement_key,
                    r.document_type_key,
                    r.requirement_level,
                    r.requirement_status
            )
            SELECT
                sj.dealer_id,
                sj.outlet_id,
                sj.display_name,
                sj.business_status,
                sj.closure_disposition,
                sj.audit_state,
                sj.audit_status,
                sj.close_reason_code,
                sj.closure_remarks,
                sj.version_no,
                sj.operating_roles,
                r.journey_document_requirement_id,
                r.requirement_key,
                r.document_type_key,
                r.requirement_level,
                r.requirement_status,
                r.evidence
            FROM scoped_journey sj
            LEFT JOIN requirements r ON true
            ORDER BY r.requirement_key NULLS LAST
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actor_id": actor_id,
        },
    ).mappings().all()
    return [dict(row) for row in rows]


def _part1_view_from_rows(
    *,
    journey_id: UUID,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if not rows:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking case not found for the requested Project.",
        )

    header = rows[0]
    roles = header.get("operating_roles") or []
    if not roles:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )
    if len(roles) != 1:
        raise ConflictError(
            error_code="VAC-CONFLICT-006",
            title="Ambiguous Project operating role",
            detail="The current Project assignments resolve to more than one operating role.",
        )

    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        requirement_key = row.get("requirement_key")
        if not isinstance(requirement_key, str):
            continue
        kind = _PART1_KINDS[requirement_key]
        if kind in seen and requirement_key != "booking_payment_receipt":
            continue
        if kind in seen:
            requirements = [item for item in requirements if item["kind"] != kind]
        evidence_rows = row.get("evidence")
        requirements.append(
            {
                "kind": kind,
                "requirementKey": requirement_key,
                "documentTypeKey": (
                    "dealer_receipt"
                    if kind == "BOOKING_PAYMENT_RECEIPT"
                    else row["document_type_key"]
                ),
                "requirementLevel": row["requirement_level"],
                "requirementStatus": row["requirement_status"],
                "evidence": evidence_rows if isinstance(evidence_rows, list) else [],
            }
        )
        seen.add(kind)

    by_kind = {item["kind"]: item for item in requirements}
    pan_count = len(by_kind.get("PAN", {}).get("evidence", []))
    aadhaar_count = len(by_kind.get("AADHAAR", {}).get("evidence", []))
    docket_count = len(by_kind.get("BOOKING_DOCKET", {}).get("evidence", []))
    receipt_count = len(by_kind.get("BOOKING_PAYMENT_RECEIPT", {}).get("evidence", []))

    return {
        "journeyId": str(journey_id),
        "aggregateVersion": int(header.get("version_no") or 0),
        "operatingRole": roles[0],
        "capture": {"CUSTOMER_NAME": header["display_name"]},
        "bookingStage": {
            "businessStatus": header.get("business_status"),
            "closureDisposition": header.get("closure_disposition"),
            "auditState": header.get("audit_state") or "NOT_STARTED",
            "auditStatus": header.get("audit_status") or "NOT_EVALUATED",
            "closeReasonCode": header.get("close_reason_code"),
            "closureRemarks": header.get("closure_remarks"),
        },
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
        "productMaster": _product_master_compatibility_placeholder(),
    }


def _product_master_compatibility_placeholder() -> dict[str, Any]:
    """Preserve the existing response shape without doing Product Master work."""
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
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    rows = _part1_bootstrap_rows(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    return _part1_view_from_rows(journey_id=journey_id, rows=rows)
