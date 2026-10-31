"""uc03_discount_variance.py -- where a discount given is more than the
Journey was entitled to.

Asked by the product owner on 2026-10-24: a tab that highlights any variance in
discounts, with the discount category, PC, outlet, customer and car model
beside each.

``discount_variance_rows`` reads the Deal as the Deal tab builds it, so the
figures here are the Deal's own and cannot drift from it; ``refresh_price_variance``
stores the result on the Journey's runtime row beside the price variance (same
refresh, same staleness), and the report below reads that, in one query.

2026-10-31 (product owner: "Excess Discount tab is empty dont see any discount
given more"). The EXCESS_DISCOUNT rule had 25 open "Discount given beyond the
entitlement" tasks in Mahindra Orissa and the tab showed none: the tab counted
only a figure taken from an invoice, and that Project's Deal reads the NDC /
account statement in place of the invoice, so every discount was Rs 0 on the
tab; and a discount with no entitlement on record was "entitlement unknown"
here while the rule takes it as an entitlement of zero. Both now read the
given figure, the entitlement and the Rs 100 tolerance through
``uc03_discount_given``, so a discount the rule raises its task on is a row
here, with the document its figure came from beside it.

Which Journeys are tallied:
- a Project whose Deal takes its figures from the account statement
  (``ACCOUNT_STATEMENT``): every Journey the rule judges -- the NDC, else the
  cost sheet standing in for it, else what the booking form offered;
- every other Project: as decided on 2026-10-24, only once the vehicle invoice
  is on file and its price was read.
"""
from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy import Connection, text

from audit_core import uc03_discount_given as dg
from audit_core.dependencies import get_connection, get_human_principal
from audit_core.security import HumanPrincipal
from audit_core.security_authorization import (
    SecurityAuthorizationClient,
    get_security_authorization_client,
)
from audit_core.uc03_p2_access import authorize_p2
from audit_core.uc03_p2_journey360 import _dec

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-discount-variance"])

_READ_PERMISSION = "audit.journey.read"
TOLERANCE = dg.LINE_TOLERANCE
OVER, UNDER, UNKNOWN = "OVER_ENTITLEMENT", "UNDER_ENTITLEMENT", "ENTITLEMENT_UNKNOWN"


def _vehicle_price_read(view: dict[str, Any]) -> bool:
    for group in view.get("categories") or []:
        for row in group.get("components") or []:
            if row.get("key") == "ex_showroom_price":
                return _dec(row.get("current")) is not None
    return False


def _tallied(view: dict[str, Any]) -> bool:
    summary = view.get("summary") or {}
    if summary.get("billedSource") == "ACCOUNT_STATEMENT":
        # The account statement is this Project's source of truth: no vehicle
        # invoice is read into its Deal, so none is required here.
        return True
    return bool(summary.get("vehicleInvoiceOnFile")) and _vehicle_price_read(view)


def discount_variance_rows(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The discounts of this Deal given above or below their entitlement.

    Read exactly as the EXCESS_DISCOUNT rule reads them (``uc03_discount_given``):
    a line nothing gives a figure on is not a discount; an entitlement that is
    not known is reported as such, never as zero; no entitlement on record is
    an entitlement of zero. ``invoice`` keeps its name for the screens and is
    the amount given; ``givenDocument`` says which document it came from."""
    if not _tallied(view) or not dg.standards_known(view):
        return []
    rows: list[dict[str, Any]] = []
    for row in view.get("discounts") or []:
        if not dg.has_figure(row):
            continue
        given, column = dg.given(row)
        if given is None:
            continue
        if dg.entitlement_unknown(row):
            if given <= 0:
                continue
            status, entitled, variance = UNKNOWN, None, None
        else:
            entitled = dg.entitled(row)
            variance = given - entitled
            if dg.is_over(given, entitled):
                status = OVER
            elif -variance > dg.LINE_TOLERANCE:
                status = UNDER
            else:
                continue
        rows.append({
            "key": row.get("key"), "label": row.get("label"), "status": status,
            "entitled": None if entitled is None else str(entitled), "invoice": str(given),
            "variance": None if variance is None else str(variance),
            "givenFrom": column, "givenDocument": dg.given_document(row, column),
        })
    return rows


_REPORT_SQL = """
    SELECT j.journey_id, j.journey_reference,
           COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
           j.created_by_display_name AS pc_name, o.outlet_name,
           jp.model_name_snapshot AS model, jp.variant_name_snapshot AS variant,
           e.item->>'key' AS discount_key, e.item->>'label' AS discount_label,
           e.item->>'status' AS status, (e.item->>'entitled')::numeric AS entitled,
           (e.item->>'invoice')::numeric AS invoice, (e.item->>'variance')::numeric AS variance,
           e.item->>'givenFrom' AS given_from, e.item->>'givenDocument' AS given_document
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
      -- 2026-10-31: a cancelled or duplicate booking, or one closed with no
      -- delivery, is no deal to audit (the Journey list's own "cancelled").
      -- A deleted Journey has no journeys row and so no row here.
      AND NOT EXISTS (
        SELECT 1 FROM auditcore.journey_stage_states bs
        WHERE bs.tenant_id = j.tenant_id AND bs.journey_id = j.journey_id AND bs.stage_code = 'BOOKING'
          AND (bs.business_status IN ('BOOKING_CANCELLED', 'DUPLICATE_BOOKING')
               OR bs.closure_disposition = 'NO_DELIVERY'))
    -- Over-grants first, so a long list of under-grants never crowds them out
    -- of the limit (2026-10-31); then under, then entitlement unknown.
    ORDER BY CASE e.item->>'status' WHEN 'OVER_ENTITLEMENT' THEN 0 WHEN 'UNDER_ENTITLEMENT' THEN 1 ELSE 2 END,
             abs(COALESCE((e.item->>'variance')::numeric, 0)) DESC, j.journey_reference, e.item->>'key'
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
        "givenFrom": r["given_from"], "givenDocument": r["given_document"],
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
    """Discounts given above or below the entitlement, with the discount
    category, PC, outlet, customer and car model and the document each figure
    came from. Read-only; over-grants first."""
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
