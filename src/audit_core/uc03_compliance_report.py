"""Deal Compliance Report -- a lightweight, on-demand, read-only view of one
journey's commercial line items alongside any open/resolved audit findings.

Deliberately NOT a reuse of the Journey 360 aggregation
(uc03_journey_overview_projection.py) -- that endpoint's own code comments
document needing a long client timeout allowance for its full read. This
module runs a handful of single-journey, indexed-by-(tenant_id, journey_id)
queries instead, matching the "add a dedicated, lightweight backend endpoint"
scoping decided for this feature: a TL/PM should be able to open a report for
one deal without paying for everything Journey 360 assembles.

The report is always generated live, never snapshotted: documents (bank
statements, RTO paperwork, insurance confirmations) keep arriving after
Delivery, so a frozen report would go stale. Every call here just reads
current state -- there is no persistence introduced by this module.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import Connection, text

from audit_core.authorization import AuthorizationError
from audit_core.db import set_tenant_context
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.errors import DependencyUnavailableError, NotFoundError
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    SecurityAuthorizationError,
    get_security_authorization_client,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}", tags=["compliance-report"])

# Same permission key uc03_review_queue.py uses, checked the same way: a live
# call to the Security service (SecurityAuthorizationClient.check_user_
# permission), not the static authorize()/Principal.permissions JWT-claim
# check findings.py happens to use for this same key. That static check has
# no proven, reachable frontend caller anywhere in this codebase (findings.py's
# own listFindings/createFinding are dead code, never called) -- there was no
# actual evidence a real user's JWT carries this permission in its static
# claims, and in practice it didn't: every real request 403'd. The live
# role-based check below is what Review Queue actually exercises daily.
_PERMISSION_KEY = "audit.finding.read"


def _authorize(
    client: SecurityAuthorizationClient,
    *,
    human_principal: HumanPrincipal,
    tenant_id: str,
) -> None:
    try:
        decision = client.check_user_permission(
            user_id=human_principal.subject,
            tenant_id=tenant_id,
            permission_key=_PERMISSION_KEY,
        )
    except SecurityAuthorizationError as exc:
        raise DependencyUnavailableError(
            detail="The compliance report is temporarily unavailable. Please try again."
        ) from exc
    if not decision.allowed:
        raise AuthorizationError(
            error_code="VAC-AUTH-002",
            status_code=403,
            title="Permission denied",
        )


def _require_business_scope(
    connection: Connection,
    *,
    tenant_id: str,
    actor_id: str,
    dealer_id: UUID,
    outlet_id: UUID | None,
) -> None:
    # require_business_scope (business_assignments.py) takes a Principal
    # (subject + tenant_id + permissions) -- HumanPrincipal only carries
    # subject, so it isn't a fit. Same query uc03_review_queue.py's own
    # _QUEUE_SQL embeds inline, extracted here for one journey instead of
    # filtering a whole result set.
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
                    OR (dealer_id = :dealer_id AND (outlet_id IS NULL OR outlet_id = :outlet_id))
              )
            LIMIT 1
            """
        ),
        {"tenant_id": tenant_id, "actor_id": actor_id, "dealer_id": dealer_id, "outlet_id": outlet_id},
    ).scalar_one_or_none()
    if assigned is None:
        raise AuthorizationError(
            error_code="VAC-AUTH-004",
            status_code=403,
            title="Business scope denied",
        )


# A finding raised more recently than this is flagged isNew=True so a TL/PM
# revisiting a report can tell what showed up since a previous look, without
# this module needing to persist per-viewer "last seen" state anywhere.
_NEW_FINDING_WINDOW = timedelta(days=3)

_OPEN_STATUSES = ("OPEN", "ACKNOWLEDGED")
_RESOLVED_STATUSES = ("RESOLVED", "VOIDED")
_HIGH_SEVERITIES = {"HIGH", "CRITICAL"}

