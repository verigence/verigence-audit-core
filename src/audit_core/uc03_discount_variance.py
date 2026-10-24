"""uc03_discount_variance.py -- where a discount on the invoice is not what the
Journey was entitled to.

Asked by the product owner on 2026-10-24: a tab that highlights any variance in
discounts, tallied from the INVOICE only -- never the booking form -- with the
discount category, PC, outlet, customer and car model beside each.

``discount_variance_rows`` reads the Deal as the Deal tab builds it, so the
figures here are the Deal's own and cannot drift from it; ``refresh_price_variance``
stores the result on the Journey's runtime row beside the price variance (same
refresh, same staleness), and the report below reads that, in one query.

Only a Journey whose vehicle invoice was read is tallied: with no invoice, or an
invoice that gave no price, there is nothing to tally, and the figure the Deal
shows for those is the booking's, which this tab must not use.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-discount-variance"])

_READ_PERMISSION = "audit.journey.read"
TOLERANCE = Decimal(100)
OVER, UNDER, UNKNOWN = "OVER_ENTITLEMENT", "UNDER_ENTITLEMENT", "ENTITLEMENT_UNKNOWN"


def _dec(value: Any) -> Decimal | None:
    try:
        return None if value in (None, "") else Decimal(str(value))
    except InvalidOperation:
        return None


def _vehicle_price_read(view: dict[str, Any]) -> bool:
    for group in view.get("categories") or []:
        for row in group.get("components") or []:
            if row.get("key") == "ex_showroom_price":
                return _dec(row.get("current")) is not None
    return False


def discount_variance_rows(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The discounts of this Deal whose invoice figure is not the entitlement.

    ``billed`` is the invoice's own figure (GST-inclusive, as the price list
    quotes it); a booking figure is never used. A discount the invoice does not
    carry is Rs 0. An entitlement that is not known is reported as such, never
    as zero."""
    summary = view.get("summary") or {}
    if not summary.get("vehicleInvoiceOnFile") or not _vehicle_price_read(view):
        return []
    rows: list[dict[str, Any]] = []
    for row in view.get("discounts") or []:
        invoiced = _dec(row.get("billed")) if row.get("billedFrom") == "invoice" else None
        billed = invoiced if invoiced is not None else Decimal(0)
        entitled = _dec(row.get("entitled"))
        if entitled is None:
            if billed > 0:
                status, variance = UNKNOWN, None
            else:
                continue
        else:
            variance = billed - entitled
            if abs(variance) <= TOLERANCE:
                continue
            status = OVER if variance > 0 else UNDER
        rows.append({
            "key": row.get("key"), "label": row.get("label"), "status": status,
            "entitled": None if entitled is None else str(entitled), "invoice": str(billed),
            "variance": None if variance is None else str(variance),
        })
    return rows


_REPORT_SQL = """
    SELECT j.journey_id, j.journey_reference,
           COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
           j.created_by_display_name AS pc_name, o.outlet_name,
           jp.model_name_snapshot AS model, jp.variant_name_snapshot AS variant,
           e.item->>'key' AS discount_key, e.item->>'label' AS discount_label,
           e.item->>'status' AS status, (e.item->>'entitled')::numeric AS entitled,
           (e.item->>'invoice')::numeric AS invoice, (e.item->>'variance')::numeric AS variance
    FROM auditcore.p2_journey_runtime r
    JOIN auditcore.journeys j ON j.tenant_id = r.tenant_id AND j.journey_id = r.journey_id
    CROSS JOIN LATERAL jsonb_array_elements(COALESCE(r.discount_variance, '[]'::jsonb)) AS e(item)
    LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    LEFT JOIN auditcore.dealer_outlets o
      ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
    WHERE r.tenant_id = :tenant_id
      AND (CAST(:dealer_id AS uuid) IS NULL OR j.dealer_id = CAST(:dealer_id AS uuid))
      AND (CAST(:outlet_id AS uuid) IS NULL OR j.outlet_id = CAST(:outlet_id AS uuid))
    ORDER BY (e.item->>'status' = 'ENTITLEMENT_UNKNOWN'), abs(COALESCE((e.item->>'variance')::numeric, 0)) DESC,
             j.journey_reference
    LIMIT :limit
"""


def discount_variance_report(
    connection: Connection, *, tenant_id: str, dealer_id: UUID | None = None, outlet_id: UUID | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    rows = connection.execute(
        text(_REPORT_SQL),
        {"tenant_id": tenant_id, "dealer_id": dealer_id, "outlet_id": outlet_id, "limit": limit},
    ).mappings().all()
    items = [{
        "journeyId": str(r["journey_id"]), "journeyReference": r["journey_reference"],
        "customer": r["customer"], "pc": r["pc_name"], "outlet": r["outlet_name"],
        "model": r["model"], "variant": r["variant"],
        "discountKey": r["discount_key"], "discount": r["discount_label"], "status": r["status"],
        "entitled": None if r["entitled"] is None else str(r["entitled"]),
        "invoice": None if r["invoice"] is None else str(r["invoice"]),
        "variance": None if r["variance"] is None else str(r["variance"]),
    } for r in rows]
    pending = connection.execute(
        text(
            """
            SELECT count(*) FROM auditcore.p2_journey_runtime
            WHERE tenant_id = :tenant_id AND (discount_variance IS NULL
               OR price_variance_fact_version IS NULL OR price_variance_fact_version <> fact_version)
            """
        ),
        {"tenant_id": tenant_id},
    ).scalar_one()
    return {
        "items": items,
        "counts": {
            "total": len(items),
            "over": sum(1 for i in items if i["status"] == OVER),
            "under": sum(1 for i in items if i["status"] == UNDER),
            "unknown": sum(1 for i in items if i["status"] == UNKNOWN),
            "journeys": len({i["journeyId"] for i in items}),
        },
        "notYetCalculated": int(pending),
        "toleranceRupees": int(TOLERANCE),
    }


@router.get("/discounts/variance")
def read_discount_variance(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    outlet_id: UUID | None = None,
    dealer_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> dict[str, Any]:
    """Discounts whose invoice figure is not the entitlement, with the discount
    category, PC, outlet, customer and car model. Read-only; invoice figures only."""
    authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    return discount_variance_report(
        connection, tenant_id=tenant_id, dealer_id=dealer_id, outlet_id=outlet_id, limit=limit)
