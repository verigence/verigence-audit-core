from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.di_client import DiClient, DiClientError
from audit_core.errors import AuditCoreError, ConflictError, DependencyUnavailableError, NotFoundError
from audit_core.evidence import get_di_client, get_security_oauth_client
from audit_core.idempotency import execute_idempotent_json_command
from audit_core.observability import get_correlation_id
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.security_integration import SecurityOAuthClient, SecurityTokenError
from audit_core.uc03_booking_commands import (
    BookingCommandResponse,
    _aggregate_lock,
    _append_workflow_event,
    _authorize_security,
    _build_response,
    _journey_context,
    _parse_if_match,
    _require_expected_version,
    _require_transition,
    _set_etag,
    _stage_state,
)
from audit_core.uc03_document_assessments import (
    _assessment_row,
    _effective_applicability,
)

router = APIRouter(
    prefix="/v1/tenants/{tenant_id}/journeys/{journey_id}",
    tags=["uc03-booking-capture"],
)

_DI_AUDIENCE = "di"
_ACTIVE_BOOKING_STATUSES = {"BOOKING_STARTED", "BOOKING_IN_PROGRESS"}
_TERMINAL_PROCESSING_STATUSES = {
    "COMPLETED",
    "COMPLETE",
    "PROCESSED",
    "SUCCEEDED",
    "READY",
    "VERIFIED",
}
_HUMAN_FLAG_CATEGORIES = {
    "PHYSICAL_OBSERVATION",
    "DOCUMENT_EXCEPTION",
    "PAYMENT_EXCEPTION",
    "CUSTOMER_IDENTITY_CONCERN",
    "COMMERCIAL_EXCEPTION",
    "PROCESS_NON_COMPLIANCE",
    "DELIVERY_EXCEPTION",
    "OTHER",
}
_SEVERITIES = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}

# Only source relationships reconciled as SUPPORTED for the C1 Booking profile are
# allowed to become Audit Core proposals. Provisional/TBD mappings stay disabled.
_SUPPORTED_PROPOSAL_FIELDS: dict[str, set[str]] = {
    "booking_form": {
        "customer_name",
        "customer_phone",
        "vehicle_model",
        "vehicle_variant",
        "vehicle_color",
        "booking_reference_number",
        "booking_date",
    },
    "booking_docket": {
        "customer_name",
        "customer_phone",
        "vehicle_model",
        "vehicle_variant",
        "vehicle_color",
        "booking_reference_number",
        "booking_date",
    },
    "pan_card": {"pan_name"},
    "pan": {"pan_name"},
}

# These DI fields have an unambiguous existing typed-domain owner. Product strings
# are shown as proposals but cannot be accepted without a configured SKU/master
# resolution, so they deliberately do not appear here.
_PROPOSAL_CAPTURE_MAP = {
    "customer_name": "CUSTOMER_NAME",
    "pan_name": "CUSTOMER_NAME",
    "customer_phone": "CUSTOMER_NUMBER",
    "customer_email": "CUSTOMER_EMAIL",
    "booking_reference_number": "BOOKING_REFERENCE",
    "booking_date": "BOOKING_DATE",
}

_CAPTURE_FIELDS = {
    "CUSTOMER_NAME",
    "CUSTOMER_NUMBER",
    "CUSTOMER_EMAIL",
    "CUSTOMER_TYPE",
    "BOOKING_REFERENCE",
    "BOOKING_DATE",
    "DEAL_TYPE",
    "DEAL_SOURCE",
    "LEAD_SOURCE",
    "REGISTRATION_STATE",
    "TERRITORY_CATEGORIZATION",
    "DISTRICT_NAME",
    "REGISTRATION_TYPE",
    "REGISTRATION_CATEGORY",
    "EXCHANGE_TAKEN",
    "TRADE_IN_REGISTRATION",
    "TRADE_IN_MAKE_MODEL",
    "TRADE_IN_ACTUAL_VALUE",
}


class CaptureCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    sourceEvidenceId: UUID | None = None


class ProposalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acceptedValue: Any | None = None


class HumanFlagCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str = Field(min_length=1, max_length=100)
    severity: str = Field(min_length=1, max_length=20)
    summary: str = Field(min_length=1, max_length=500)
    remarks: str | None = Field(default=None, max_length=4000)
    evidenceIds: list[UUID] = Field(default_factory=list, max_length=20)


class ExtractionRefreshResponse(BaseModel):
    journeyId: UUID
    refreshedDocuments: int
    createdProposals: int
    failedDocuments: int
    aggregateVersion: int


def _require_active_booking(state) -> None:
    if state is None or state["business_status"] not in _ACTIVE_BOOKING_STATUSES:
        raise ConflictError(
            error_code="VAC-CONFLICT-004",
            title="Booking state conflict",
            detail="The Booking must be active before capture or extraction work can change.",
        )