# Every finding_type_code this codebase raises, bucketed into one of the
# report's commercial sections. No existing table/map does this end to end
# today (uc03_rule_engine_findings.py's _FINDING_TYPE_BY_CATEGORY only covers
# the 5 rule-engine anomaly types) -- this is deliberately its own, small,
# report-local map. A type with no entry here still counts in the summary
# totals and appears under "General" rather than silently disappearing.
_REPORT_CATEGORY_BY_FINDING_TYPE: dict[str, str] = {
    "PRICING_ANOMALY": "PRICING",
    "COMMERCIAL_EXCEPTION": "PRICING",
    "DISCOUNT_ANOMALY": "DISCOUNTS",
    "ACCESSORY_ANOMALY": "ACCESSORIES",
    "INSURANCE_ANOMALY": "INSURANCE",
    "RTO_ANOMALY": "REGISTRATION",
    "VEHICLE_IDENTITY_ANOMALY": "REGISTRATION",
    "VIN_RECONCILIATION_MISMATCH": "REGISTRATION",
    "FINANCE_HYPOTHECATION_MISSING": "FINANCE",
    "PAYMENT_EXCEPTION": "PAYMENTS",
    "PAYMENT_UNVERIFIED": "PAYMENTS",
    "DOCUMENT_EXCEPTION": "DOCUMENTS",
    "DOCUMENT_MISSING": "DOCUMENTS",
    "DELIVERY_DOCUMENT_MISSING": "DOCUMENTS",
    "DOCUMENT_UNRECOGNIZED": "DOCUMENTS",
    "REQUIRED_DOCUMENT_ANSWER_NO": "DOCUMENTS",
    "MODEL_NOT_IDENTIFIED": "DOCUMENTS",
    "MANUAL_VERIFICATION": "DOCUMENTS",
    "AUTOMATED_SYNC_FAILURE": "DOCUMENTS",
}

_SECTION_ORDER: list[tuple[str, str]] = [
    ("PRICING", "Pricing"),
    ("DISCOUNTS", "Discounts"),
    ("ACCESSORIES", "Accessories"),
    ("INSURANCE", "Insurance"),
    ("FINANCE", "Finance"),
    ("REGISTRATION", "Registration"),
    ("PAYMENTS", "Payments"),
    ("DOCUMENTS", "Documents"),
]


def _friendly(value: str | None) -> str | None:
    if not value:
        return value
    return value.replace("_", " ").title()


def _to_float(value: object) -> float | None:
    return None if value is None else float(value)


class ComplianceReportLineItem(BaseModel):
    label: str
    detail: str | None = None
    standardAmount: float | None = None
    actualAmount: float | None = None


class ComplianceReportFlag(BaseModel):
    findingId: UUID
    findingTypeCode: str | None
    title: str
    severity: str
    findingClass: str | None
    status: str
    createdAtUtc: datetime
    isNew: bool


class ComplianceReportResolvedFinding(BaseModel):
    findingId: UUID
    findingTypeCode: str | None
    title: str
    severity: str
    createdAtUtc: datetime
    resolvedAtUtc: datetime | None
    resolutionReason: str | None


class ComplianceReportSection(BaseModel):
    key: str
    label: str
    lineItems: list[ComplianceReportLineItem]
    flags: list[ComplianceReportFlag]


class ComplianceReportHeader(BaseModel):
    journeyId: UUID
    journeyReference: str | None
    bookingReference: str | None
    productLabel: str | None
    dealerName: str
    outletName: str
    customerDisplayName: str
    vin: str | None
    dealType: str | None
    financedBy: str | None
    bookingDate: date | None
    deliveryDate: date | None


class ComplianceReportSummary(BaseModel):
    totalFindings: int
    openFindings: int
    resolvedFindings: int
    highOrCriticalOpen: int


class ComplianceReportResponse(BaseModel):
    generatedAtUtc: datetime
    header: ComplianceReportHeader
    summary: ComplianceReportSummary
    sections: list[ComplianceReportSection]
    resolvedHistory: list[ComplianceReportResolvedFinding]


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


