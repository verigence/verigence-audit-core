from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import Connection, text

from audit_core.authorization import authorize
from audit_core.business_assignments import require_business_scope
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_principal
from audit_core.errors import NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["payments-finance"])

SourceKind = Literal["EVIDENCE", "OPERATIONAL_INPUT", "SOURCE_SYSTEM"]
VerificationResult = Literal["VERIFIED", "EXCEPTION", "REJECTED", "REVIEW_REQUIRED"]


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paymentAtUtc: datetime | None = None
    amount: Decimal = Field(ge=0)
    currencyCode: str = Field(default="INR", min_length=3, max_length=3)
    paymentMethodCode: str | None = Field(default=None, max_length=80)
    paymentReference: str | None = Field(default=None, max_length=240)
    actualStatusCode: str | None = Field(default=None, max_length=100)
    statusSource: SourceKind | None = None
    sourceEvidenceId: UUID | None = None


class PaymentVerificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: VerificationResult
    notes: str | None = None
    verifiedByRoleCode: str | None = Field(default=None, max_length=80)
    sourceEvidenceId: UUID | None = None


class PaymentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paymentId: UUID
    paymentAtUtc: datetime | None = None
    amount: Decimal | None = Field(default=None, ge=0)
    paymentMethodCode: str | None = Field(default=None, max_length=80)
    paymentReference: str | None = Field(default=None, max_length=240)
    actualStatusCode: str | None = Field(default=None, max_length=100)
    statusSource: SourceKind | None = None
    sourceEvidenceId: UUID | None = None
    verification: PaymentVerificationInput | None = None

    @model_validator(mode="after")
    def require_change(self):
        changed = any(
            value is not None
            for value in (
                self.paymentAtUtc,
                self.amount,
                self.paymentMethodCode,
                self.paymentReference,
                self.actualStatusCode,
                self.statusSource,
                self.sourceEvidenceId,
                self.verification,
            )
        )
        if not changed:
            raise ValueError("At least one payment field or verification is required")
        return self


class VerificationResponse(BaseModel):
    paymentVerificationEventId: UUID
    result: str
    notes: str | None
    verifiedByActorId: str
    verifiedByRoleCode: str | None
    sourceEvidenceId: UUID | None
    occurredAtUtc: datetime


class PaymentResponse(BaseModel):
    paymentId: UUID
    journeyId: UUID
    paymentAtUtc: datetime | None
    amount: Decimal
    currencyCode: str
    paymentMethodCode: str | None
    paymentReference: str | None
    actualStatusCode: str | None
    statusSource: str | None
    sourceEvidenceId: UUID | None
    versionNo: int
    verifications: list[VerificationResponse] = Field(default_factory=list)


class FinancePut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    financeTypeCode: str | None = Field(default=None, max_length=80)
    providerName: str | None = Field(default=None, max_length=240)
    doReference: str | None = Field(default=None, max_length=240)
    poReference: str | None = Field(default=None, max_length=240)
    financedAmount: Decimal | None = None
    actualStatusCode: str | None = Field(default=None, max_length=100)
    sourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None
    details: dict = Field(default_factory=dict)


class FinanceResponse(FinancePut):
    financeRecordId: UUID
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


def _authorize_scope(
    connection: Connection,
    principal: Principal,
    *,
    tenant_id: str,
    journey_id: UUID,
):
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )


def _verification_rows(
    connection: Connection,
    *,
    tenant_id: str,
    payment_id: UUID,
) -> list[VerificationResponse]:
    rows = connection.execute(
        text(
            """
            SELECT payment_verification_event_id, verification_result,
                   verification_notes, verified_by_actor_id, verified_by_role_code,
                   source_evidence_id, occurred_at_utc
            FROM auditcore.payment_verification_events
            WHERE tenant_id = :tenant_id AND payment_id = :payment_id
            ORDER BY occurred_at_utc, payment_verification_event_id
            """
        ),
        {"tenant_id": tenant_id, "payment_id": payment_id},
    ).mappings().all()
    return [
        VerificationResponse(
            paymentVerificationEventId=row["payment_verification_event_id"],
            result=row["verification_result"],
            notes=row["verification_notes"],
            verifiedByActorId=row["verified_by_actor_id"],
            verifiedByRoleCode=row["verified_by_role_code"],
            sourceEvidenceId=row["source_evidence_id"],
            occurredAtUtc=row["occurred_at_utc"],
        )
        for row in rows
    ]


