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


def _proposal_text(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    field_key: str,
) -> str | None:
    row = connection.execute(
        text(
            """
            SELECT accepted_value, proposed_value
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND field_key=:field_key
              AND proposal_status <> 'SUPERSEDED'
              AND lower(COALESCE(source_document_type_key,''))
                    IN ('booking_form','booking_docket')
            ORDER BY created_at_utc DESC, capture_proposal_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "field_key": field_key},
    ).mappings().one_or_none()
    if row is None:
        return None
    value = row["accepted_value"] if row["accepted_value"] is not None else row["proposed_value"]
    if isinstance(value, dict):
        value = value.get("value")
    return str(value).strip() if value not in (None, "") else None


def _product_master_match(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    extracted_model = _proposal_text(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        field_key="vehicle_model",
    )
    extracted_variant = _proposal_text(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        field_key="vehicle_variant",
    )
    base = {
        "extractedModel": extracted_model,
        "extractedVariant": extracted_variant,
        "modelId": None,
        "modelName": None,
        "variantId": None,
        "variantName": None,
        "masterVersionIds": [],
    }
    if not extracted_model or not extracted_variant:
        return {
            **base,
            "status": "PENDING_EXTRACTION",
            "message": "Model and Variant will be matched after Booking Docket extraction.",
        }

    booking_date = connection.execute(
        text(
            """
            SELECT booking_date FROM auditcore.bookings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if booking_date is None:
        return {
            **base,
            "status": "PENDING_BOOKING_DATE",
            "message": "Approve the extracted Actual Booking Date before effective Product Master matching.",
        }

    effective_versions = connection.execute(
        text(
            """
            SELECT DISTINCT ON (v.product_master_id)
                   v.version_id
            FROM auditcore.project_product_master_versions v
            JOIN auditcore.project_product_masters m
              ON m.tenant_id=v.tenant_id
             AND m.product_master_id=v.product_master_id
            WHERE v.tenant_id=:tenant_id
              AND m.status='ACTIVE'
              AND v.lifecycle_status IN ('PUBLISHED','RETIRED')
              AND v.effective_from <= :booking_date
            ORDER BY v.product_master_id, v.effective_from DESC,
                     v.version_no DESC, v.version_id DESC
            """
        ),
        {"tenant_id": tenant_id, "booking_date": booking_date},
    ).scalars().all()
    if not effective_versions:
        return {
            **base,
            "status": "NO_EFFECTIVE_MASTER",
            "message": "No effective Project Product Master exists for the Actual Booking Date.",
        }

    model_norm = _normalized(extracted_model)
    variant_norm = _normalized(extracted_variant)
    candidates = connection.execute(
        text(
            """
            SELECT DISTINCT m.model_id, m.model_name,
                            v.variant_id, v.variant_name
            FROM auditcore.project_product_master_items i
            JOIN auditcore.product_skus s
              ON s.product_sku_id=i.product_sku_id AND s.is_active
            JOIN auditcore.product_models m
              ON m.model_id=s.model_id AND m.is_active
            JOIN auditcore.product_variants v
              ON v.variant_id=s.variant_id AND v.is_active
            WHERE i.tenant_id=:tenant_id
              AND i.version_id = ANY(:version_ids)
              AND lower(regexp_replace(trim(COALESCE(m.model_name,'')), '[^a-zA-Z0-9]+', '', 'g'))
                    = :model_norm
              AND lower(regexp_replace(trim(COALESCE(v.variant_name,'')), '[^a-zA-Z0-9]+', '', 'g'))
                    = :variant_norm
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_ids": effective_versions,
            "model_norm": model_norm,
            "variant_norm": variant_norm,
        },
    ).mappings().all()

    if not candidates:
        # Codes are also valid master identifiers, but remain exact after
        # normalization; no fuzzy/manual fallback is permitted.
        candidates = connection.execute(
            text(
                """
                SELECT DISTINCT m.model_id, m.model_name,
                                v.variant_id, v.variant_name
                FROM auditcore.project_product_master_items i
                JOIN auditcore.product_skus s
                  ON s.product_sku_id=i.product_sku_id AND s.is_active
                JOIN auditcore.product_models m
                  ON m.model_id=s.model_id AND m.is_active
                JOIN auditcore.product_variants v
                  ON v.variant_id=s.variant_id AND v.is_active
                WHERE i.tenant_id=:tenant_id
                  AND i.version_id = ANY(:version_ids)
                  AND lower(regexp_replace(trim(COALESCE(m.model_code,'')), '[^a-zA-Z0-9]+', '', 'g'))
                        = :model_norm
                  AND lower(regexp_replace(trim(COALESCE(v.variant_code,'')), '[^a-zA-Z0-9]+', '', 'g'))
                        = :variant_norm
                """
            ),
            {
                "tenant_id": tenant_id,
                "version_ids": effective_versions,
                "model_norm": model_norm,
                "variant_norm": variant_norm,
            },
        ).mappings().all()

    if len(candidates) == 1:
        candidate = candidates[0]
        return {
            **base,
            "status": "MATCHED",
            "modelId": str(candidate["model_id"]),
            "modelName": candidate["model_name"],
            "variantId": str(candidate["variant_id"]),
            "variantName": candidate["variant_name"],
            "masterVersionIds": [str(value) for value in effective_versions],
            "message": "Extracted Model and Variant match the effective Project Product Master.",
        }
    if len(candidates) > 1:
        return {
            **base,
            "status": "AMBIGUOUS",
            "masterVersionIds": [str(value) for value in effective_versions],
            "message": "More than one Product Master match exists. Do not key in a replacement; log an Observation.",
        }
    return {
        **base,
        "status": "NO_MATCH",
        "masterVersionIds": [str(value) for value in effective_versions],
        "message": "Extracted Model/Variant do not match the effective Project Product Master. Log an Observation.",
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
        "productMaster": _product_master_match(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        ),
    }
