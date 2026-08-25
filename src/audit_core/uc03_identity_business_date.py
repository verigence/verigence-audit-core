from __future__ import annotations

import json
from datetime import date, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core import uc03_booking_capture
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_booking_commands import _authorize_security, _journey_context

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}/booking",
    tags=["uc03-identity-business-date"],
)


class BookingIdentityBusinessDateView(BaseModel):
    journeyId: UUID
    enteredName: str
    legalName: str | None
    legalNameStatus: str
    legalNameSourceEvidenceId: UUID | None
    actualBookingDate: date | None
    auditCapturedAtUtc: datetime
    captureLagDays: int | None


_original_write_typed_capture = uc03_booking_capture._write_typed_capture
_original_completion_summary = uc03_booking_capture._completion_summary
_installed = False
_PROFILE_SUPERSEDED = "SUPERSEDED_BY_ACTUAL_BOOKING_DATE"
_PROFILE_REASON_PREFIX = "Requirement not effective on Actual Booking Date"


def _effective_versions_for_business_date(
    connection: Connection,
    *,
    tenant_id: str,
    business_date: date,
) -> dict[str, UUID | None]:
    """Resolve immutable decision configuration that was effective on a business date.

    RETIRED versions remain eligible for historical cases because retirement does not
    erase the period in which a published version governed dealer activity.
    """
    row = connection.execute(
        text(
            """
            SELECT
                (
                    SELECT v.document_requirement_profile_version_id
                    FROM auditcore.document_requirement_profile_versions v
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status IN ('PUBLISHED', 'RETIRED')
                      AND v.effective_from <= :business_date
                      AND (v.effective_to IS NULL OR v.effective_to >= :business_date)
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.document_requirement_profile_version_id DESC
                    LIMIT 1
                ) AS document_profile_version_id,
                (
                    SELECT v.policy_version_id
                    FROM auditcore.project_policy_versions v
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status IN ('PUBLISHED', 'RETIRED')
                      AND v.effective_from <= :business_date
                      AND (v.effective_to IS NULL OR v.effective_to >= :business_date)
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.policy_version_id DESC
                    LIMIT 1
                ) AS policy_version_id,
                (
                    SELECT v.price_list_version_id
                    FROM auditcore.price_list_versions v
                    WHERE v.tenant_id = :tenant_id
                      AND v.lifecycle_status IN ('PUBLISHED', 'RETIRED')
                      AND v.effective_from <= :business_date
                      AND (v.effective_to IS NULL OR v.effective_to >= :business_date)
                    ORDER BY v.effective_from DESC, v.version_no DESC,
                             v.price_list_version_id DESC
                    LIMIT 1
                ) AS price_list_version_id
            """
        ),
        {"tenant_id": tenant_id, "business_date": business_date},
    ).mappings().one()
    return dict(row)