def _scope(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
) -> dict[str, Any]:
    _authorize_security(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    return _journey_context(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )


def _journey_customer_id(connection: Connection, tenant_id: str, journey_id: UUID) -> UUID:
    value = connection.execute(
        text(
            """
            SELECT customer_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    if value is None:
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Booking not found",
            detail="Booking case not found for the requested Project.",
        )
    return value


def _validate_evidence_for_journey(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    evidence_id: UUID | None,
) -> None:
    if evidence_id is None:
        return
    found = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.evidence
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND evidence_id = :evidence_id
              AND association_status = 'ACTIVE'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "evidence_id": evidence_id},
    ).scalar_one_or_none()
    if found is None:
        raise AuditCoreError(
            error_code="VAC-VAL-003",
            status_code=400,
            title="Unsupported evidence",
            detail="The selected evidence is not linked to this Booking.",
        )


def _as_text(value: Any, field_key: str) -> str:
    if not isinstance(value, (str, int, float, bool)):
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail=f"{field_key} requires a scalar value.",
        )
    result = str(value).strip()
    if not result:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail=f"{field_key} cannot be blank.",
        )
    return result


def _as_bool(value: Any, field_key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"YES", "Y", "TRUE", "1"}:
            return True
        if normalized in {"NO", "N", "FALSE", "0"}:
            return False
    raise AuditCoreError(
        error_code="VAC-VAL-002",
        status_code=422,
        title="Business validation failed",
        detail=f"{field_key} requires Yes/No.",
    )


def _as_date(value: Any, field_key: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(_as_text(value, field_key))
    except ValueError as exc:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail=f"{field_key} requires an ISO date (YYYY-MM-DD).",
        ) from exc


def _as_decimal(value: Any, field_key: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail=f"{field_key} requires a numeric value.",
        ) from exc


def _write_typed_capture(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    field_key: str,
    value: Any,
    source_evidence_id: UUID | None,
) -> tuple[str, str]:
    key = field_key.strip().upper()
    if key not in _CAPTURE_FIELDS:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Unsupported Booking capture field",
            detail="This Booking field does not yet have an approved typed-domain mapping.",
        )
    _validate_evidence_for_journey(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        evidence_id=source_evidence_id,
    )
    customer_id = _journey_customer_id(connection, tenant_id, journey_id)
    source_kind = "EVIDENCE" if source_evidence_id is not None else "OPERATIONAL_INPUT"

    if key in {"CUSTOMER_NAME", "CUSTOMER_NUMBER", "CUSTOMER_EMAIL", "CUSTOMER_TYPE"}:
        updates: dict[str, Any] = {
            "tenant_id": tenant_id,
            "customer_id": customer_id,
        }
        if key == "CUSTOMER_NAME":
            updates["column"] = "display_name"
            updates["value"] = _as_text(value, key)
        elif key == "CUSTOMER_NUMBER":
            phone = "".join(ch for ch in _as_text(value, key) if ch.isdigit())
            if len(phone) < 4:
                raise AuditCoreError(
                    error_code="VAC-VAL-002",
                    status_code=422,
                    title="Business validation failed",
                    detail="Customer Number must contain at least four digits.",
                )
            updates["column"] = "mobile_last4"
            updates["value"] = phone[-4:]
        elif key == "CUSTOMER_EMAIL":
            updates["column"] = "email_reference"
            updates["value"] = _as_text(value, key)
        else:
            updates["column"] = "customer_type_code"
            updates["value"] = _as_text(value, key).upper()
        allowed_columns = {
            "display_name",
            "mobile_last4",
            "email_reference",
            "customer_type_code",
        }
        column = updates["column"]
        if column not in allowed_columns:
            raise RuntimeError("Invalid Customer capture column")
        connection.execute(
            text(
                f"""
                UPDATE auditcore.customers
                SET {column} = :value,
                    updated_at_utc = now(),
                    version_no = version_no + 1
                WHERE tenant_id = :tenant_id AND customer_id = :customer_id
                """
            ),
            updates,
        )
        return "CUSTOMER", str(customer_id)

    if key in {"BOOKING_REFERENCE", "BOOKING_DATE", "DEAL_TYPE", "DEAL_SOURCE", "LEAD_SOURCE"}:
        column = {
            "BOOKING_REFERENCE": "booking_reference",
            "BOOKING_DATE": "booking_date",
            "DEAL_TYPE": "deal_type_code",
            "DEAL_SOURCE": "deal_source_code",
            "LEAD_SOURCE": "lead_source_code",
        }[key]
        normalized: Any = _as_date(value, key) if key == "BOOKING_DATE" else _as_text(value, key)
        connection.execute(
            text(
                f"""
                INSERT INTO auditcore.bookings (tenant_id, journey_id, {column})
                VALUES (:tenant_id, :journey_id, :value)
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    {column} = EXCLUDED.{column},
                    updated_at_utc = now(),
                    version_no = auditcore.bookings.version_no + 1
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "value": normalized},
        )
        booking_id = connection.execute(
            text(
                "SELECT booking_id FROM auditcore.bookings WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        return "BOOKING", str(booking_id)

    if key in {
        "REGISTRATION_STATE",
        "TERRITORY_CATEGORIZATION",
        "DISTRICT_NAME",
        "REGISTRATION_TYPE",
        "REGISTRATION_CATEGORY",
    }:
        column = {
            "REGISTRATION_STATE": "registration_state",
            "TERRITORY_CATEGORIZATION": "registration_territory",
            "DISTRICT_NAME": "registration_district",
            "REGISTRATION_TYPE": "registration_type_code",
            "REGISTRATION_CATEGORY": "registration_category_code",
        }[key]
        connection.execute(
            text(
                f"""
                INSERT INTO auditcore.registration_records (
                    tenant_id, journey_id, {column}, source_kind, source_evidence_id
                ) VALUES (
                    :tenant_id, :journey_id, :value, :source_kind, :source_evidence_id
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    {column} = EXCLUDED.{column},
                    source_kind = EXCLUDED.source_kind,
                    source_evidence_id = EXCLUDED.source_evidence_id,
                    updated_at_utc = now(),
                    version_no = auditcore.registration_records.version_no + 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "value": _as_text(value, key),
                "source_kind": source_kind,
                "source_evidence_id": source_evidence_id,
            },
        )
        record_id = connection.execute(
            text(
                "SELECT registration_record_id FROM auditcore.registration_records WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        return "REGISTRATION", str(record_id)

    if key in {
        "EXCHANGE_TAKEN",
        "TRADE_IN_REGISTRATION",
        "TRADE_IN_MAKE_MODEL",
        "TRADE_IN_ACTUAL_VALUE",
    }:
        if key == "EXCHANGE_TAKEN":
            column = "actual_status_code"
            normalized = "EXCHANGE_TAKEN" if _as_bool(value, key) else "NO_EXCHANGE"
        elif key == "TRADE_IN_REGISTRATION":
            column = "old_vehicle_registration"
            normalized = _as_text(value, key)
        elif key == "TRADE_IN_MAKE_MODEL":
            column = "old_vehicle_make_model"
            normalized = _as_text(value, key)
        else:
            column = "actual_value"
            normalized = _as_decimal(value, key)
        connection.execute(
            text(
                f"""
                INSERT INTO auditcore.trade_in_cases (
                    tenant_id, journey_id, {column}, source_kind, source_evidence_id
                ) VALUES (
                    :tenant_id, :journey_id, :value, :source_kind, :source_evidence_id
                )
                ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                    {column} = EXCLUDED.{column},
                    source_kind = EXCLUDED.source_kind,
                    source_evidence_id = EXCLUDED.source_evidence_id,
                    updated_at_utc = now(),
                    version_no = auditcore.trade_in_cases.version_no + 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "value": normalized,
                "source_kind": source_kind,
                "source_evidence_id": source_evidence_id,
            },
        )
        record_id = connection.execute(
            text(
                "SELECT trade_in_case_id FROM auditcore.trade_in_cases WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        return "TRADE_IN", str(record_id)

    raise RuntimeError("Unreachable capture mapping")


def _condition_value(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    condition_key: str,
) -> bool | None:
    normalized = condition_key.strip().lower()
    if normalized in {"exchangetaken", "exchange_taken"}:
        status = connection.execute(
            text(
                "SELECT actual_status_code FROM auditcore.trade_in_cases WHERE tenant_id=:tenant_id AND journey_id=:journey_id"
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one_or_none()
        if status is None:
            return None
        return str(status).upper() == "EXCHANGE_TAKEN"
    if normalized in {
        "corporatecustomer",
        "customeriscorporate",
        "corporatediscounttaken",
        "corporate_customer",
    }:
        customer_type = connection.execute(
            text(
                """
                SELECT c.customer_type_code
                FROM auditcore.journeys j
                JOIN auditcore.customers c
                  ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
                WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one_or_none()
        if customer_type is None:
            return None
        return str(customer_type).upper() in {"CORPORATE", "BUSINESS", "COMPANY"}
    return None


def _resolve_booking_applicability(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    rows = connection.execute(
        text(
            """
            SELECT journey_document_requirement_id, requirement_key,
                   requirement_status, condition_snapshot
            FROM auditcore.journey_document_requirements
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND upper(process_area)='BOOKING'
              AND requirement_level='CONDITIONAL'
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    for row in rows:
        snapshot = dict(row["condition_snapshot"] or {})
        raw_key = snapshot.get("conditionKey")
        if not isinstance(raw_key, str) or not raw_key.strip():
            continue
        resolved = _condition_value(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            condition_key=raw_key,
        )
        if resolved is None:
            continue
        new_state = "APPLICABLE" if resolved else "NOT_APPLICABLE"
        old_state = snapshot.get("applicabilityState")
        if old_state == new_state:
            continue
        reason = f"{raw_key}={'Yes' if resolved else 'No'}"
        snapshot["applicabilityState"] = new_state
        snapshot["applicabilityReason"] = reason
        new_requirement_status = "PENDING" if resolved else "NOT_APPLICABLE"
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_document_requirements
                SET requirement_status=:requirement_status,
                    condition_snapshot=CAST(:snapshot AS jsonb),
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id
                  AND journey_document_requirement_id=:requirement_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "requirement_id": row["journey_document_requirement_id"],
                "requirement_status": new_requirement_status,
                "snapshot": json.dumps(snapshot),
            },
        )
        existing = _assessment_row(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_key=row["requirement_key"],
        )
        if existing is not None and existing["applicability_state"] != new_state:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_document_assessments
                    SET applicability_state=:state,
                        applicability_reason=:reason,
                        answer='UNANSWERED', evidence_id=NULL,
                        answered_at_utc=NULL,
                        version_no=version_no+1,
                        updated_at_utc=now()
                    WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                      AND stage_code='BOOKING' AND requirement_key=:requirement_key
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "journey_id": journey_id,
                    "requirement_key": row["requirement_key"],
                    "state": new_state,
                    "reason": reason,
                },
            )
        changes.append(
            {
                "requirementKey": row["requirement_key"],
                "previousState": old_state or "UNRESOLVED",
                "applicabilityState": new_state,
                "reason": reason,
            }
        )
    return changes


def _capture_snapshot(connection: Connection, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    row = connection.execute(
        text(
            """
            SELECT j.customer_id, c.display_name, c.mobile_last4,
                   c.email_reference, c.customer_type_code,
                   b.booking_reference, b.booking_date, b.deal_type_code,
                   b.deal_source_code, b.lead_source_code,
                   rr.registration_state, rr.registration_territory,
                   rr.registration_district, rr.registration_type_code,
                   rr.registration_category_code,
                   tic.actual_status_code AS trade_in_status,
                   tic.old_vehicle_registration, tic.old_vehicle_make_model,
                   tic.actual_value AS trade_in_actual_value
            FROM auditcore.journeys j
            JOIN auditcore.customers c
              ON c.tenant_id=j.tenant_id AND c.customer_id=j.customer_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id=j.tenant_id AND b.journey_id=j.journey_id
            LEFT JOIN auditcore.registration_records rr
              ON rr.tenant_id=j.tenant_id AND rr.journey_id=j.journey_id
            LEFT JOIN auditcore.trade_in_cases tic
              ON tic.tenant_id=j.tenant_id AND tic.journey_id=j.journey_id
            WHERE j.tenant_id=:tenant_id AND j.journey_id=:journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()
    return {
        "CUSTOMER_NAME": row["display_name"],
        "CUSTOMER_NUMBER": row["mobile_last4"],
        "CUSTOMER_EMAIL": row["email_reference"],
        "CUSTOMER_TYPE": row["customer_type_code"],
        "BOOKING_REFERENCE": row["booking_reference"],
        "BOOKING_DATE": row["booking_date"].isoformat() if row["booking_date"] else None,
        "DEAL_TYPE": row["deal_type_code"],
        "DEAL_SOURCE": row["deal_source_code"],
        "LEAD_SOURCE": row["lead_source_code"],
        "REGISTRATION_STATE": row["registration_state"],
        "TERRITORY_CATEGORIZATION": row["registration_territory"],
        "DISTRICT_NAME": row["registration_district"],
        "REGISTRATION_TYPE": row["registration_type_code"],
        "REGISTRATION_CATEGORY": row["registration_category_code"],
        "EXCHANGE_TAKEN": (
            None
            if row["trade_in_status"] is None
            else row["trade_in_status"] == "EXCHANGE_TAKEN"
        ),
        "TRADE_IN_REGISTRATION": row["old_vehicle_registration"],
        "TRADE_IN_MAKE_MODEL": row["old_vehicle_make_model"],
        "TRADE_IN_ACTUAL_VALUE": (
            str(row["trade_in_actual_value"])
            if row["trade_in_actual_value"] is not None
            else None
        ),
    }


def _proposal_value(row) -> Any:
    value = row["accepted_value"] if row["accepted_value"] is not None else row["proposed_value"]
    if isinstance(value, dict) and set(value) == {"value"}:
        return value["value"]
    return value


def _proposals(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT capture_proposal_id, field_key, source_evidence_id,
                   source_evidence_fact_id, source_fact_version,
                   source_document_type_key, value_source, proposed_value,
                   confidence_score, proposal_status, accepted_value,
                   accepted_by_actor_id, accepted_by_role, accepted_at_utc,
                   owning_domain_key, owning_record_reference, version_no
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
            ORDER BY created_at_utc, capture_proposal_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        {
            "proposalId": str(row["capture_proposal_id"]),
            "fieldKey": row["field_key"],
            "sourceEvidenceId": str(row["source_evidence_id"]),
            "sourceFactId": row["source_evidence_fact_id"],
            "sourceFactVersion": row["source_fact_version"],
            "sourceDocumentTypeKey": row["source_document_type_key"],
            "valueSource": row["value_source"],
            "proposedValue": (row["proposed_value"] or {}).get("value")
            if isinstance(row["proposed_value"], dict)
            else row["proposed_value"],
            "confidence": float(row["confidence_score"])
            if row["confidence_score"] is not None
            else None,
            "status": row["proposal_status"],
            "acceptedValue": _proposal_value(row)
            if row["proposal_status"] in {"ACCEPTED", "CORRECTED"}
            else None,
            "canAccept": row["field_key"] in _PROPOSAL_CAPTURE_MAP,
            "owningDomainKey": row["owning_domain_key"],
            "owningRecordReference": row["owning_record_reference"],
            "version": row["version_no"],
        }
        for row in rows
    ]


def _document_views(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT jdr.journey_document_requirement_id, jdr.requirement_key,
                   jdr.document_type_key, jdr.requirement_level,
                   jdr.requirement_status, jdr.condition_snapshot,
                   e.evidence_id, e.processing_status_cache,
                   e.verification_status_cache, e.cache_updated_at_utc
            FROM auditcore.journey_document_requirements jdr
            LEFT JOIN LATERAL (
                SELECT evidence_id, processing_status_cache,
                       verification_status_cache, cache_updated_at_utc
                FROM auditcore.evidence e
                WHERE e.tenant_id=jdr.tenant_id
                  AND e.journey_document_requirement_id=jdr.journey_document_requirement_id
                  AND e.association_status='ACTIVE'
                ORDER BY e.linked_at_utc DESC, e.evidence_id DESC
                LIMIT 1
            ) e ON true
            WHERE jdr.tenant_id=:tenant_id AND jdr.journey_id=:journey_id
              AND upper(jdr.process_area)='BOOKING'
            ORDER BY jdr.requirement_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    result: list[dict[str, Any]] = []
    for row in rows:
        state, reason = _effective_applicability(row)
        assessment = _assessment_row(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            requirement_key=row["requirement_key"],
        )
        if assessment is not None:
            state = assessment["applicability_state"]
            reason = assessment["applicability_reason"]
        result.append(
            {
                "requirementKey": row["requirement_key"],
                "documentTypeKey": row["document_type_key"],
                "requirementLevel": row["requirement_level"],
                "requirementStatus": row["requirement_status"],
                "applicabilityState": state,
                "applicabilityReason": reason,
                "answer": assessment["answer"] if assessment is not None else "UNANSWERED",
                "evidenceId": str(row["evidence_id"]) if row["evidence_id"] else None,
                "processingStatus": row["processing_status_cache"],
                "verificationStatus": row["verification_status_cache"],
                "updatedAtUtc": row["cache_updated_at_utc"].isoformat()
                if row["cache_updated_at_utc"]
                else None,
            }
        )
    return result


def _flags(connection: Connection, tenant_id: str, journey_id: UUID) -> list[dict[str, Any]]:
    rows = connection.execute(
        text(
            """
            SELECT audit_finding_id, finding_type_code, severity, finding_status,
                   title, description, stage_code, origin_kind,
                   origin_actor_id, origin_role_snapshot, rule_key,
                   blocking_completion, created_at_utc, updated_at_utc
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND (stage_code='BOOKING' OR stage_code IS NULL)
            ORDER BY created_at_utc DESC, audit_finding_id DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        {
            "flagId": str(row["audit_finding_id"]),
            "category": row["finding_type_code"],
            "severity": row["severity"],
            "status": row["finding_status"],
            "title": row["title"],
            "description": row["description"],
            "originKind": row["origin_kind"],
            "originActorId": row["origin_actor_id"],
            "originRole": row["origin_role_snapshot"],
            "ruleKey": row["rule_key"],
            "blockingCompletion": bool(row["blocking_completion"]),
            "createdAtUtc": row["created_at_utc"].isoformat(),
            "updatedAtUtc": row["updated_at_utc"].isoformat(),
        }
        for row in rows
    ]


def _completion_summary(connection: Connection, tenant_id: str, journey_id: UUID) -> dict[str, Any]:
    documents = _document_views(connection, tenant_id, journey_id)
    blockers: list[dict[str, str]] = []
    for item in documents:
        if item["applicabilityState"] == "UNRESOLVED":
            blockers.append(
                {
                    "code": "DOCUMENT_APPLICABILITY_PENDING",
                    "label": f"Resolve applicability for {item['requirementKey']}",
                }
            )
            continue
        if item["applicabilityState"] == "APPLICABLE" and item["requirementLevel"] in {
            "REQUIRED",
            "CONDITIONAL",
        }:
            if item["requirementStatus"] not in {"SATISFIED", "WAIVED"}:
                blockers.append(
                    {
                        "code": "DOCUMENT_REQUIREMENT_PENDING",
                        "label": f"Address {item['requirementKey']}",
                    }
                )
            processing = (item["processingStatus"] or "").upper()
            if item["evidenceId"] and processing and processing not in _TERMINAL_PROCESSING_STATUSES:
                if processing not in {"FAILED", "ERROR", "REJECTED"}:
                    blockers.append(
                        {
                            "code": "DOCUMENT_PROCESSING_PENDING",
                            "label": f"Processing {item['requirementKey']}",
                        }
                    )
                else:
                    blockers.append(
                        {
                            "code": "DOCUMENT_PROCESSING_FAILED",
                            "label": f"Resolve processing issue for {item['requirementKey']}",
                        }
                    )

    pending_proposals = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND proposal_status='PENDING'
              AND field_key = ANY(:accept_fields)
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "accept_fields": list(_PROPOSAL_CAPTURE_MAP),
        },
    ).scalar_one()
    if pending_proposals:
        blockers.append(
            {
                "code": "EXTRACTION_PROPOSALS_PENDING",
                "label": f"Review {pending_proposals} extraction proposal(s)",
            }
        )

    blocking_flags = connection.execute(
        text(
            """
            SELECT count(*)
            FROM auditcore.audit_findings
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING'
              AND finding_status IN ('OPEN','ACKNOWLEDGED')
              AND blocking_completion=true
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    if blocking_flags:
        blockers.append(
            {
                "code": "REVIEW_REQUIRED",
                "label": f"{blocking_flags} flag(s) require review before Booking completion",
            }
        )

    return {
        "ready": not blockers,
        "blockers": blockers,
        "documentCount": len(documents),
        "addressedDocumentCount": sum(
            1
            for item in documents
            if item["applicabilityState"] == "NOT_APPLICABLE"
            or item["requirementStatus"] in {"SATISFIED", "WAIVED", "NOT_APPLICABLE"}
        ),
        "pendingProposalCount": int(pending_proposals),
        "blockingFlagCount": int(blocking_flags),
    }


@router.put("/capture/{field_key}")
def record_booking_capture(
    tenant_id: str,
    journey_id: UUID,
    field_key: str,
    payload: CaptureCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)
    normalized_field = field_key.strip().upper()

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_active_booking(state)
        domain, record_reference = _write_typed_capture(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            field_key=normalized_field,
            value=payload.value,
            source_evidence_id=payload.sourceEvidenceId,
        )
        applicability_changes = _resolve_booking_applicability(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
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
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_CAPTURE_RECORDED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "fieldKey": normalized_field,
                "owningDomainKey": domain,
                "sourceEvidenceId": str(payload.sourceEvidenceId) if payload.sourceEvidenceId else None,
                "applicabilityChanges": applicability_changes,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "fieldKey": normalized_field,
            "value": payload.value,
            "owningDomainKey": domain,
            "owningRecordReference": record_reference,
            "applicabilityChanges": applicability_changes,
            "aggregateVersion": next_version,
            "eventId": str(event_id),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.capture:{journey_id}:{normalized_field}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "fieldKey": normalized_field,
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
    )
    _set_etag(response, body)
    return body


def _proposal_row(connection: Connection, tenant_id: str, journey_id: UUID, proposal_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT capture_proposal_id, field_key, source_evidence_id,
                   proposed_value, proposal_status, accepted_value, version_no
            FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND capture_proposal_id=:proposal_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "proposal_id": proposal_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-014",
            title="Extraction proposal not found",
            detail="The Booking extraction proposal was not found.",
        )
    return row


def _decide_proposal(
    *,
    tenant_id: str,
    journey_id: UUID,
    proposal_id: UUID,
    payload: ProposalDecision,
    corrected: bool,
    request: Request,
    response: Response,
    idempotency_key: str,
    if_match: str,
    human_principal: HumanPrincipal,
    authorization_client: SecurityAuthorizationClient,
    connection: Connection,
) -> dict[str, Any]:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_active_booking(state)
        proposal = _proposal_row(connection, tenant_id, journey_id, proposal_id)
        if proposal["proposal_status"] != "PENDING":
            raise ConflictError(
                error_code="VAC-CONFLICT-008",
                title="Extraction proposal already decided",
                detail="Refresh the Booking before deciding this extraction proposal.",
            )
        capture_key = _PROPOSAL_CAPTURE_MAP.get(proposal["field_key"])
        if capture_key is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Proposal requires configured master resolution",
                detail="This extracted field cannot be accepted until its typed-domain/master mapping is configured.",
            )
        machine_value = proposal["proposed_value"]
        if isinstance(machine_value, dict) and "value" in machine_value:
            machine_value = machine_value["value"]
        accepted_value = payload.acceptedValue if corrected else machine_value
        if corrected and payload.acceptedValue is None:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Business validation failed",
                detail="A corrected proposal requires acceptedValue.",
            )
        domain, record_reference = _write_typed_capture(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            field_key=capture_key,
            value=accepted_value,
            source_evidence_id=proposal["source_evidence_id"],
        )
        applicability_changes = _resolve_booking_applicability(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
        )
        status_value = "CORRECTED" if corrected else "ACCEPTED"
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_capture_proposals
                SET proposal_status=:status,
                    accepted_value=CAST(:accepted_value AS jsonb),
                    accepted_by_actor_id=:actor_id,
                    accepted_by_role=:actor_role,
                    accepted_at_utc=now(),
                    owning_domain_key=:domain,
                    owning_record_reference=:record_reference,
                    version_no=version_no+1,
                    updated_at_utc=now()
                WHERE tenant_id=:tenant_id AND capture_proposal_id=:proposal_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "proposal_id": proposal_id,
                "status": status_value,
                "accepted_value": json.dumps({"value": accepted_value}, default=str),
                "actor_id": human_principal.subject,
                "actor_role": context["operating_role"],
                "domain": domain,
                "record_reference": record_reference,
            },
        )
        next_version = int(state["version_no"]) + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='BOOKING_IN_PROGRESS',
                    audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                    latest_activity_at_utc=now(), updated_at_utc=now(), version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="EXTRACTION_PROPOSAL_CORRECTED" if corrected else "EXTRACTION_PROPOSAL_ACCEPTED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={
                "proposalId": str(proposal_id),
                "fieldKey": proposal["field_key"],
                "sourceEvidenceId": str(proposal["source_evidence_id"]),
                "owningDomainKey": domain,
                "machineValuePreserved": True,
                "applicabilityChanges": applicability_changes,
            },
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "proposalId": str(proposal_id),
            "fieldKey": proposal["field_key"],
            "status": status_value,
            "proposedValue": machine_value,
            "acceptedValue": accepted_value,
            "owningDomainKey": domain,
            "owningRecordReference": record_reference,
            "aggregateVersion": next_version,
            "eventId": str(event_id),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.proposal.{'correct' if corrected else 'accept'}:{proposal_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "proposalId": str(proposal_id),
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
    )
    _set_etag(response, body)
    return body


@router.post("/extraction-proposals/{proposal_id}/accept")
def accept_extraction_proposal(
    tenant_id: str,
    journey_id: UUID,
    proposal_id: UUID,
    payload: ProposalDecision,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    return _decide_proposal(
        tenant_id=tenant_id,
        journey_id=journey_id,
        proposal_id=proposal_id,
        payload=payload,
        corrected=False,
        request=request,
        response=response,
        idempotency_key=idempotency_key,
        if_match=if_match,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )


@router.post("/extraction-proposals/{proposal_id}/correct")
def correct_extraction_proposal(
    tenant_id: str,
    journey_id: UUID,
    proposal_id: UUID,
    payload: ProposalDecision,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    return _decide_proposal(
        tenant_id=tenant_id,
        journey_id=journey_id,
        proposal_id=proposal_id,
        payload=payload,
        corrected=True,
        request=request,
        response=response,
        idempotency_key=idempotency_key,
        if_match=if_match,
        human_principal=human_principal,
        authorization_client=authorization_client,
        connection=connection,
    )


@router.post("/booking/extraction/refresh", response_model=ExtractionRefreshResponse)
def refresh_booking_extraction(
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
    _require_active_booking(state)
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
            supported = _SUPPORTED_PROPOSAL_FIELDS.get(document_type, set())
            for fact in facts:
                if fact.field_key not in supported:
                    continue
                connection.execute(
                    text(
                        """
                        UPDATE auditcore.journey_capture_proposals
                        SET proposal_status='SUPERSEDED', updated_at_utc=now()
                        WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                          AND stage_code='BOOKING' AND source_evidence_id=:evidence_id
                          AND field_key=:field_key AND proposal_status='PENDING'
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
                            value_source, proposed_value, confidence_score,
                            owning_domain_key
                        ) VALUES (
                            :tenant_id, :journey_id, 'BOOKING', :field_key,
                            :evidence_id, :fact_id, :fact_version,
                            :document_type, :value_source,
                            CAST(:proposed_value AS jsonb), :confidence,
                            :owning_domain
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
                        "proposed_value": json.dumps({"value": fact.value}, default=str),
                        "confidence": fact.confidence_score,
                        "owning_domain": _PROPOSAL_CAPTURE_MAP.get(fact.field_key),
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


@router.get("/processing-status")
def get_booking_processing_status(
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
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    _require_active_booking(state)
    documents = _document_views(connection, tenant_id, journey_id)
    pending = 0
    failed = 0
    for document in documents:
        processing = (document["processingStatus"] or "").upper()
        if processing in {"FAILED", "ERROR", "REJECTED"}:
            failed += 1
        elif document["evidenceId"] and processing not in _TERMINAL_PROCESSING_STATUSES:
            pending += 1
    proposal_count = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.journey_capture_proposals
            WHERE tenant_id=:tenant_id AND journey_id=:journey_id
              AND stage_code='BOOKING' AND proposal_status='PENDING'
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one()
    return {
        "version": int(state["version_no"]),
        "pendingCount": pending,
        "readyProposalCount": int(proposal_count),
        "failedCount": failed,
        "documents": documents,
        "userMessage": (
            "One or more documents need attention. Retry processing or upload a clearer document."
            if failed
            else None
        ),
    }


@router.get("/flags")
def list_booking_flags(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[dict[str, Any]]:
    _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    return _flags(connection, tenant_id, journey_id)


@router.post("/flags")
def create_booking_flag(
    tenant_id: str,
    journey_id: UUID,
    payload: HumanFlagCommand,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    category = payload.category.strip().upper()
    severity = payload.severity.strip().upper()
    if category not in _HUMAN_FLAG_CATEGORIES or severity not in _SEVERITIES:
        raise AuditCoreError(
            error_code="VAC-VAL-002",
            status_code=422,
            title="Business validation failed",
            detail="The selected Booking flag category or severity is not enabled.",
        )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_active_booking(state)
        for evidence_id in payload.evidenceIds:
            _validate_evidence_for_journey(
                connection,
                tenant_id=tenant_id,
                journey_id=journey_id,
                evidence_id=evidence_id,
            )
        flag_id = connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_findings (
                    tenant_id, journey_id, finding_type_code, severity,
                    finding_status, title, description, created_by_actor_id,
                    correlation_id, stage_code, origin_kind, origin_actor_id,
                    origin_role_snapshot, blocking_completion
                ) VALUES (
                    :tenant_id, :journey_id, :category, :severity,
                    'OPEN', :title, :description, :actor_id,
                    :correlation_id, 'BOOKING', 'HUMAN', :actor_id,
                    :actor_role, false
                ) RETURNING audit_finding_id
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "category": category,
                "severity": severity,
                "title": payload.summary.strip(),
                "description": (payload.remarks or "").strip() or None,
                "actor_id": human_principal.subject,
                "actor_role": context["operating_role"],
                "correlation_id": correlation_id,
            },
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO auditcore.audit_finding_events (
                    tenant_id, audit_finding_id, journey_id, stage_code,
                    event_type, actor_id, actor_role_snapshot, safe_payload,
                    correlation_id
                ) VALUES (
                    :tenant_id, :flag_id, :journey_id, 'BOOKING',
                    'RAISED', :actor_id, :actor_role, CAST(:payload AS jsonb),
                    :correlation_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "flag_id": flag_id,
                "journey_id": journey_id,
                "actor_id": human_principal.subject,
                "actor_role": context["operating_role"],
                "payload": json.dumps(
                    {
                        "originKind": "HUMAN",
                        "category": category,
                        "evidenceIds": [str(value) for value in payload.evidenceIds],
                    }
                ),
                "correlation_id": correlation_id,
            },
        )
        next_version = int(state["version_no"]) + 1
        connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET audit_status='FLAGS_RAISED',
                    audit_state=CASE WHEN audit_state='NOT_STARTED' THEN 'IN_PROGRESS' ELSE audit_state END,
                    latest_activity_at_utc=now(), updated_at_utc=now(), version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id AND stage_code='BOOKING'
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id, "version": next_version},
        )
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_FLAG_RAISED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={"flagId": str(flag_id), "category": category, "severity": severity},
            aggregate_version=next_version,
        )
        return {
            "journeyId": str(journey_id),
            "flagId": str(flag_id),
            "category": category,
            "severity": severity,
            "status": "OPEN",
            "aggregateVersion": next_version,
            "eventId": str(event_id),
        }

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.flag.create:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={
            "expectedVersion": expected_version,
            "payload": payload.model_dump(mode="json"),
        },
        execute=execute,
    )
    _set_etag(response, body)
    return body


@router.post("/booking/close-ready", response_model=BookingCommandResponse)
def close_booking_ready(
    tenant_id: str,
    journey_id: UUID,
    request: Request,
    response: Response,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)],
    if_match: Annotated[str, Header(alias="If-Match", min_length=1, max_length=64)],
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> BookingCommandResponse:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    expected_version = _parse_if_match(if_match)
    correlation_id = get_correlation_id(request)

    def execute() -> dict[str, Any]:
        _aggregate_lock(connection, tenant_id=tenant_id, journey_id=journey_id)
        state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
        _require_expected_version(state, expected_version)
        _require_transition(
            state,
            allowed=_ACTIVE_BOOKING_STATUSES,
            action="closed ready for Delivery",
        )
        completion = _completion_summary(connection, tenant_id, journey_id)
        if not completion["ready"]:
            raise ConflictError(
                error_code="VAC-CONFLICT-009",
                title="Booking checkpoint is incomplete",
                detail="Complete the outstanding Booking audit work before closing ready for Delivery.",
            )
        open_flags = connection.execute(
            text(
                """
                SELECT count(*) FROM auditcore.audit_findings
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING' AND finding_status IN ('OPEN','ACKNOWLEDGED')
                """
            ),
            {"tenant_id": tenant_id, "journey_id": journey_id},
        ).scalar_one()
        next_version = int(state["version_no"]) + 1
        row = connection.execute(
            text(
                """
                UPDATE auditcore.journey_stage_states
                SET business_status='BOOKING_CLOSED',
                    closure_disposition='PROCEED_TO_DELIVERY',
                    audit_state='COMPLETE',
                    audit_status=:audit_status,
                    capture_completed_at_utc=now(),
                    business_completed_at_utc=now(),
                    closed_by_actor_id=:actor_id,
                    closed_at_utc=now(),
                    latest_activity_at_utc=now(),
                    updated_at_utc=now(),
                    version_no=:version
                WHERE tenant_id=:tenant_id AND journey_id=:journey_id
                  AND stage_code='BOOKING'
                RETURNING journey_id, business_status, closure_disposition,
                          audit_state, audit_status, close_reason_code,
                          closure_remarks, latest_activity_at_utc, version_no
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "actor_id": human_principal.subject,
                "audit_status": "FLAGS_RAISED" if open_flags else "NO_FLAGS",
                "version": next_version,
            },
        ).mappings().one()
        event_id = _append_workflow_event(
            connection,
            tenant_id=tenant_id,
            journey_id=journey_id,
            event_type="BOOKING_CLOSED",
            source_kind="HUMAN",
            actor_id=human_principal.subject,
            actor_role_snapshot=context["operating_role"],
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            safe_payload={"closureDisposition": "PROCEED_TO_DELIVERY"},
            aggregate_version=next_version,
        )
        return _build_response(row, event_id=event_id)

    body, _ = execute_idempotent_json_command(
        connection,
        tenant_id=tenant_id,
        operation_key=f"uc03.booking.close-ready:{journey_id}",
        idempotency_key=idempotency_key,
        request_payload={"expectedVersion": expected_version},
        execute=execute,
    )
    _set_etag(response, body)
    return BookingCommandResponse.model_validate(body)


@router.get("/uc03-workspace")
def get_booking_workspace(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> dict[str, Any]:
    context = _scope(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        human_principal=human_principal,
        authorization_client=authorization_client,
    )
    state = _stage_state(connection, tenant_id=tenant_id, journey_id=journey_id)
    if state is None:
        return {
            "journeyId": str(journey_id),
            "bookingStage": {
                "businessStatus": None,
                "auditState": "NOT_STARTED",
                "auditStatus": "NOT_EVALUATED",
            },
            "capture": _capture_snapshot(connection, tenant_id, journey_id),
            "documents": [],
            "proposals": [],
            "flags": [],
            "completion": {"ready": False, "blockers": [{"code": "BOOKING_NOT_STARTED", "label": "Start Booking"}]},
            "permittedActions": ["START_BOOKING"],
            "aggregateVersion": 0,
            "operatingRole": context["operating_role"],
        }
    documents = _document_views(connection, tenant_id, journey_id)
    proposals = _proposals(connection, tenant_id, journey_id)
    flags = _flags(connection, tenant_id, journey_id)
    completion = _completion_summary(connection, tenant_id, journey_id)
    permitted = []
    if state["business_status"] in _ACTIVE_BOOKING_STATUSES:
        permitted.extend(
            [
                "CAPTURE",
                "UPLOAD_DOCUMENT",
                "ASSESS_DOCUMENT",
                "CREATE_FLAG",
                "CLOSE_NO_DELIVERY",
                "CANCEL_BOOKING",
                "MARK_DUPLICATE",
            ]
        )
        if completion["ready"]:
            permitted.append("CLOSE_READY")
    return {
        "journeyId": str(journey_id),
        "bookingStage": {
            "businessStatus": state["business_status"],
            "closureDisposition": state["closure_disposition"],
            "auditState": state["audit_state"],
            "auditStatus": state["audit_status"],
            "closeReasonCode": state["close_reason_code"],
            "closureRemarks": state["closure_remarks"],
        },
        "capture": _capture_snapshot(connection, tenant_id, journey_id),
        "documents": documents,
        "proposals": proposals,
        "flags": flags,
        "completion": completion,
        "processingSummary": {
            "pendingCount": sum(
                1
                for item in documents
                if item["evidenceId"]
                and (item["processingStatus"] or "").upper() not in _TERMINAL_PROCESSING_STATUSES
                and (item["processingStatus"] or "").upper() not in {"FAILED", "ERROR", "REJECTED"}
            ),
            "failedCount": sum(
                1
                for item in documents
                if (item["processingStatus"] or "").upper() in {"FAILED", "ERROR", "REJECTED"}
            ),
            "readyProposalCount": sum(1 for item in proposals if item["status"] == "PENDING"),
        },
        "flagSummary": {
            "openCount": sum(1 for item in flags if item["status"] in {"OPEN", "ACKNOWLEDGED"}),
            "totalCount": len(flags),
        },
        "permittedActions": permitted,
        "aggregateVersion": int(state["version_no"]),
        "operatingRole": context["operating_role"],
    }