def _header(connection: Connection, tenant_id: str, journey_id: UUID) -> ComplianceReportHeader:
    row = connection.execute(
        text(
            """
            SELECT j.journey_id, j.journey_reference,
                   c.display_name AS customer_display_name,
                   d.dealer_name, o.outlet_name,
                   b.booking_reference, b.booking_date, b.deal_type_code,
                   NULLIF(concat_ws(' · ',
                       NULLIF(jp.model_name_snapshot, ''),
                       NULLIF(jp.variant_name_snapshot, ''),
                       NULLIF(jp.colour_name_snapshot, '')), '') AS product_label,
                   dl.actual_delivered_at, dl.planned_delivery_at,
                   vr.vin,
                   fr.provider_name AS financed_by
            FROM auditcore.journeys j
            JOIN auditcore.customers c
                ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
            JOIN auditcore.dealers d
                ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
            JOIN auditcore.dealer_outlets o
                ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
            LEFT JOIN auditcore.bookings b
                ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
            LEFT JOIN auditcore.deliveries dl
                ON dl.tenant_id = j.tenant_id AND dl.journey_id = j.journey_id
            LEFT JOIN auditcore.journey_products jp
                ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
            LEFT JOIN auditcore.vehicle_records vr
                ON vr.tenant_id = j.tenant_id AND vr.journey_id = j.journey_id
            LEFT JOIN auditcore.finance_records fr
                ON fr.tenant_id = j.tenant_id AND fr.journey_id = j.journey_id
            WHERE j.tenant_id = :tenant_id AND j.journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one()

    delivered_at = row["actual_delivered_at"] or row["planned_delivery_at"]
    return ComplianceReportHeader(
        journeyId=row["journey_id"],
        journeyReference=row["journey_reference"],
        bookingReference=row["booking_reference"],
        productLabel=row["product_label"],
        dealerName=row["dealer_name"],
        outletName=row["outlet_name"],
        customerDisplayName=row["customer_display_name"],
        vin=row["vin"],
        dealType=_friendly(row["deal_type_code"]),
        financedBy=row["financed_by"],
        bookingDate=row["booking_date"],
        deliveryDate=delivered_at.date() if isinstance(delivered_at, datetime) else delivered_at,
    )


def _pricing_items(connection: Connection, tenant_id: str, journey_id: UUID) -> list[ComplianceReportLineItem]:
    rows = connection.execute(
        text(
            """
            SELECT component_key, standard_amount, actual_amount, actual_source_kind
            FROM auditcore.commercial_lines
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY component_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        ComplianceReportLineItem(
            label=_friendly(row["component_key"]) or row["component_key"],
            detail=_friendly(row["actual_source_kind"]),
            standardAmount=_to_float(row["standard_amount"]),
            actualAmount=_to_float(row["actual_amount"]),
        )
        for row in rows
    ]


def _discount_items(connection: Connection, tenant_id: str, journey_id: UUID) -> list[ComplianceReportLineItem]:
    rows = connection.execute(
        text(
            """
            SELECT discount_key, standard_eligible_amount, actual_discount_amount, eligibility_result
            FROM auditcore.discount_applications
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY discount_key
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        ComplianceReportLineItem(
            label=_friendly(row["discount_key"]) or row["discount_key"],
            detail=_friendly(row["eligibility_result"]),
            standardAmount=_to_float(row["standard_eligible_amount"]),
            actualAmount=_to_float(row["actual_discount_amount"]),
        )
        for row in rows
    ]


def _accessory_items(connection: Connection, tenant_id: str, journey_id: UUID) -> list[ComplianceReportLineItem]:
    rows = connection.execute(
        text(
            """
            SELECT addon_type_code, provider_name, standard_amount, actual_amount, reference_number
            FROM auditcore.journey_addons
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY addon_type_code
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        ComplianceReportLineItem(
            label=_friendly(row["addon_type_code"]) or row["addon_type_code"],
            detail=row["provider_name"] or row["reference_number"],
            standardAmount=_to_float(row["standard_amount"]),
            actualAmount=_to_float(row["actual_amount"]),
        )
        for row in rows
    ]


def _insurance_items(connection: Connection, tenant_id: str, journey_id: UUID) -> list[ComplianceReportLineItem]:
    row = connection.execute(
        text(
            """
            SELECT insurer_name, policy_reference, cover_note_reference,
                   standard_premium_amount, actual_premium_amount, self_insurance_flag
            FROM auditcore.insurance_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return []
    if row["self_insurance_flag"]:
        detail = "Self-insured"
    else:
        detail = " · ".join(v for v in (row["insurer_name"], row["policy_reference"] or row["cover_note_reference"]) if v) or None
    return [
        ComplianceReportLineItem(
            label="Insurance Premium",
            detail=detail,
            standardAmount=_to_float(row["standard_premium_amount"]),
            actualAmount=_to_float(row["actual_premium_amount"]),
        )
    ]


def _finance_items(connection: Connection, tenant_id: str, journey_id: UUID) -> list[ComplianceReportLineItem]:
    row = connection.execute(
        text(
            """
            SELECT finance_type_code, provider_name, financed_amount
            FROM auditcore.finance_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        # No finance_records row is the correct signal for a presumed cash /
        # outright purchase -- no invoice ever declares "this is not financed".
        return []
    return [
        ComplianceReportLineItem(
            label="Financed Amount",
            detail=row["provider_name"] or _friendly(row["finance_type_code"]),
            standardAmount=None,
            actualAmount=_to_float(row["financed_amount"]),
        )
    ]


def _registration_items(connection: Connection, tenant_id: str, journey_id: UUID) -> list[ComplianceReportLineItem]:
    row = connection.execute(
        text(
            """
            SELECT registration_state, registration_territory, registration_district,
                   registration_type_code, registration_category_code, registration_number
            FROM auditcore.registration_records
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().one_or_none()
    if row is None:
        return []
    location = " / ".join(v for v in (row["registration_state"], row["registration_district"]) if v)
    return [
        ComplianceReportLineItem(
            label=row["registration_number"] or "Registration pending",
            detail=" · ".join(v for v in (location, _friendly(row["registration_type_code"])) if v) or None,
            standardAmount=None,
            actualAmount=None,
        )
    ]


def _payment_items(connection: Connection, tenant_id: str, journey_id: UUID) -> list[ComplianceReportLineItem]:
    rows = connection.execute(
        text(
            """
            SELECT payment_at_utc, amount, payment_method_code, payment_reference, actual_status_code
            FROM auditcore.payments
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id
            ORDER BY payment_at_utc
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()
    return [
        ComplianceReportLineItem(
            label=f"{_friendly(row['payment_method_code']) or 'Payment'} · {row['payment_at_utc'].date().isoformat()}"
            if row["payment_at_utc"] else (_friendly(row["payment_method_code"]) or "Payment"),
            detail=row["payment_reference"] or _friendly(row["actual_status_code"]),
            standardAmount=None,
            actualAmount=_to_float(row["amount"]),
        )
        for row in rows
    ]


_ITEM_LOADERS = {
    "PRICING": _pricing_items,
    "DISCOUNTS": _discount_items,
    "ACCESSORIES": _accessory_items,
    "INSURANCE": _insurance_items,
    "FINANCE": _finance_items,
    "REGISTRATION": _registration_items,
    "PAYMENTS": _payment_items,
}


def _findings(connection: Connection, tenant_id: str, journey_id: UUID):
    return connection.execute(
        text(
            """
            SELECT audit_finding_id, finding_type_code, severity, finding_status,
                   finding_class, title, resolution_reason, resolved_at_utc, created_at_utc
            FROM auditcore.audit_findings
            WHERE tenant_id = :tenant_id AND journey_id = :journey_id AND subject_kind = 'JOURNEY'
            ORDER BY created_at_utc
            """
        ),
        {"tenant_id": tenant_id, "journey_id": journey_id},
    ).mappings().all()


@router.get("/journeys/{journey_id}/compliance-report", response_model=ComplianceReportResponse)
def get_compliance_report(
    tenant_id: str,
    journey_id: UUID,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[SecurityAuthorizationClient, Depends(get_security_authorization_client)],
    connection: Annotated[Connection, Depends(get_connection)],
) -> ComplianceReportResponse:
    # Same finding data a PC can already reach through the Review Queue;
    # restricting the *action* of generating a Compliance Report to TL/PM is
    # enforced at the frontend menu today, not here. Revisit if that needs to
    # become a hard backend rule.
    _authorize(authorization_client, human_principal=human_principal, tenant_id=tenant_id)
    set_tenant_context(connection, tenant_id)
    journey = _journey_scope(connection, tenant_id, journey_id)
    _require_business_scope(
        connection,
        tenant_id=tenant_id,
        actor_id=human_principal.subject,
        dealer_id=journey["dealer_id"],
        outlet_id=journey["outlet_id"],
    )

    header = _header(connection, tenant_id, journey_id)
    findings = _findings(connection, tenant_id, journey_id)

    now = datetime.now(UTC)
    open_by_category: dict[str, list[ComplianceReportFlag]] = {}
    resolved_history: list[ComplianceReportResolvedFinding] = []
    open_count = 0
    resolved_count = 0
    high_or_critical_open = 0

    for row in findings:
        category = _REPORT_CATEGORY_BY_FINDING_TYPE.get(row["finding_type_code"] or "", "GENERAL")
        status = row["finding_status"]
        if status in _OPEN_STATUSES:
            open_count += 1
            severity = (row["severity"] or "MEDIUM").upper()
            if severity in _HIGH_SEVERITIES:
                high_or_critical_open += 1
            created_at = row["created_at_utc"]
            is_new = bool(created_at) and (now - created_at) < _NEW_FINDING_WINDOW
            open_by_category.setdefault(category, []).append(
                ComplianceReportFlag(
                    findingId=row["audit_finding_id"],
                    findingTypeCode=row["finding_type_code"],
                    title=row["title"],
                    severity=severity,
                    findingClass=row["finding_class"],
                    status=status,
                    createdAtUtc=created_at,
                    isNew=is_new,
                )
            )
        elif status in _RESOLVED_STATUSES:
            resolved_count += 1
            resolved_history.append(
                ComplianceReportResolvedFinding(
                    findingId=row["audit_finding_id"],
                    findingTypeCode=row["finding_type_code"],
                    title=row["title"],
                    severity=(row["severity"] or "MEDIUM").upper(),
                    createdAtUtc=row["created_at_utc"],
                    resolvedAtUtc=row["resolved_at_utc"],
                    resolutionReason=row["resolution_reason"],
                )
            )

    sections: list[ComplianceReportSection] = []
    for key, label in _SECTION_ORDER:
        loader = _ITEM_LOADERS.get(key)
        line_items = loader(connection, tenant_id, journey_id) if loader else []
        section_flags = open_by_category.get(key, [])
        if not line_items and not section_flags:
            continue
        sections.append(ComplianceReportSection(key=key, label=label, lineItems=line_items, flags=section_flags))

    general_flags = open_by_category.get("GENERAL", [])
    if general_flags:
        sections.append(ComplianceReportSection(key="GENERAL", label="General", lineItems=[], flags=general_flags))

    resolved_history.sort(key=lambda item: item.resolvedAtUtc or item.createdAtUtc, reverse=True)

    return ComplianceReportResponse(
        generatedAtUtc=now,
        header=header,
        summary=ComplianceReportSummary(
            totalFindings=len(findings),
            openFindings=open_count,
            resolvedFindings=resolved_count,
            highOrCriticalOpen=high_or_critical_open,
        ),
        sections=sections,
        resolvedHistory=resolved_history,
    )