def _reconcile_booking_document_requirements(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    document_profile_version_id: UUID | None,
    business_date: date,
) -> list[dict[str, str]]:
    """Rebind Booking requirements without deleting historical work.

    Requirements that existed only in the provisional profile remain persisted but
    become Not Applicable. Requirements effective on the Actual Booking Date are
    inserted or reactivated. Evidence rows are never deleted or detached here.
    """
    if document_profile_version_id is None:
        return []

    desired_rows = connection.execute(
        text(
            """
            SELECT document_requirement_item_id, requirement_key,
                   document_type_key, process_area, requirement_level,
                   condition_config
            FROM auditcore.document_requirement_items
            WHERE tenant_id = :tenant_id
              AND document_requirement_profile_version_id = :profile_version_id
              AND upper(process_area) = 'BOOKING'
            ORDER BY sort_order, requirement_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "profile_version_id": document_profile_version_id,
        },
    ).mappings().all()
    desired = {row["requirement_key"]: row for row in desired_rows}

    existing_rows = connection.execute(
        text(
            """
            SELECT journey_document_requirement_id, document_requirement_item_id,
                   requirement_key, document_type_key, process_area,
                   requirement_level, requirement_status, condition_snapshot
            FROM auditcore.journey_document_requirements
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND upper(process_area) = 'BOOKING'
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    existing = {row["requirement_key"]: row for row in existing_rows}
    changes: list[dict[str, str]] = []

    for requirement_key, current in existing.items():
        wanted = desired.get(requirement_key)
        current_snapshot = dict(current["condition_snapshot"] or {})
        if wanted is None:
            if current_snapshot.get("profileReconciliationState") != _PROFILE_SUPERSEDED:
                current_snapshot["profileReconciliationPreviousStatus"] = current[
                    "requirement_status"
                ]
            reason = f"{_PROFILE_REASON_PREFIX} {business_date.isoformat()}"
            current_snapshot["profileReconciliationState"] = _PROFILE_SUPERSEDED
            current_snapshot["profileReconciliationBusinessDate"] = business_date.isoformat()
            current_snapshot["applicabilityState"] = "NOT_APPLICABLE"
            current_snapshot["applicabilityReason"] = reason
            connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_document_requirements
                    SET requirement_status = 'NOT_APPLICABLE',
                        condition_snapshot = CAST(:snapshot AS jsonb),
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id
                      AND journey_document_requirement_id = :requirement_id
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "requirement_id": current["journey_document_requirement_id"],
                    "snapshot": json.dumps(current_snapshot),
                },
            )
            connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_document_assessments
                    SET applicability_state = 'NOT_APPLICABLE',
                        applicability_reason = :reason,
                        version_no = version_no + 1,
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id
                      AND journey_id = :journey_id
                      AND stage_code = 'BOOKING'
                      AND requirement_key = :requirement_key
                      AND (
                          applicability_state IS DISTINCT FROM 'NOT_APPLICABLE'
                          OR applicability_reason IS DISTINCT FROM :reason
                      )
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "requirement_key": requirement_key,
                    "reason": reason,
                },
            )
            changes.append(
                {
                    "requirementKey": requirement_key,
                    "action": "SUPERSEDED_NOT_APPLICABLE",
                }
            )
            continue

        was_superseded = (
            current_snapshot.get("profileReconciliationState") == _PROFILE_SUPERSEDED
        )
        new_snapshot = dict(wanted["condition_config"] or {})
        if not was_superseded and (
            current_snapshot.get("conditionKey") == new_snapshot.get("conditionKey")
        ):
            for key in ("applicabilityState", "applicabilityReason"):
                if key in current_snapshot:
                    new_snapshot[key] = current_snapshot[key]

        restored_status = current["requirement_status"]
        if was_superseded:
            restored_status = str(
                current_snapshot.get("profileReconciliationPreviousStatus") or "PENDING"
            )
        elif wanted["requirement_level"] != "CONDITIONAL" and restored_status == "NOT_APPLICABLE":
            restored_status = "PENDING"

        connection.execute(
            text(
                """
                UPDATE auditcore.journey_document_requirements
                SET document_requirement_item_id = :item_id,
                    document_type_key = :document_type_key,
                    process_area = :process_area,
                    requirement_level = :requirement_level,
                    requirement_status = :requirement_status,
                    condition_snapshot = CAST(:snapshot AS jsonb),
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id
                  AND journey_document_requirement_id = :requirement_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "requirement_id": current["journey_document_requirement_id"],
                "item_id": wanted["document_requirement_item_id"],
                "document_type_key": wanted["document_type_key"],
                "process_area": wanted["process_area"],
                "requirement_level": wanted["requirement_level"],
                "requirement_status": restored_status,
                "snapshot": json.dumps(new_snapshot),
            },
        )
        if was_superseded:
            target_state = (
                "UNRESOLVED" if wanted["requirement_level"] == "CONDITIONAL" else "APPLICABLE"
            )
            connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_document_assessments
                    SET applicability_state = :state,
                        applicability_reason = NULL,
                        version_no = version_no + 1,
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id
                      AND journey_id = :journey_id
                      AND stage_code = 'BOOKING'
                      AND requirement_key = :requirement_key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "requirement_key": requirement_key,
                    "state": target_state,
                },
            )
            changes.append(
                {"requirementKey": requirement_key, "action": "REACTIVATED"}
            )
        elif (
            current["document_requirement_item_id"]
            != wanted["document_requirement_item_id"]
        ):
            changes.append(
                {"requirementKey": requirement_key, "action": "REBIND_VERSION"}
            )

    for requirement_key, wanted in desired.items():
        if requirement_key in existing:
            continue
        connection.execute(
            text(
                """
                INSERT INTO auditcore.journey_document_requirements (
                    tenant_id, journey_id, document_requirement_item_id,
                    requirement_key, document_type_key, process_area,
                    requirement_level, requirement_status, condition_snapshot
                ) VALUES (
                    :tenant_id, :journey_id, :item_id,
                    :requirement_key, :document_type_key, :process_area,
                    :requirement_level, 'PENDING', CAST(:snapshot AS jsonb)
                )
                ON CONFLICT (tenant_id, journey_id, requirement_key) DO NOTHING
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "item_id": wanted["document_requirement_item_id"],
                "requirement_key": requirement_key,
                "document_type_key": wanted["document_type_key"],
                "process_area": wanted["process_area"],
                "requirement_level": wanted["requirement_level"],
                "snapshot": json.dumps(dict(wanted["condition_config"] or {})),
            },
        )
        changes.append({"requirementKey": requirement_key, "action": "ADDED"})

    return changes