def _payment_response(connection: Connection, tenant_id: str, payment_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT payment_id, journey_id, payment_at_utc, amount, currency_code,
                   payment_method_code, payment_reference, actual_status_code,
                   status_source, source_evidence_id, version_no
            FROM auditcore.payments
            WHERE tenant_id = :tenant_id AND payment_id = :payment_id
            """
        ),
        {"tenant_id": tenant_id, "payment_id": payment_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-007",
            title="Payment not found",
            detail="Payment not found for the requested Journey.",
        )
    return PaymentResponse(
        paymentId=row["payment_id"],
        journeyId=row["journey_id"],
        paymentAtUtc=row["payment_at_utc"],
        amount=row["amount"],
        currencyCode=row["currency_code"],
        paymentMethodCode=row["payment_method_code"],
        paymentReference=row["payment_reference"],
        actualStatusCode=row["actual_status_code"],
        statusSource=row["status_source"],
        sourceEvidenceId=row["source_evidence_id"],
        versionNo=row["version_no"],
        verifications=_verification_rows(
            connection,
            tenant_id=tenant_id,
            payment_id=payment_id,
        ),
    )


@router.get("/journeys/{journey_id}/payments", response_model=list[PaymentResponse])
def list_payments(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> list[PaymentResponse]:
    authorize(principal, tenant_id=tenant_id, permission="audit.payment.read")
    set_tenant_context(connection, tenant_id)
    _authorize_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    ids = connection.execute(
        text(
            """
            SELECT payment_id
            FROM auditcore.payments
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc, payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalars().all()
    return [_payment_response(connection, tenant_id, payment_id) for payment_id in ids]


@router.post("/journeys/{journey_id}/payments", response_model=PaymentResponse, status_code=201)
def create_payment(
    tenant_id: str,
    journey_id: UUID,
    payload: PaymentCreate,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PaymentResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.payment.write")
    set_tenant_context(connection, tenant_id)
    _authorize_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    payment_id = connection.execute(
        text(
            """
            INSERT INTO auditcore.payments (
                tenant_id, journey_id, payment_at_utc, amount, currency_code,
                payment_method_code, payment_reference, actual_status_code,
                status_source, source_evidence_id
            ) VALUES (
                :tenant_id, :journey_id, :payment_at_utc, :amount, :currency_code,
                :payment_method_code, :payment_reference, :actual_status_code,
                :status_source, :source_evidence_id
            ) RETURNING payment_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "payment_at_utc": payload.paymentAtUtc,
            "amount": payload.amount,
            "currency_code": payload.currencyCode.upper(),
            "payment_method_code": payload.paymentMethodCode,
            "payment_reference": payload.paymentReference,
            "actual_status_code": payload.actualStatusCode,
            "status_source": payload.statusSource,
            "source_evidence_id": payload.sourceEvidenceId,
        },
    ).scalar_one()
    return _payment_response(connection, tenant_id, payment_id)


@router.patch("/journeys/{journey_id}/payments", response_model=PaymentResponse)
def patch_payment(
    tenant_id: str,
    journey_id: UUID,
    payload: PaymentPatch,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> PaymentResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.payment.write")
    set_tenant_context(connection, tenant_id)
    _authorize_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    current = connection.execute(
        text(
            """
            SELECT payment_id
            FROM auditcore.payments
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND payment_id = :payment_id
            """
        ),
        {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "payment_id": payload.paymentId,
        },
    ).scalar_one_or_none()
    if current is None:
        raise NotFoundError(
            error_code="VAC-NF-007",
            title="Payment not found",
            detail="Payment not found for the requested Journey.",
        )

    updates = payload.model_fields_set - {"paymentId", "verification"}
    column_by_field = {
        "paymentAtUtc": "payment_at_utc",
        "amount": "amount",
        "paymentMethodCode": "payment_method_code",
        "paymentReference": "payment_reference",
        "actualStatusCode": "actual_status_code",
        "statusSource": "status_source",
        "sourceEvidenceId": "source_evidence_id",
    }
    if updates:
        assignments = [f"{column_by_field[field]} = :{field}" for field in updates]
        assignments.extend(["updated_at_utc = now()", "version_no = version_no + 1"])
        params = {field: getattr(payload, field) for field in updates}
        params.update({"tenant_id": tenant_id, "payment_id": payload.paymentId})
        connection.execute(
            text(
                "UPDATE auditcore.payments SET "
                + ", ".join(assignments)
                + " WHERE tenant_id = :tenant_id AND payment_id = :payment_id"
            ),
            params,
        )

    if payload.verification is not None:
        authorize(principal, tenant_id=tenant_id, permission="audit.payment.verify")
        connection.execute(
            text(
                """
                INSERT INTO auditcore.payment_verification_events (
                    tenant_id, journey_id, payment_id, verification_result,
                    verification_notes, verified_by_actor_id, verified_by_role_code,
                    source_evidence_id
                ) VALUES (
                    :tenant_id, :journey_id, :payment_id, :result,
                    :notes, :actor_id, :role_code, :source_evidence_id
                )
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "payment_id": payload.paymentId,
                "result": payload.verification.result,
                "notes": payload.verification.notes,
                "actor_id": principal.subject,
                "role_code": payload.verification.verifiedByRoleCode,
                "source_evidence_id": payload.verification.sourceEvidenceId,
            },
        )

    return _payment_response(connection, tenant_id, payload.paymentId)


def _finance_response(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT finance_record_id, journey_id, finance_type_code, provider_name,
                   do_reference, po_reference, financed_amount, actual_status_code,
                   source_kind, source_evidence_id, details
            FROM auditcore.finance_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc DESC, finance_record_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        raise NotFoundError(
            error_code="VAC-NF-008",
            title="Finance record not found",
            detail="Finance record not found for the requested Journey.",
        )
    return FinanceResponse(
        financeRecordId=row["finance_record_id"],
        journeyId=row["journey_id"],
        financeTypeCode=row["finance_type_code"],
        providerName=row["provider_name"],
        doReference=row["do_reference"],
        poReference=row["po_reference"],
        financedAmount=row["financed_amount"],
        actualStatusCode=row["actual_status_code"],
        sourceKind=row["source_kind"],
        sourceEvidenceId=row["source_evidence_id"],
        details=row["details"],
    )


@router.get("/journeys/{journey_id}/finance", response_model=FinanceResponse)
def get_finance(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FinanceResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.payment.read")
    set_tenant_context(connection, tenant_id)
    _authorize_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    return _finance_response(connection, tenant_id, journey_id)


@router.put("/journeys/{journey_id}/finance", response_model=FinanceResponse)
def put_finance(
    tenant_id: str,
    journey_id: UUID,
    payload: FinancePut,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> FinanceResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.payment.write")
    set_tenant_context(connection, tenant_id)
    _authorize_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        journey_id=journey_id,
    )
    current_id = connection.execute(
        text(
            """
            SELECT finance_record_id
            FROM auditcore.finance_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc DESC, finance_record_id DESC
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).scalar_one_or_none()
    params = {
        "tenant_id": tenant_id,
        "journey_id": journey_id,
        "finance_type_code": payload.financeTypeCode,
        "provider_name": payload.providerName,
        "do_reference": payload.doReference,
        "po_reference": payload.poReference,
        "financed_amount": payload.financedAmount,
        "actual_status_code": payload.actualStatusCode,
        "source_kind": payload.sourceKind,
        "source_evidence_id": payload.sourceEvidenceId,
        "details": __import__("json").dumps(payload.details),
    }
    if current_id is None:
        connection.execute(
            text(
                """
                INSERT INTO auditcore.finance_records (
                    tenant_id, journey_id, finance_type_code, provider_name,
                    do_reference, po_reference, financed_amount, actual_status_code,
                    source_kind, source_evidence_id, details
                ) VALUES (
                    :tenant_id, :journey_id, :finance_type_code, :provider_name,
                    :do_reference, :po_reference, :financed_amount, :actual_status_code,
                    :source_kind, :source_evidence_id, CAST(:details AS jsonb)
                )
                """
            ),
            params,
        )
    else:
        connection.execute(
            text(
                """
                UPDATE auditcore.finance_records
                SET finance_type_code = :finance_type_code,
                    provider_name = :provider_name,
                    do_reference = :do_reference,
                    po_reference = :po_reference,
                    financed_amount = :financed_amount,
                    actual_status_code = :actual_status_code,
                    source_kind = :source_kind,
                    source_evidence_id = :source_evidence_id,
                    details = CAST(:details AS jsonb),
                    updated_at_utc = now()
                WHERE tenant_id = :tenant_id
                  AND finance_record_id = :finance_record_id
                """
            ),
            {**params, "finance_record_id": current_id},
        )
    return _finance_response(connection, tenant_id, journey_id)
