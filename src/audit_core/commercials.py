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
from audit_core.errors import AuditCoreError, NotFoundError
from audit_core.security import Principal

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["commercials"])

SourceKind = Literal["EVIDENCE", "OPERATIONAL_INPUT", "SOURCE_SYSTEM", "CALCULATED"]


class CommercialLineInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    componentKey: str = Field(max_length=100)
    priceListItemId: UUID
    actualAmount: Decimal | None = None
    actualSourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None
    sourceReference: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def require_actual_provenance(self):
        if self.actualAmount is not None and self.actualSourceKind is None:
            raise ValueError("actualSourceKind is required when actualAmount is supplied")
        return self


class DiscountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    discountSchemeVersionId: UUID
    discountKey: str = Field(max_length=120)
    actualDiscountAmount: Decimal | None = None
    actualSourceKind: SourceKind | None = None
    sourceEvidenceId: UUID | None = None

    @model_validator(mode="after")
    def require_actual_provenance(self):
        if self.actualDiscountAmount is not None and self.actualSourceKind is None:
            raise ValueError(
                "actualSourceKind is required when actualDiscountAmount is supplied"
            )
        return self


class CommercialsPut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[CommercialLineInput] = Field(default_factory=list)
    discounts: list[DiscountInput] = Field(default_factory=list)


class CommercialLineResponse(BaseModel):
    commercialLineId: UUID
    componentKey: str
    standardAmount: Decimal | None
    actualAmount: Decimal | None
    currencyCode: str
    priceListItemId: UUID
    priceListVersionId: UUID
    actualSourceKind: str | None
    sourceEvidenceId: UUID | None
    sourceReference: str | None


class DiscountResponse(BaseModel):
    discountApplicationId: UUID
    discountSchemeVersionId: UUID
    discountKey: str
    standardEligibleAmount: Decimal | None
    actualDiscountAmount: Decimal | None
    actualSourceKind: str | None
    sourceEvidenceId: UUID | None


class CommercialsResponse(BaseModel):
    journeyId: UUID
    lines: list[CommercialLineResponse]
    discounts: list[DiscountResponse]