def _reconcile_effective_configuration(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    business_date: date,
) -> dict[str, Any]:
    versions = _effective_versions_for_business_date(
        connection,
        tenant_id=tenant_id,
        business_date=business_date,
    )
    current = connection.execute(
        text(
            """
            SELECT document_requirement_profile_version_id,
                   policy_version_id, price_list_version_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if current is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking case not found for the requested Project.",
        )

    requirement_changes = _reconcile_booking_document_requirements(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        document_profile_version_id=versions["document_profile_version_id"],
        business_date=business_date,
    )
    connection.execute(
        text(
            """
            UPDATE auditcore.journeys
            SET document_requirement_profile_version_id = :document_profile_version_id,
                policy_version_id = :policy_version_id,
                price_list_version_id = :price_list_version_id,
                updated_at_utc = now(),
                version_no = version_no + 1
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            **versions,
        },
    )
    return {
        "businessDate": business_date.isoformat(),
        "previous": dict(current),
        "effective": versions,
        "requirementChanges": requirement_changes,
    }


def _identity_aware_write_typed_capture(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    field_key: str,
    value: Any,
    source_evidence_id: UUID | None,
) -> tuple[str, str]:
    """Preserve Entered Name and reconcile configuration on Actual Booking Date."""
    key = field_key.strip().upper()
    if key == "CUSTOMER_NAME":
        customer_id = uc03_booking_capture._journey_customer_id(
            connection, tenant_id, journey_id
        )
        if source_evidence_id is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Entered Name is read-only",
                detail=(
                    "The Customer Name entered when the Booking Journey was created "
                    "is retained as audit input and cannot be edited afterward."
                ),
            )
        uc03_booking_capture._validate_evidence_for_journey(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            evidence_id=source_evidence_id,
        )
        return "CUSTOMER", str(customer_id)

    result = _original_write_typed_capture(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        field_key=field_key,
        value=value,
        source_evidence_id=source_evidence_id,
    )
    if key == "BOOKING_DATE":
        business_date = uc03_booking_capture._as_date(value, key)
        _reconcile_effective_configuration(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            business_date=business_date,
        )
    return result


