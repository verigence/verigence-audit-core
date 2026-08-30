from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError, NotFoundError, ValidationError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/uc03", tags=["uc03-journey-search"])

_JOURNEY_READ_PERMISSION = "audit.journey.read"
_FULL_CONTACT_PERMISSION = "audit.customer.contact.full.read"
_MASK_PREFIX = "******"


class JourneySearchItem(BaseModel):
    journeyId: UUID
    customerDisplayName: str
    customerLegalName: str | None = None
    customerMobileLast4: str | None = None
    bookingReference: str | None = None
    productLabel: str | None = None
    dealerId: UUID
    dealerName: str
    outletId: UUID
    outletName: str
    bookingStatus: str | None = None
    deliveryStatus: str | None = None
    vin: str | None = None
    registrationNumber: str | None = None
    invoiceReference: str | None = None
    matchedOn: str
    matchedValue: str | None = None
    latestActivityAtUtc: datetime


class JourneySearchResponse(BaseModel):
    query: str
    items: list[JourneySearchItem]
    resultCount: int


class JourneyOverviewResponse(BaseModel):
    journey: dict[str, Any]
    customer: dict[str, Any]
    booking: dict[str, Any] | None = None
    commercialLines: list[dict[str, Any]] = Field(default_factory=list)
    discounts: list[dict[str, Any]] = Field(default_factory=list)
    payments: list[dict[str, Any]] = Field(default_factory=list)
    finance: dict[str, Any] | None = None
    insurance: dict[str, Any] | None = None
    addons: list[dict[str, Any]] = Field(default_factory=list)
    tradeIn: dict[str, Any] | None = None
    vehicle: dict[str, Any] | None = None
    registration: dict[str, Any] | None = None
    delivery: dict[str, Any] | None = None
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)


def _authorize_read(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=_JOURNEY_READ_PERMISSION,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="Journey search is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _can_read_full_contact(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
) -> bool:
    try:
        return client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=_FULL_CONTACT_PERMISSION,
        ).allowed
    except SecurityAuthorizationError:
        return False


def _visible_mobile(
    mobile_number: str | None,
    mobile_last4: str | None,
    *,
    full_contact: bool,
) -> str | None:
    if mobile_number is None:
        return None
    if full_contact:
        return mobile_number
    last4 = mobile_last4 or "".join(character for character in mobile_number if character.isdigit())[-4:]
    return f"{_MASK_PREFIX}{last4}" if last4 else None


def _as_dict(row: Any) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _as_dicts(rows: Any) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def _search_query_parts(query: str) -> dict[str, Any]:
    candidate = query.strip()
    if len(candidate) < 3:
        raise ValidationError(detail="Enter at least 3 characters to search Journeys.")
    if len(candidate) > 160:
        raise ValidationError(detail="Journey search text cannot exceed 160 characters.")

    upper = candidate.upper()
    lower = candidate.lower()
    digits = "".join(character for character in candidate if character.isdigit())
    mobile_candidate = bool(
        len(digits) >= 6
        and all(character.isdigit() or character in "+- ()." for character in candidate)
    )
    try:
        technical_id: UUID | None = UUID(candidate)
    except ValueError:
        technical_id = None

    return {
        "query": candidate,
        "q_upper": upper,
        "q_lower": lower,
        "name_contains": f"%{lower}%",
        "ref_prefix": f"{upper}%",
        "ref_contains": f"%{upper}%",
        "q_digits": digits,
        "mobile_suffix": f"%{digits}",
        "mobile_candidate": mobile_candidate,
        "technical_id": technical_id,
    }