def _journey_scope(connection: Connection, tenant_id: str, journey_id: UUID):
    row = connection.execute(
        text(
            """
            SELECT j.dealer_id, j.outlet_id, jp.product_sku_id
            FROM auditcore.journeys j
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
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


def _published_price_item(
    connection: Connection,
    *,
    tenant_id: str,
    price_list_item_id: UUID,
    product_sku_id: UUID | None,
):
    row = connection.execute(
        text(
            """
            SELECT pli.price_list_item_id, pli.price_list_version_id,
                   pli.product_sku_id, pli.component_key, pli.standard_amount,
                   plv.currency_code
            FROM auditcore.price_list_items pli
            JOIN auditcore.price_list_versions plv
              ON plv.tenant_id = pli.tenant_id
             AND plv.price_list_version_id = pli.price_list_version_id
            WHERE pli.tenant_id = :tenant_id
              AND pli.price_list_item_id = :item_id
              AND plv.lifecycle_status = 'PUBLISHED'
            """
        ),
        {"tenant_id": tenant_id, "item_id": price_list_item_id},
    ).mappings().one_or_none()
    if row is None or product_sku_id is None or row["product_sku_id"] != product_sku_id:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="No effective master version",
            detail="Commercial standard must reference a published Price List item for the Journey product.",
        )
    return row


def _published_discount_benefit(
    connection: Connection,
    *,
    tenant_id: str,
    version_id: UUID,
    discount_key: str,
):
    row = connection.execute(
        text(
            """
            SELECT dsv.discount_scheme_version_id, dsb.benefit_key,
                   dsb.benefit_type, dsb.amount_value, dsb.percentage_value
            FROM auditcore.discount_scheme_versions dsv
            JOIN auditcore.discount_scheme_benefits dsb
              ON dsb.tenant_id = dsv.tenant_id
             AND dsb.discount_scheme_version_id = dsv.discount_scheme_version_id
            WHERE dsv.tenant_id = :tenant_id
              AND dsv.discount_scheme_version_id = :version_id
              AND dsv.lifecycle_status = 'PUBLISHED'
              AND dsb.benefit_key = :discount_key
            """
        ),
        {
            "tenant_id": tenant_id,
            "version_id": version_id,
            "discount_key": discount_key,
        },
    ).mappings().one_or_none()
    if row is None:
        raise AuditCoreError(
            error_code="VAC-MASTER-002",
            status_code=422,
            title="No effective master version",
            detail="Discount must reference a configured benefit in a published Discount Scheme version.",
        )
    return row


def _response(connection: Connection, tenant_id: str, journey_id: UUID) -> CommercialsResponse:
    line_rows = connection.execute(
        text(
            """
            SELECT cl.commercial_line_id, cl.component_key, cl.standard_amount,
                   cl.actual_amount, cl.currency_code, cl.price_list_item_id,
                   pli.price_list_version_id, cl.actual_source_kind,
                   cl.source_evidence_id, cl.source_reference
            FROM auditcore.commercial_lines cl
            JOIN auditcore.price_list_items pli
              ON pli.tenant_id = cl.tenant_id
             AND pli.price_list_item_id = cl.price_list_item_id
            WHERE cl.tenant_id = :tenant_id AND cl.journey_id = :journey_id
            ORDER BY cl.component_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    discount_rows = connection.execute(
        text(
            """
            SELECT discount_application_id, discount_scheme_version_id,
                   discount_key, standard_eligible_amount, actual_discount_amount,
                   actual_source_kind, source_evidence_id
            FROM auditcore.discount_applications
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY discount_key, discount_application_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return CommercialsResponse(
        journeyId=journey_id,
        lines=[
            CommercialLineResponse(
                commercialLineId=row["commercial_line_id"],
                componentKey=row["component_key"],
                standardAmount=row["standard_amount"],
                actualAmount=row["actual_amount"],
                currencyCode=row["currency_code"],
                priceListItemId=row["price_list_item_id"],
                priceListVersionId=row["price_list_version_id"],
                actualSourceKind=row["actual_source_kind"],
                sourceEvidenceId=row["source_evidence_id"],
                sourceReference=row["source_reference"],
            )
            for row in line_rows
        ],
        discounts=[
            DiscountResponse(
                discountApplicationId=row["discount_application_id"],
                discountSchemeVersionId=row["discount_scheme_version_id"],
                discountKey=row["discount_key"],
                standardEligibleAmount=row["standard_eligible_amount"],
                actualDiscountAmount=row["actual_discount_amount"],
                actualSourceKind=row["actual_source_kind"],
                sourceEvidenceId=row["source_evidence_id"],
            )
            for row in discount_rows
        ],
    )


@router.put("/journeys/{journey_id}/commercials", response_model=CommercialsResponse)
def put_commercials(
    tenant_id: str,
    journey_id: UUID,
    payload: CommercialsPut,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CommercialsResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.update")
    set_tenant_context(connection, tenant_id)
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )

    for line in payload.lines:
        master = _published_price_item(
            connection,
            tenant_id=tenant_id,
            price_list_item_id=line.priceListItemId,
            product_sku_id=journey["product_sku_id"],
        )
        if line.componentKey != master["component_key"]:
            raise AuditCoreError(
                error_code="VAC-VAL-002",
                status_code=422,
                title="Commercial component mismatch",
                detail="componentKey must match the referenced Price List item.",
            )
        connection.execute(
            text(
                """
                INSERT INTO auditcore.commercial_lines (
                    tenant_id, journey_id, component_key, standard_amount,
                    actual_amount, currency_code, price_list_item_id,
                    actual_source_kind, source_evidence_id, source_reference
                ) VALUES (
                    :tenant_id, :journey_id, :component_key, :standard_amount,
                    :actual_amount, :currency_code, :price_list_item_id,
                    :actual_source_kind, :source_evidence_id, :source_reference
                )
                ON CONFLICT (tenant_id, journey_id, component_key) DO UPDATE SET
                    standard_amount = EXCLUDED.standard_amount,
                    actual_amount = EXCLUDED.actual_amount,
                    currency_code = EXCLUDED.currency_code,
                    price_list_item_id = EXCLUDED.price_list_item_id,
                    actual_source_kind = EXCLUDED.actual_source_kind,
                    source_evidence_id = EXCLUDED.source_evidence_id,
                    source_reference = EXCLUDED.source_reference,
                    updated_at_utc = now()
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "component_key": line.componentKey,
                "standard_amount": master["standard_amount"],
                "actual_amount": line.actualAmount,
                "currency_code": master["currency_code"],
                "price_list_item_id": line.priceListItemId,
                "actual_source_kind": line.actualSourceKind,
                "source_evidence_id": line.sourceEvidenceId,
                "source_reference": line.sourceReference,
            },
        )

    for discount in payload.discounts:
        benefit = _published_discount_benefit(
            connection,
            tenant_id=tenant_id,
            version_id=discount.discountSchemeVersionId,
            discount_key=discount.discountKey,
        )
        existing_id = connection.execute(
            text(
                """
                SELECT discount_application_id
                FROM auditcore.discount_applications
                WHERE tenant_id = :tenant_id
                  AND journey_id = :journey_id
                  AND discount_scheme_version_id = :version_id
                  AND discount_key = :discount_key
                ORDER BY created_at_utc DESC
                LIMIT 1
                """
            ),
            {
                "tenant_id": tenant_id,
                "journey_id": journey_id,
                "version_id": discount.discountSchemeVersionId,
                "discount_key": discount.discountKey,
            },
        ).scalar_one_or_none()
        params = {
            "tenant_id": tenant_id,
            "journey_id": journey_id,
            "version_id": discount.discountSchemeVersionId,
            "discount_key": discount.discountKey,
            "standard_amount": benefit["amount_value"],
            "actual_amount": discount.actualDiscountAmount,
            "actual_source_kind": discount.actualSourceKind,
            "source_evidence_id": discount.sourceEvidenceId,
            "details": {
                "benefitType": benefit["benefit_type"],
                "configuredPercentage": (
                    str(benefit["percentage_value"])
                    if benefit["percentage_value"] is not None
                    else None
                ),
            },
        }
        if existing_id is None:
            connection.execute(
                text(
                    """
                    INSERT INTO auditcore.discount_applications (
                        tenant_id, journey_id, discount_scheme_version_id,
                        discount_key, standard_eligible_amount,
                        actual_discount_amount, actual_source_kind,
                        source_evidence_id, details
                    ) VALUES (
                        :tenant_id, :journey_id, :version_id,
                        :discount_key, :standard_amount,
                        :actual_amount, :actual_source_kind,
                        :source_evidence_id, CAST(:details AS jsonb)
                    )
                    """
                ),
                {**params, "details": __import__("json").dumps(params["details"])},
            )
        else:
            connection.execute(
                text(
                    """
                    UPDATE auditcore.discount_applications
                    SET standard_eligible_amount = :standard_amount,
                        actual_discount_amount = :actual_amount,
                        actual_source_kind = :actual_source_kind,
                        source_evidence_id = :source_evidence_id,
                        details = CAST(:details AS jsonb),
                        updated_at_utc = now()
                    WHERE tenant_id = :tenant_id
                      AND discount_application_id = :application_id
                    """
                ),
                {
                    **params,
                    "application_id": existing_id,
                    "details": __import__("json").dumps(params["details"]),
                },
            )

    return _response(connection, tenant_id, journey_id)


@router.get("/journeys/{journey_id}/commercials", response_model=CommercialsResponse)
def get_commercials(
    tenant_id: str,
    journey_id: UUID,
    principal: Annotated[Principal, Depends(get_principal)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> CommercialsResponse:
    authorize(principal, tenant_id=tenant_id, permission="audit.journey.read")
    set_tenant_context(connection, tenant_id)
    journey = _journey_scope(connection, tenant_id, journey_id)
    require_business_scope(
        connection,
        principal,
        tenant_id=tenant_id,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )
    return _response(connection, tenant_id, journey_id)