def _business_date_aware_completion_summary(
    connection: Connection,
    tenant_id: str,
    journey_id: UUID,
) -> dict[str, Any]:
    summary = _original_completion_summary(connection, tenant_id, journey_id)
    blockers = list(summary["blockers"])
    row = connection.execute(
        text(
            """
            SELECT b.booking_date,
                   j.document_requirement_profile_version_id,
                   j.policy_version_id, j.price_list_version_id
            FROM auditcore.journeys j
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return summary
    if row["booking_date"] is None:
        blockers.append(
            {
                "code": "ACTUAL_BOOKING_DATE_REQUIRED",
                "label": "Capture or validate Actual Booking Date before Booking completion",
            }
        )
    else:
        effective = _effective_versions_for_business_date(
            connection,
            tenant_id=tenant_id,
            business_date=row["booking_date"],
        )
        missing = [
            label
            for key, label in (
                ("document_profile_version_id", "Document Requirement Profile"),
                ("policy_version_id", "Project Policy"),
                ("price_list_version_id", "Price List"),
            )
            if effective[key] is None
        ]
        if missing:
            blockers.append(
                {
                    "code": "BUSINESS_DATE_CONFIGURATION_MISSING",
                    "label": (
                        "No effective configuration for Actual Booking Date: "
                        + ", ".join(missing)
                    ),
                }
            )
        elif any(
            row[key] != effective[key]
            for key in (
                "document_profile_version_id",
                "policy_version_id",
                "price_list_version_id",
            )
        ):
            blockers.append(
                {
                    "code": "BUSINESS_DATE_CONFIGURATION_RECONCILIATION_PENDING",
                    "label": "Reconcile Booking configuration to the Actual Booking Date before completion",
                }
            )

    summary["blockers"] = blockers
    summary["ready"] = not blockers
    return summary


def install_uc03_identity_business_date() -> None:
    """Install reconciled UC03 identity and business-date publication boundaries."""
    global _installed
    if _installed:
        return

    for document_type in ("booking_form", "booking_docket"):
        fields = set(uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS.get(document_type, set()))
        fields.discard("customer_name")
        uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS[document_type] = fields

    uc03_booking_capture._SUPPORTED_PROPOSAL_FIELDS["aadhaar"] = {"aadhaar_name"}
    uc03_booking_capture._PROPOSAL_CAPTURE_MAP["aadhaar_name"] = "CUSTOMER_NAME"
    uc03_booking_capture._write_typed_capture = _identity_aware_write_typed_capture
    uc03_booking_capture._completion_summary = _business_date_aware_completion_summary
    _installed = True


@router.get("/identity-context", response_model=BookingIdentityBusinessDateView)
def get_booking_identity_business_date(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingIdentityBusinessDateView:
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )

    row = connection.execute(
        text(
            """
            SELECT
                j.journey_id,
                c.display_name AS entered_name,
                c.legal_name,
                c.legal_name_status,
                c.legal_name_source_evidence_id,
                b.booking_date AS actual_booking_date,
                j.created_at_utc AS audit_captured_at_utc,
                CASE
                    WHEN b.booking_date IS NULL THEN NULL
                    ELSE ((j.created_at_utc AT TIME ZONE p.timezone_name)::date - b.booking_date)
                END AS capture_lag_days
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id = j.tenant_id
             AND c.customer_id = j.customer_id
            JOIN auditcore.projects p
              ON p.tenant_id = j.tenant_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id
             AND b.journey_id = j.journey_id
            WHERE j.tenant_id = :tenant_id
              AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking case not found for the requested Project.",
        )

    return BookingIdentityBusinessDateView(
        journeyId=row["journey_id"],
        enteredName=row["entered_name"],
        legalName=row["legal_name"],
        legalNameStatus=row["legal_name_status"],
        legalNameSourceEvidenceId=row["legal_name_source_evidence_id"],
        actualBookingDate=row["actual_booking_date"],
        auditCapturedAtUtc=row["audit_captured_at_utc"],
        captureLagDays=row["capture_lag_days"],
    )