@router.get("/journey-search", response_model=JourneySearchResponse)
def search_journeys(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    query: Annotated[str, Query(alias="q", min_length=3, max_length=160)],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> JourneySearchResponse:
    """Search only Journeys inside the caller's active Dealer/Outlet assignments."""

    _authorize_read(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    search = _search_query_parts(query)

    rows = connection.execute(
        text(
            """
            WITH scoped AS (
                SELECT
                    j.journey_id,
                    j.booking_id,
                    j.delivery_id,
                    j.dealer_id,
                    j.outlet_id,
                    c.display_name AS customer_display_name,
                    c.legal_name AS customer_legal_name,
                    c.mobile_number AS customer_mobile_number,
                    c.mobile_last4 AS customer_mobile_last4,
                    d.dealer_name,
                    o.outlet_name,
                    b.booking_reference,
                    COALESCE(bs.business_status, b.actual_status_code) AS booking_status,
                    COALESCE(ds.business_status, dl.actual_delivery_status_code) AS delivery_status,
                    vr.vin,
                    vr.chassis_number,
                    vr.dms_reference,
                    vr.invoice_reference,
                    rr.registration_number,
                    pm.payment_reference AS matched_payment_reference,
                    NULLIF(
                        concat_ws(
                            ' · ',
                            NULLIF(jp.model_name_snapshot, ''),
                            NULLIF(jp.variant_name_snapshot, ''),
                            NULLIF(jp.colour_name_snapshot, '')
                        ),
                        ''
                    ) AS product_label,
                    GREATEST(
                        j.updated_at_utc,
                        COALESCE(b.updated_at_utc, j.updated_at_utc),
                        COALESCE(dl.updated_at_utc, j.updated_at_utc),
                        COALESCE(vr.updated_at_utc, j.updated_at_utc),
                        COALESCE(rr.updated_at_utc, j.updated_at_utc),
                        COALESCE(bs.latest_activity_at_utc, j.updated_at_utc),
                        COALESCE(ds.latest_activity_at_utc, j.updated_at_utc)
                    ) AS latest_activity_at_utc,
                    regexp_replace(COALESCE(c.mobile_number, ''), '[^0-9]', '', 'g')
                        AS customer_mobile_digits
                FROM auditcore.journeys j
                JOIN auditcore.customers c
                  ON c.tenant_id = j.tenant_id
                 AND c.customer_id = j.customer_id
                JOIN auditcore.dealers d
                  ON d.tenant_id = j.tenant_id
                 AND d.dealer_id = j.dealer_id
                JOIN auditcore.dealer_outlets o
                  ON o.tenant_id = j.tenant_id
                 AND o.dealer_id = j.dealer_id
                 AND o.outlet_id = j.outlet_id
                LEFT JOIN auditcore.bookings b
                  ON b.tenant_id = j.tenant_id
                 AND b.journey_id = j.journey_id
                LEFT JOIN auditcore.deliveries dl
                  ON dl.tenant_id = j.tenant_id
                 AND dl.journey_id = j.journey_id
                LEFT JOIN auditcore.vehicle_records vr
                  ON vr.tenant_id = j.tenant_id
                 AND vr.journey_id = j.journey_id
                LEFT JOIN auditcore.registration_records rr
                  ON rr.tenant_id = j.tenant_id
                 AND rr.journey_id = j.journey_id
                LEFT JOIN auditcore.journey_products jp
                  ON jp.tenant_id = j.tenant_id
                 AND jp.journey_id = j.journey_id
                LEFT JOIN auditcore.journey_stage_states bs
                  ON bs.tenant_id = j.tenant_id
                 AND bs.journey_id = j.journey_id
                 AND bs.stage_code = 'BOOKING'
                LEFT JOIN auditcore.journey_stage_states ds
                  ON ds.tenant_id = j.tenant_id
                 AND ds.journey_id = j.journey_id
                 AND ds.stage_code = 'DELIVERY'
                LEFT JOIN LATERAL (
                    SELECT p.payment_reference
                    FROM auditcore.payments p
                    WHERE p.tenant_id = j.tenant_id
                      AND p.journey_id = j.journey_id
                      AND p.payment_reference IS NOT NULL
                      AND upper(p.payment_reference) LIKE :ref_prefix
                    ORDER BY
                        CASE WHEN upper(p.payment_reference) = :q_upper THEN 0 ELSE 1 END,
                        p.created_at_utc DESC,
                        p.payment_id DESC
                    LIMIT 1
                ) pm ON true
                WHERE j.tenant_id = :tenant_id
                  AND EXISTS (
                        SELECT 1
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
                  )
            ),
            matched AS (
                SELECT
                    scoped.*,
                    CASE
                        WHEN upper(COALESCE(booking_reference, '')) = :q_upper THEN 10
                        WHEN :mobile_candidate
                             AND customer_mobile_digits = :q_digits THEN 20
                        WHEN upper(COALESCE(vin, '')) = :q_upper THEN 30
                        WHEN upper(COALESCE(chassis_number, '')) = :q_upper THEN 31
                        WHEN upper(COALESCE(registration_number, '')) = :q_upper THEN 32
                        WHEN upper(COALESCE(invoice_reference, '')) = :q_upper THEN 33
                        WHEN upper(COALESCE(dms_reference, '')) = :q_upper THEN 34
                        WHEN upper(COALESCE(matched_payment_reference, '')) = :q_upper THEN 35
                        WHEN lower(customer_display_name) = :q_lower THEN 40
                        WHEN lower(COALESCE(customer_legal_name, '')) = :q_lower THEN 41
                        WHEN upper(COALESCE(booking_reference, '')) LIKE :ref_prefix THEN 50
                        WHEN upper(COALESCE(vin, '')) LIKE :ref_prefix THEN 51
                        WHEN upper(COALESCE(chassis_number, '')) LIKE :ref_prefix THEN 52
                        WHEN upper(COALESCE(registration_number, '')) LIKE :ref_prefix THEN 53
                        WHEN upper(COALESCE(invoice_reference, '')) LIKE :ref_prefix THEN 54
                        WHEN upper(COALESCE(dms_reference, '')) LIKE :ref_prefix THEN 55
                        WHEN matched_payment_reference IS NOT NULL THEN 56
                        WHEN lower(customer_display_name) LIKE :name_contains THEN 60
                        WHEN lower(COALESCE(customer_legal_name, '')) LIKE :name_contains THEN 61
                        WHEN length(:q_upper) >= 6
                             AND upper(COALESCE(vin, '')) LIKE :ref_contains THEN 70
                        WHEN length(:q_upper) >= 6
                             AND upper(COALESCE(chassis_number, '')) LIKE :ref_contains THEN 71
                        WHEN CAST(:technical_id AS uuid) IS NOT NULL
                             AND journey_id = CAST(:technical_id AS uuid) THEN 90
                        WHEN CAST(:technical_id AS uuid) IS NOT NULL
                             AND booking_id = CAST(:technical_id AS uuid) THEN 91
                        WHEN CAST(:technical_id AS uuid) IS NOT NULL
                             AND delivery_id = CAST(:technical_id AS uuid) THEN 92
                        ELSE 999
                    END AS match_rank
                FROM scoped
                WHERE
                    lower(customer_display_name) LIKE :name_contains
                    OR lower(COALESCE(customer_legal_name, '')) LIKE :name_contains
                    OR upper(COALESCE(booking_reference, '')) LIKE :ref_prefix
                    OR upper(COALESCE(vin, '')) LIKE :ref_prefix
                    OR upper(COALESCE(chassis_number, '')) LIKE :ref_prefix
                    OR upper(COALESCE(registration_number, '')) LIKE :ref_prefix
                    OR upper(COALESCE(invoice_reference, '')) LIKE :ref_prefix
                    OR upper(COALESCE(dms_reference, '')) LIKE :ref_prefix
                    OR matched_payment_reference IS NOT NULL
                    OR (
                        :mobile_candidate
                        AND customer_mobile_digits LIKE :mobile_suffix
                    )
                    OR (
                        length(:q_upper) >= 6
                        AND upper(COALESCE(vin, '')) LIKE :ref_contains
                    )
                    OR (
                        length(:q_upper) >= 6
                        AND upper(COALESCE(chassis_number, '')) LIKE :ref_contains
                    )
                    OR (
                        CAST(:technical_id AS uuid) IS NOT NULL
                        AND (
                            journey_id = CAST(:technical_id AS uuid)
                            OR booking_id = CAST(:technical_id AS uuid)
                            OR delivery_id = CAST(:technical_id AS uuid)
                        )
                    )
            )
            SELECT *,
                CASE
                    WHEN match_rank IN (10, 50) THEN 'DEALER_BOOKING_NUMBER'
                    WHEN match_rank = 20 THEN 'MOBILE_NUMBER'
                    WHEN match_rank IN (30, 51, 70) THEN 'VIN'
                    WHEN match_rank IN (31, 52, 71) THEN 'CHASSIS_NUMBER'
                    WHEN match_rank IN (32, 53) THEN 'REGISTRATION_NUMBER'
                    WHEN match_rank IN (33, 54) THEN 'INVOICE_REFERENCE'
                    WHEN match_rank IN (34, 55) THEN 'DMS_REFERENCE'
                    WHEN match_rank IN (35, 56) THEN 'PAYMENT_REFERENCE'
                    WHEN match_rank IN (40, 60) THEN 'CUSTOMER_ENTERED_NAME'
                    WHEN match_rank IN (41, 61) THEN 'CUSTOMER_LEGAL_NAME'
                    ELSE 'TECHNICAL_ID'
                END AS matched_on,
                CASE
                    WHEN match_rank IN (10, 50) THEN booking_reference
                    WHEN match_rank = 20 THEN
                        CASE
                            WHEN customer_mobile_last4 IS NULL THEN NULL
                            ELSE :mask_prefix || customer_mobile_last4
                        END
                    WHEN match_rank IN (30, 51, 70) THEN vin
                    WHEN match_rank IN (31, 52, 71) THEN chassis_number
                    WHEN match_rank IN (32, 53) THEN registration_number
                    WHEN match_rank IN (33, 54) THEN invoice_reference
                    WHEN match_rank IN (34, 55) THEN dms_reference
                    WHEN match_rank IN (35, 56) THEN matched_payment_reference
                    WHEN match_rank IN (40, 60) THEN customer_display_name
                    WHEN match_rank IN (41, 61) THEN customer_legal_name
                    ELSE journey_id::text
                END AS matched_value
            FROM matched
            WHERE match_rank < 999
            ORDER BY match_rank, latest_activity_at_utc DESC, journey_id DESC
            LIMIT :limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "actor_id": human_principal.subject,
            "q_upper": search["q_upper"],
            "q_lower": search["q_lower"],
            "q_digits": search["q_digits"],
            "name_contains": search["name_contains"],
            "ref_prefix": search["ref_prefix"],
            "ref_contains": search["ref_contains"],
            "mobile_suffix": search["mobile_suffix"],
            "mobile_candidate": search["mobile_candidate"],
            "technical_id": search["technical_id"],
            "mask_prefix": _MASK_PREFIX,
            "limit": limit,
        },
    ).mappings().all()

    items = [
        JourneySearchItem(
            journeyId=row["journey_id"],
            customerDisplayName=row["customer_display_name"],
            customerLegalName=row["customer_legal_name"],
            customerMobileLast4=row["customer_mobile_last4"],
            bookingReference=row["booking_reference"],
            productLabel=row["product_label"],
            dealerId=row["dealer_id"],
            dealerName=row["dealer_name"],
            outletId=row["outlet_id"],
            outletName=row["outlet_name"],
            bookingStatus=row["booking_status"],
            deliveryStatus=row["delivery_status"],
            vin=row["vin"],
            registrationNumber=row["registration_number"],
            invoiceReference=row["invoice_reference"],
            matchedOn=row["matched_on"],
            matchedValue=row["matched_value"],
            latestActivityAtUtc=row["latest_activity_at_utc"],
        )
        for row in rows
    ]
    return JourneySearchResponse(query=search["query"], items=items, resultCount=len(items))


def _scoped_overview_header(
    connection: Connection,
    *,
    tenant_id: str,
    journey_id: UUID,
    actor_id: str,
):
    return connection.execute(
        text(
            """
            SELECT
                j.journey_id AS "journeyId",
                j.customer_id AS "customerId",
                j.booking_id AS "bookingId",
                j.delivery_id AS "deliveryId",
                j.dealer_id AS "dealerId",
                d.dealer_name AS "dealerName",
                j.outlet_id AS "outletId",
                o.outlet_name AS "outletName",
                p.project_name AS "projectName",
                p.timezone_name AS "timezoneName",
                NULLIF(
                    concat_ws(
                        ' · ',
                        NULLIF(jp.model_name_snapshot, ''),
                        NULLIF(jp.variant_name_snapshot, ''),
                        NULLIF(jp.colour_name_snapshot, '')
                    ),
                    ''
                ) AS "productLabel",
                COALESCE(bs.business_status, b.actual_status_code) AS "bookingStatus",
                COALESCE(bs.audit_state, 'NOT_STARTED') AS "bookingAuditState",
                COALESCE(bs.audit_status, 'NOT_EVALUATED') AS "bookingAuditStatus",
                COALESCE(ds.business_status, dl.actual_delivery_status_code) AS "deliveryStatus",
                COALESCE(ds.audit_state, 'NOT_STARTED') AS "deliveryAuditState",
                COALESCE(ds.audit_status, 'NOT_EVALUATED') AS "deliveryAuditStatus",
                j.audit_state AS "journeyAuditState",
                j.audit_outcome AS "journeyAuditOutcome",
                j.created_at_utc AS "createdAtUtc",
                j.updated_at_utc AS "updatedAtUtc"
            FROM auditcore.journeys j
            JOIN auditcore.projects p
              ON p.tenant_id = j.tenant_id
             AND p.project_status = 'ACTIVE'
            JOIN auditcore.dealers d
              ON d.tenant_id = j.tenant_id
             AND d.dealer_id = j.dealer_id
            JOIN auditcore.dealer_outlets o
              ON o.tenant_id = j.tenant_id
             AND o.dealer_id = j.dealer_id
             AND o.outlet_id = j.outlet_id
            LEFT JOIN auditcore.bookings b
              ON b.tenant_id = j.tenant_id
             AND b.journey_id = j.journey_id
            LEFT JOIN auditcore.deliveries dl
              ON dl.tenant_id = j.tenant_id
             AND dl.journey_id = j.journey_id
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id = j.tenant_id
             AND jp.journey_id = j.journey_id
            LEFT JOIN auditcore.journey_stage_states bs
              ON bs.tenant_id = j.tenant_id
             AND bs.journey_id = j.journey_id
             AND bs.stage_code = 'BOOKING'
            LEFT JOIN auditcore.journey_stage_states ds
              ON ds.tenant_id = j.tenant_id
             AND ds.journey_id = j.journey_id
             AND ds.stage_code = 'DELIVERY'
            WHERE j.tenant_id = :tenant_id
              AND j.journey_id = :journey_id
              AND EXISTS (
                    SELECT 1
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
              )
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id, "actor_id": actor_id},
    ).mappings().one_or_none()


@router.get("/journeys/{journey_id}/overview", response_model=JourneyOverviewResponse)
def get_journey_overview(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient,
        Depends(get_security_authorization_client),
    ],
    connection: Annotated[Connection, Depends(get_connection)],
) -> JourneyOverviewResponse:
    """Return a read-only Journey 360 projection using only Audit Core-owned data."""

    _authorize_read(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    set_tenant_context(connection, tenant_id)
    header = _scoped_overview_header(
        connection,
        tenant_id=tenant_id,
        journey_id=journey_id,
        actor_id=human_principal.subject,
    )
    if header is None:
        # Do not disclose whether a Journey exists outside the caller's business scope.
        raise NotFoundError(
            error_code="VAC-NF-005",
            title="Journey not found",
            detail="Journey not found in your current Project scope.",
        )

    full_contact = _can_read_full_contact(
        authorization_client,
        human_principal=human_principal,
        tenant_id=tenant_id,
    )
    customer = connection.execute(
        text(
            """
            SELECT
                c.customer_id AS "customerId",
                c.display_name AS "enteredName",
                c.legal_name AS "legalName",
                c.legal_name_status AS "legalNameStatus",
                c.mobile_number AS "mobileNumber",
                c.mobile_last4 AS "mobileLast4",
                c.email_reference AS "emailReference",
                c.external_customer_ref AS "externalCustomerReference",
                c.customer_type_code AS "customerType",
                c.relationship_type AS "relationshipType",
                c.relationship_name AS "relationshipName",
                c.status AS "status"
            FROM auditcore.customers c
            WHERE c.tenant_id = :tenant_id
              AND c.customer_id = :customer_id
            """
        ),
        {"tenant_id": tenant_id, "customer_id": header["customerId"]},
    ).mappings().one()
    customer_data = dict(customer)
    customer_data["mobileNumber"] = _visible_mobile(
        customer["mobileNumber"],
        customer["mobileLast4"],
        full_contact=full_contact,
    )

    booking = connection.execute(
        text(
            """
            SELECT
                b.booking_id AS "bookingId",
                b.booking_reference AS "bookingReference",
                b.booking_date AS "bookingDate",
                b.sales_staff_id AS "salesStaffId",
                b.price_list_id AS "priceListId",
                b.deal_type_code AS "dealType",
                b.deal_source_code AS "dealSource",
                b.lead_source_code AS "leadSource",
                b.outright_purchase AS "outrightPurchase",
                b.corporate_id_available AS "corporateIdAvailable",
                b.gst_benefit AS "gstBenefit",
                b.expected_delivery_text AS "expectedDeliveryText",
                b.expected_delivery_date AS "expectedDeliveryDate",
                b.actual_status_code AS "actualStatusCode",
                jp.product_sku_id AS "productSkuId",
                jp.model_code_snapshot AS "modelCode",
                jp.model_name_snapshot AS "modelName",
                jp.variant_code_snapshot AS "variantCode",
                jp.variant_name_snapshot AS "variantName",
                jp.colour_code_snapshot AS "colourCode",
                jp.colour_name_snapshot AS "colourName",
                jp.selection_source AS "selectionSource"
            FROM auditcore.bookings b
            LEFT JOIN auditcore.journey_products jp
              ON jp.tenant_id = b.tenant_id
             AND jp.journey_id = b.journey_id
            WHERE b.tenant_id = :tenant_id
              AND b.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()

    commercial_lines = connection.execute(
        text(
            """
            SELECT
                commercial_line_id AS "commercialLineId",
                component_key AS "componentKey",
                standard_amount AS "standardAmount",
                actual_amount AS "actualAmount",
                currency_code AS "currencyCode",
                actual_source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId",
                source_reference AS "sourceReference"
            FROM auditcore.commercial_lines
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY component_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    discounts = connection.execute(
        text(
            """
            SELECT
                discount_application_id AS "discountApplicationId",
                discount_key AS "discountKey",
                standard_eligible_amount AS "standardEligibleAmount",
                actual_discount_amount AS "actualDiscountAmount",
                actual_source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId"
            FROM auditcore.discount_applications
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY discount_key, discount_application_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    payments = connection.execute(
        text(
            """
            SELECT
                payment_id AS "paymentId",
                booking_id AS "bookingId",
                delivery_id AS "deliveryId",
                payment_stage AS "paymentStage",
                payment_at_utc AS "paymentAtUtc",
                amount AS "amount",
                currency_code AS "currencyCode",
                payment_method_code AS "paymentMethodCode",
                payment_reference AS "paymentReference",
                actual_status_code AS "actualStatusCode",
                status_source AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId"
            FROM auditcore.payments
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY payment_at_utc NULLS LAST, created_at_utc, payment_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    finance = connection.execute(
        text(
            """
            SELECT
                finance_record_id AS "financeRecordId",
                finance_type_code AS "financeTypeCode",
                provider_name AS "providerName",
                do_reference AS "doReference",
                po_reference AS "poReference",
                financed_amount AS "financedAmount",
                actual_status_code AS "actualStatusCode",
                source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId",
                details AS "details"
            FROM auditcore.finance_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    insurance = connection.execute(
        text(
            """
            SELECT
                insurance_record_id AS "insuranceRecordId",
                insurer_name AS "insurerName",
                policy_reference AS "policyReference",
                cover_note_reference AS "coverNoteReference",
                standard_premium_amount AS "standardPremiumAmount",
                actual_premium_amount AS "actualPremiumAmount",
                self_insurance_flag AS "selfInsuranceFlag",
                insurance_by AS "insuranceBy",
                actual_status_code AS "actualStatusCode",
                source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId"
            FROM auditcore.insurance_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    addons = connection.execute(
        text(
            """
            SELECT
                journey_addon_id AS "journeyAddonId",
                addon_type_code AS "addonTypeCode",
                provider_name AS "providerName",
                standard_amount AS "standardAmount",
                actual_amount AS "actualAmount",
                reference_number AS "referenceNumber",
                source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId",
                details AS "details"
            FROM auditcore.journey_addons
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY created_at_utc, journey_addon_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    trade_in = connection.execute(
        text(
            """
            SELECT
                trade_in_case_id AS "tradeInCaseId",
                actual_status_code AS "actualStatusCode",
                old_vehicle_registration AS "oldVehicleRegistration",
                old_vehicle_make_model AS "oldVehicleMakeModel",
                quoted_value AS "quotedValue",
                actual_value AS "actualValue",
                handover_at_utc AS "handoverAtUtc",
                payment_at_utc AS "paymentAtUtc",
                resale_at_utc AS "resaleAtUtc",
                source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId",
                details AS "details"
            FROM auditcore.trade_in_cases
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    vehicle = connection.execute(
        text(
            """
            SELECT
                vehicle_record_id AS "vehicleRecordId",
                vin AS "vin",
                chassis_number AS "chassisNumber",
                dms_reference AS "dmsReference",
                invoice_reference AS "invoiceReference",
                allocated_at_utc AS "allocatedAtUtc",
                source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId"
            FROM auditcore.vehicle_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    registration = connection.execute(
        text(
            """
            SELECT
                registration_record_id AS "registrationRecordId",
                registration_state AS "registrationState",
                registration_territory AS "registrationTerritory",
                registration_district AS "registrationDistrict",
                registration_type_code AS "registrationTypeCode",
                registration_category_code AS "registrationCategoryCode",
                registration_number AS "registrationNumber",
                registration_by AS "registrationBy",
                actual_status_code AS "actualStatusCode",
                source_kind AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId"
            FROM auditcore.registration_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    delivery = connection.execute(
        text(
            """
            SELECT
                delivery_id AS "deliveryId",
                booking_id AS "bookingId",
                planned_delivery_at AS "plannedDeliveryAt",
                delivery_intimated_at AS "deliveryIntimatedAt",
                actual_delivery_status_code AS "actualDeliveryStatusCode",
                actual_delivered_at AS "actualDeliveredAt",
                status_source AS "sourceKind",
                source_evidence_id AS "sourceEvidenceId"
            FROM auditcore.deliveries
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    evidence = connection.execute(
        text(
            """
            SELECT
                evidence_id AS "evidenceId",
                document_type_key AS "documentTypeKey",
                evidence_purpose AS "evidencePurpose",
                process_area AS "processArea",
                processing_status_cache AS "processingStatus",
                verification_status_cache AS "verificationStatus",
                confirmation_status_cache AS "confirmationStatus",
                linked_at_utc AS "linkedAtUtc"
            FROM auditcore.evidence
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND association_status = 'ACTIVE'
            ORDER BY linked_at_utc, evidence_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    findings = connection.execute(
        text(
            """
            SELECT
                audit_finding_id AS "auditFindingId",
                stage_code AS "stageCode",
                finding_type_code AS "findingTypeCode",
                severity AS "severity",
                finding_status AS "findingStatus",
                title AS "title",
                description AS "description",
                expected_summary AS "expectedSummary",
                observed_summary AS "observedSummary",
                resolution_reason AS "resolutionReason",
                blocking_completion AS "blockingCompletion",
                created_at_utc AS "createdAtUtc",
                updated_at_utc AS "updatedAtUtc"
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id
              AND journey_id = :journey_id
              AND finding_status <> 'VOIDED'
            ORDER BY
                CASE severity
                    WHEN 'CRITICAL' THEN 5
                    WHEN 'HIGH' THEN 4
                    WHEN 'MEDIUM' THEN 3
                    WHEN 'LOW' THEN 2
                    WHEN 'INFO' THEN 1
                    ELSE 0
                END DESC,
                updated_at_utc DESC,
                audit_finding_id DESC
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()

    return JourneyOverviewResponse(
        journey=dict(header),
        customer=customer_data,
        booking=_as_dict(booking),
        commercialLines=_as_dicts(commercial_lines),
        discounts=_as_dicts(discounts),
        payments=_as_dicts(payments),
        finance=_as_dict(finance),
        insurance=_as_dict(insurance),
        addons=_as_dicts(addons),
        tradeIn=_as_dict(trade_in),
        vehicle=_as_dict(vehicle),
        registration=_as_dict(registration),
        delivery=_as_dict(delivery),
        evidence=_as_dicts(evidence),
        findings=_as_dicts(findings),
    )
