import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError, authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal, get_principal
from audit_core.errors import DependencyUnavailableError, NotFoundError
from audit_core.security import HumanPrincipal, Principal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["insurance-trade-in"])
SourceKind = Literal["EVIDENCE", "OPERATIONAL_INPUT", "SOURCE_SYSTEM"]


class AddonInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    journeyAddonId: UUID | None = None
    addonTypeCode: str = Field(max_length=80)
    providerName: str | None = Field(default=None, max_length=240)
    standardAmount: Decimal | None = None
    actualAmount: Decimal | None = None
    referenceNumber: str | None = Field(default=None, max_length=240)
    sourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None
    details: dict = Field(default_factory=dict)


class AddonResponse(AddonInput):
    journeyAddonId: UUID


class InsurancePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    insurerName: str | None = Field(default=None, max_length=240)
    policyReference: str | None = Field(default=None, max_length=240)
    coverNoteReference: str | None = Field(default=None, max_length=240)
    standardPremiumAmount: Decimal | None = None
    actualPremiumAmount: Decimal | None = None
    selfInsuranceFlag: bool | None = None
    actualStatusCode: str | None = Field(default=None, max_length=100)
    agentIntermediaryName: str | None = Field(default=None, max_length=200)
    agentIntermediaryCode: str | None = Field(default=None, max_length=80)
    mispCode: str | None = Field(default=None, max_length=80)
    insuranceAddOns: list[str] | None = None
    sourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None
    addons: list[AddonInput] = Field(default_factory=list)


class InsuranceResponse(BaseModel):
    insuranceRecordId: UUID
    journeyId: UUID
    insurerName: str | None
    policyReference: str | None
    coverNoteReference: str | None
    standardPremiumAmount: Decimal | None
    actualPremiumAmount: Decimal | None
    selfInsuranceFlag: bool | None
    actualStatusCode: str | None
    agentIntermediaryName: str | None
    agentIntermediaryCode: str | None
    mispCode: str | None
    insuranceAddOns: list[str] | None
    sourceKind: str | None
    sourceEvidenceId: UUID | None
    addons: list[AddonResponse]


class TradeInPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actualStatusCode: str | None = Field(default=None, max_length=100)
    oldVehicleRegistration: str | None = Field(default=None, max_length=120)
    oldVehicleMakeModel: str | None = Field(default=None, max_length=240)
    quotedValue: Decimal | None = None
    actualValue: Decimal | None = None
    handoverAtUtc: datetime | None = None
    paymentAtUtc: datetime | None = None
    resaleAtUtc: datetime | None = None
    sourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None
    details: dict = Field(default_factory=dict)


class TradeInResponse(TradeInPut):
    tradeInCaseId: UUID
    journeyId: UUID


def _journey_scope(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT dealer_id, outlet_id
            FROM auditcore.journeys
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
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


def _scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    journey_id: UUID,
) -> None:
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )


def _authorize_read_human(
    authorization_client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
    permission: str,
) -> None:
    """Same permission check as authorize()/get_principal, for a caller on
    the newer thin HumanPrincipal (get_human_principal) instead -- see
    get_insurance's own fix for why this endpoint needed it."""
    try:
        decision = authorization_client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=permission,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="This is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _scope_human(
    connection: Connection,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
    journey_id: UUID,
) -> None:
    """Same business-scope check as _scope()/require_business_scope, for a
    caller on the newer thin HumanPrincipal instead -- require_business_
    scope needs a full Principal (.tenant_id, .subject), which HumanPrincipal
    deliberately doesn't carry. Kept local rather than widening require_
    business_scope's own signature, which other still-on-get_principal
    callers depend on."""
    journey = _journey_scope(connection, tenant_id, journey_id)
    assigned = connection.execute(
        text(
            """
            SELECT 1
            FROM auditcore.business_assignments
            WHERE tenant_id = :tenant_id
              AND security_actor_id = :actor_id
              AND assignment_status = 'ACTIVE'
              AND effective_from <= now()
              AND (effective_to IS NULL OR effective_to >= now())
              AND (
                    dealer_id IS NULL
                    OR (
                        dealer_id = :dealer_id
                        AND (outlet_id IS NULL OR outlet_id = :outlet_id)
                    )
              )
            LIMIT 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": human_principal.subject,
            "dealer_id": journey["dealer_id"],
            "outlet_id": journey["outlet_id"],
        },
    ).scalar_one_or_none()
    if assigned is None:
        raise AuthorizationError(
            error_code="VAC-AUTH-004",
            status_code=403,
            title="Business scope denied",
        )


def _addons(connection: Connection, tenant_id: str, journey_id: UUID):
    rows = connection.execute(
        text(
            """
            SELECT journey_addon_id, addon_type_code, provider_name,
                   standard_amount, actual_amount, reference_number,
                   source_kind, source_evidence_id, details
            FROM auditcore.journey_addons
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc, journey_addon_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        AddonResponse(
            journeyAddonId=row["journey_addon_id"],
            addonTypeCode=row["addon_type_code"],
            providerName=row["provider_name"],
            standardAmount=row["standard_amount"],
            actualAmount=row["actual_amount"],
            referenceNumber=row["reference_number"],
            sourceKind=row["source_kind"],
            sourceEvidenceId=row["source_evidence_id"],
            details=row["details"],
        )
        for row in rows
    ]


def _insurance(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT insurance_record_id, journey_id, insurer_name, policy_reference,
                   cover_note_reference, standard_premium_amount,
                   actual_premium_amount, self_insurance_flag, actual_status_code,
                   agent_intermediary_name, agent_intermediary_code, misp_code,
                   add_ons, source_kind, source_evidence_id
            FROM auditcore.insurance_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-009",
            title="Insurance record not found",
            detail="Insurance record not found for the requested Journey.",
        )
    return InsuranceResponse(
        insuranceRecordId=row["insurance_record_id"],
        journeyId=row["journey_id"],
        insurerName=row["insurer_name"],
        policyReference=row["policy_reference"],
        coverNoteReference=row["cover_note_reference"],
        standardPremiumAmount=row["standard_premium_amount"],
        actualPremiumAmount=row["actual_premium_amount"],
        selfInsuranceFlag=row["self_insurance_flag"],
        actualStatusCode=row["actual_status_code"],
        agentIntermediaryName=row["agent_intermediary_name"],
        agentIntermediaryCode=row["agent_intermediary_code"],
        mispCode=row["misp_code"],
        insuranceAddOns=row["add_ons"],
        sourceKind=row["source_kind"],
        sourceEvidenceId=row["source_evidence_id"],
        addons=_addons(connection, tenant_id, journey_id),
    )


@router.get("/journeys/{journey_id}/insurance", response_model=InsuranceResponse)
def get_insurance(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> InsuranceResponse:
    # Confirmed live: this endpoint was on get_principal, which requires a
    # JWT carrying tenant_id/permissions claims directly -- the human token
    # format actually issued today deliberately carries neither (permissions
    # are checked live against the Security service instead), so every real
    # PC/TL request here has been a guaranteed 401, never a transient one.
    _authorize_read_human(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
        permission="audit.journey.read",
    )
    set_tenant_context(connection, tenant_id)
    _scope_human(connection, human_principal=human_principal, tenant_id=tenant_id, journey_id=journey_id)
    return _insurance(connection, tenant_id, journey_id)


@router.put("/journeys/{journey_id}/insurance", response_model=InsuranceResponse)
def put_insurance(
    tenant_id: str,
    journey_id: UUID,
    payload: InsurancePut,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> InsuranceResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.update")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.insurance_records (
                tenant_id, journey_id, insurer_name, policy_reference,
                cover_note_reference, standard_premium_amount,
                actual_premium_amount, self_insurance_flag, actual_status_code,
                agent_intermediary_name, agent_intermediary_code, misp_code,
                add_ons, source_kind, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :insurer_name, :policy_reference,
                :cover_note_reference, :standard_premium_amount,
                :actual_premium_amount, :self_insurance_flag, :actual_status_code,
                :agent_intermediary_name, :agent_intermediary_code, :misp_code,
                CAST(:add_ons AS jsonb), :source_kind, :source_evidence_id
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                insurer_name = EXCLUDED.insurer_name,
                policy_reference = EXCLUDED.policy_reference,
                cover_note_reference = EXCLUDED.cover_note_reference,
                standard_premium_amount = EXCLUDED.standard_premium_amount,
                actual_premium_amount = EXCLUDED.actual_premium_amount,
                self_insurance_flag = EXCLUDED.self_insurance_flag,
                actual_status_code = EXCLUDED.actual_status_code,
                agent_intermediary_name = EXCLUDED.agent_intermediary_name,
                agent_intermediary_code = EXCLUDED.agent_intermediary_code,
                misp_code = EXCLUDED.misp_code,
                add_ons = EXCLUDED.add_ons,
                source_kind = EXCLUDED.source_kind,
                source_evidence_id = EXCLUDED.source_evidence_id,
                updated_at_utc = now(),
                version_no = auditcore.insurance_records.version_no + 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "insurer_name": payload.insurerName,
            "policy_reference": payload.policyReference,
            "cover_note_reference": payload.coverNoteReference,
            "standard_premium_amount": payload.standardPremiumAmount,
            "actual_premium_amount": payload.actualPremiumAmount,
            "self_insurance_flag": payload.selfInsuranceFlag,
            "actual_status_code": payload.actualStatusCode,
            "agent_intermediary_name": payload.agentIntermediaryName,
            "agent_intermediary_code": payload.agentIntermediaryCode,
            "misp_code": payload.mispCode,
            "add_ons": json.dumps(payload.insuranceAddOns) if payload.insuranceAddOns is not None else None,
            "source_kind": payload.sourceKind,
            "source_evidence_id": payload.sourceEvidenceId,
        },
    )
    for addon in payload.addons:
        params = {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "addon_type_code": addon.addonTypeCode,
            "provider_name": addon.providerName,
            "standard_amount": addon.standardAmount,
            "actual_amount": addon.actualAmount,
            "reference_number": addon.referenceNumber,
            "source_kind": addon.sourceKind,
            "source_evidence_id": addon.sourceEvidenceId,
            "details": json.dumps(addon.details),
        }
        if addon.journeyAddonId is None:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.journey_addons (
                        tenant_id, journey_id, addon_type_code, provider_name,
                        standard_amount, actual_amount, reference_number,
                        source_kind, source_evidence_id, details
                    ) VALUES (
                        :tenant_id, :journey_id, :addon_type_code, :provider_name,
                        :standard_amount, :actual_amount, :reference_number,
                        :source_kind, :source_evidence_id, CAST(:details AS jsonb)
                    )
                    """
                ),
                params,
            )
        else:
            result = connection.execute(
                text(
                    """
                    UPDATE auditcore.journey_addons
                    SET addon_type_code = :addon_type_code,
                        provider_name = :provider_name,
                        standard_amount = :standard_amount,
                        actual_amount = :actual_amount,
                        reference_number = :reference_number,
                        source_kind = :source_kind,
                        source_evidence_id = :source_evidence_id,
                        details = CAST(:details AS jsonb),
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id
                      AND journey_id = :journey_id
                      AND journey_addon_id = :journey_addon_id
                    """
                ),
                {**params, "journey_addon_id": addon.journeyAddonId},
            )
            if result.rowcount == 0:
                raise NotFoundError(
                    error_code="VAC-NF-010",
                    title="Add-on record not found",
                    detail="Add-on record not found for the requested Journey.",
                )
    return _insurance(connection, tenant_id, journey_id)


def _trade_in(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT trade_in_case_id, journey_id, actual_status_code,
                   old_vehicle_registration, old_vehicle_make_model,
                   quoted_value, actual_value, handover_at_utc, payment_at_utc,
                   resale_at_utc, source_kind, source_evidence_id, details
            FROM auditcore.trade_in_cases
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-011",
            title="Trade-in record not found",
            detail="Trade-in record not found for the requested Journey.",
        )
    return TradeInResponse(
        tradeInCaseId=row["trade_in_case_id"],
        journeyId=row["journey_id"],
        actualStatusCode=row["actual_status_code"],
        oldVehicleRegistration=row["old_vehicle_registration"],
        oldVehicleMakeModel=row["old_vehicle_make_model"],
        quotedValue=row["quoted_value"],
        actualValue=row["actual_value"],
        handoverAtUtc=row["handover_at_utc"],
        paymentAtUtc=row["payment_at_utc"],
        resaleAtUtc=row["resale_at_utc"],
        sourceKind=row["source_kind"],
        sourceEvidenceId=row["source_evidence_id"],
        details=row["details"],
    )


@router.get("/journeys/{journey_id}/trade-in", response_model=TradeInResponse)
def get_trade_in(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TradeInResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.trade_in.read")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    return _trade_in(connection, tenant_id, journey_id)


@router.put("/journeys/{journey_id}/trade-in", response_model=TradeInResponse)
def put_trade_in(
    tenant_id: str,
    journey_id: UUID,
    payload: TradeInPut,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> TradeInResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.trade_in.write")
    set_tenant_context(connection, tenant_id)
    _scope(connection, principal, tenant_id=tenant_id, journey_id=journey_id)
    connection.execute(
        text(
            """
            INSERT INTO auditcore.trade_in_cases (
                tenant_id, journey_id, actual_status_code,
                old_vehicle_registration, old_vehicle_make_model,
                quoted_value, actual_value, handover_at_utc, payment_at_utc,
                resale_at_utc, source_kind, source_evidence_id, details
            ) VALUES (
                :tenant_id, :journey_id, :actual_status_code,
                :old_vehicle_registration, :old_vehicle_make_model,
                :quoted_value, :actual_value, :handover_at_utc, :payment_at_utc,
                :resale_at_utc, :source_kind, :source_evidence_id, CAST(:details AS jsonb)
            )
            ON CONFLICT (tenant_id, journey_id) DO UPDATE SET
                actual_status_code = EXCLUDED.actual_status_code,
                old_vehicle_registration = EXCLUDED.old_vehicle_registration,
                old_vehicle_make_model = EXCLUDED.old_vehicle_make_model,
                quoted_value = EXCLUDED.quoted_value,
                actual_value = EXCLUDED.actual_value,
                handover_at_utc = EXCLUDED.handover_at_utc,
                payment_at_utc = EXCLUDED.payment_at_utc,
                resale_at_utc = EXCLUDED.resale_at_utc,
                source_kind = EXCLUDED.source_kind,
                source_evidence_id = EXCLUDED.source_evidence_id,
                details = EXCLUDED.details,
                updated_at_utc = now(),
                version_no = auditcore.trade_in_cases.version_no + 1
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "actual_status_code": payload.actualStatusCode,
            "old_vehicle_registration": payload.oldVehicleRegistration,
            "old_vehicle_make_model": payload.oldVehicleMakeModel,
            "quoted_value": payload.quotedValue,
            "actual_value": payload.actualValue,
            "handover_at_utc": payload.handoverAtUtc,
            "payment_at_utc": payload.paymentAtUtc,
            "resale_at_utc": payload.resaleAtUtc,
            "source_kind": payload.sourceKind,
            "source_evidence_id": payload.sourceEvidenceId,
            "details": json.dumps(payload.details),
        },
    )
    return _trade_in(connection, tenant_id, journey_id)
