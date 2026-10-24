"""uc03_sku_attention.py -- the SKU work a Team Lead or Reviewer has to clear.

Asked by the product owner on 2026-10-24: one place that shows every Journey
whose vehicle is not settled -- the ones with several options, the ones with no
price, the ones whose booking date cannot price them -- with the tasks already
raised against each and, where the deal is priced, how badly the price
contradicts the words.

Two decisions worth stating, because both were arrived at the hard way today.

The list says WHY each Journey needs attention by reading the tasks already
open on it, not by re-running the resolver. One rule, one place: if a task was
raised the Journey appears, and if it was not it does not. A dashboard that
decided for itself would drift from the tasks it sits beside, and we spent this
morning finding four places where exactly that had happened.

The options a Journey has are NOT computed here. The picker already computes
them, fresh, through ``uc03_model_resolution.get_model_resolution_candidates``,
and a second implementation would be a second answer. The list carries the
reason and the counts; the screen asks for the options when a person opens one.
That also keeps the list cheap, because computing options means loading the
whole price book per Journey.

Read with ``audit.journey.read``, which a Team Lead and a Reviewer both hold,
and the fix goes through the existing confirm-sku endpoint, so both roles use
one path to set a SKU.
"""
from __future__ import annotations

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

router = APIRouter(prefix="/p2/v1/tenants/{tenant_id}", tags=["uc03-phase2-sku-attention"])

_READ_PERMISSION = "audit.journey.read"

# Why a Journey is on this list, in the order a Team Lead should work it: the
# ones nothing can price first, then the ones a person has to choose between.
REASONS: dict[str, tuple[str, str]] = {
    "NO_PRICING_DATE": ("PC_BOOKING_DATE_MISSING",
                        "No usable booking date -- nothing on the deal can be priced"),
    "NO_PRICE_FOR_DATE": ("PRICE_MASTER_NOT_IN_FORCE",
                          "No price available for this vehicle on its pricing date"),
    "NOT_IN_PRICE_BOOK": ("SKU_NOT_IN_PRICE_MASTER",
                          "The SKU set on this Journey is not in the price book in force"),
    "NOTHING_IDENTIFIED": ("SKU_NOT_SET",
                           "No document identified the vehicle"),
    "MULTIPLE_OPTIONS": ("SKU_CLOSEST_MATCH_CONFIRM",
                         "Its words fit more than one SKU -- confirm which"),
    "INVOICE_DISAGREES": ("SKU_INVOICE_REVIEW",
                          "An invoice names a different vehicle from the one set"),
    "DOCUMENTS_DISAGREE": ("SKU_BOOKING_FORMS_DISAGREE",
                           "The booking forms name different vehicles"),
    "PRICE_DIFFERS": ("SKU_PRICE_MISMATCH",
                      "Matched by its words, but the price on the document differs"),
}
_BY_TASK = {task: reason for reason, (task, _label) in REASONS.items()}
_ORDER = list(REASONS)

_OPEN_STATUSES = ("READY", "IN_PROGRESS", "RETURNED", "ACTION_COMPLETED", "VERIFYING")

# How badly the price contradicts the words, for a Journey that already has a
# SKU. A figure naming another variant of the same model is a question; one
# naming another model is a different vehicle. Measured within a rupee, which
# is the band the October price book's own precision allows: no price in it is
# further than 0.50 from a whole rupee (measured 2026-10-24), so a wider band
# only sweeps in genuinely different vehicles.
_CONTRADICTION_BAND = "1"

_ATTENTION_SQL = f"""
WITH scoped AS (
    SELECT j.journey_id, j.tenant_id, j.journey_reference,
           j.created_by_display_name AS pc_name, j.created_at_utc,
           COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
           d.dealer_name, o.outlet_name, o.outlet_id, d.dealer_id,
           COALESCE(b.pricing_effective_on, b.booking_date) AS priced_on,
           b.pricing_effective_on IS NOT NULL AS date_applied_by_a_person,
           b.booking_date,
           jp.product_sku_id, jp.model_name_snapshot, jp.variant_name_snapshot,
           jp.selection_status, jp.match_basis,
           pm.model_name || ' ~ ' || pv.variant_name AS pinned_name,
           s.sku_code AS pinned_sku_code
    FROM auditcore.journeys j
    LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
    LEFT JOIN auditcore.dealers d ON d.tenant_id = j.tenant_id AND d.dealer_id = j.dealer_id
    LEFT JOIN auditcore.dealer_outlets o
      ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
    LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
    LEFT JOIN auditcore.journey_products jp
      ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
    LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
    LEFT JOIN auditcore.product_models pm ON pm.model_id = s.model_id
    LEFT JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
    WHERE j.tenant_id = :tenant_id
      AND (CAST(:outlet_id AS uuid) IS NULL OR o.outlet_id = CAST(:outlet_id AS uuid))
      AND (CAST(:dealer_id AS uuid) IS NULL OR d.dealer_id = CAST(:dealer_id AS uuid))
), tasked AS (
    SELECT sc.*, t.task_id, t.task_type, t.severity, t.task_status, t.title, t.created_at_utc AS task_raised
    FROM scoped sc
    JOIN auditcore.p2_tasks t
      ON t.tenant_id = sc.tenant_id AND t.journey_id = sc.journey_id
     AND t.task_type = ANY(:task_types)
     AND t.task_status = ANY(:open_statuses)
), plan AS (
    SELECT DISTINCT ON (journey_id) journey_id, plv FROM (
        SELECT tk.journey_id, (
            SELECT plv.price_list_version_id
            FROM auditcore.price_list_versions plv
            JOIN auditcore.price_lists pl
              ON pl.tenant_id = plv.tenant_id AND pl.price_list_id = plv.price_list_id
            WHERE plv.tenant_id = :tenant_id
              AND plv.lifecycle_status IN ('PUBLISHED', 'RETIRED')
              AND plv.effective_from <= tk.priced_on
              AND (plv.effective_to IS NULL OR plv.effective_to >= tk.priced_on)
              AND (pl.price_list_code = 'OEM_NATIVE_PRICE_LIST'
                   OR NOT EXISTS (SELECT 1 FROM auditcore.price_lists own
                                   WHERE own.tenant_id = plv.tenant_id
                                     AND own.price_list_code = 'OEM_NATIVE_PRICE_LIST'))
            ORDER BY plv.effective_from DESC, plv.version_no DESC, plv.price_list_version_id DESC
            LIMIT 1) AS plv
        FROM tasked tk
    ) q
), book AS (
    SELECT pli.price_list_version_id, s.product_sku_id,
           pm.model_name, pm.model_name || ' ~ ' || pv.variant_name AS name,
           SUM(pli.standard_amount) FILTER (WHERE pli.component_key = 'EX_SHOWROOM') AS ex_showroom
    FROM auditcore.price_list_items pli
    JOIN auditcore.product_skus s      ON s.product_sku_id = pli.product_sku_id
    JOIN auditcore.product_models pm   ON pm.model_id = s.model_id
    JOIN auditcore.product_variants pv ON pv.variant_id = s.variant_id
    WHERE pli.tenant_id = :tenant_id AND s.is_active AND pm.is_active AND pv.is_active
    GROUP BY 1, 2, 3, 4
), figures AS (
    SELECT f.journey_id,
           COALESCE(dt.classified_document_type_key, f.source_document_type_key) AS document,
           ROUND(replace(COALESCE((regexp_match(f.effective_value #>> '{{}}',
                         '[0-9][0-9,]*(?:\\.[0-9]+)?'))[1], '0'), ',', '')::numeric, 2) AS amount
    FROM auditcore.journey_document_extracted_fields f
    LEFT JOIN auditcore.document_capture_v2_documents dt
      ON dt.tenant_id = f.tenant_id AND dt.journey_id = f.journey_id
     AND dt.di_document_id = f.di_document_id
    WHERE f.tenant_id = :tenant_id AND f.field_key = 'ex_showroom_price'
      AND f.effective_value IS NOT NULL
      AND COALESCE(f.source_document_type_key, '') <> 'wholesale_invoice'
    GROUP BY 1, 2, 3
)
SELECT tk.journey_id, tk.journey_reference, tk.customer, tk.pc_name,
       tk.dealer_name, tk.outlet_name, tk.created_at_utc,
       tk.model_name_snapshot AS booked_model, tk.variant_name_snapshot AS booked_variant,
       tk.priced_on, tk.date_applied_by_a_person, tk.booking_date,
       tk.product_sku_id, tk.pinned_sku_code, tk.pinned_name,
       tk.selection_status, tk.match_basis,
       (pl.plv IS NOT NULL) AS has_a_price_book,
       tk.task_id, tk.task_type, tk.severity, tk.task_status, tk.title, tk.task_raised,
       con.names AS price_names, con.gap AS price_gap,
       con.document AS price_document, con.amount AS figure_read
FROM tasked tk
LEFT JOIN plan pl ON pl.journey_id = tk.journey_id
LEFT JOIN LATERAL (
    -- The one variant a figure on this Journey names within a rupee, when it
    -- is not the one already set. Nothing when they agree or when the figure
    -- names several.
    SELECT fg.document, fg.amount, m.names, m.gap
    FROM figures fg
    CROSS JOIN LATERAL (
        SELECT COUNT(DISTINCT bk.name) AS variants, MIN(bk.name) AS names,
               MIN(ABS(bk.ex_showroom - fg.amount)) AS gap
        FROM book bk
        WHERE bk.price_list_version_id = pl.plv
          AND bk.ex_showroom IS NOT NULL
          AND ABS(bk.ex_showroom - fg.amount) <= {_CONTRADICTION_BAND}
    ) m
    WHERE fg.journey_id = tk.journey_id
      AND m.variants = 1
      AND tk.pinned_name IS NOT NULL
      AND m.names <> tk.pinned_name
    ORDER BY fg.document <> 'booking_form', fg.amount
    LIMIT 1
) con ON true
ORDER BY tk.journey_reference, tk.task_type
"""


def _contradiction(pinned: str | None, names: str | None, gap: Any) -> dict[str, Any] | None:
    """How far apart the words and the figure are. VARIANT when the figure
    names another variant of the same model (a question for a person); MODEL
    when it names a different vehicle entirely (the serious one)."""
    if not pinned or not names:
        return None
    level = "MODEL" if pinned.split(" ~ ")[0] != names.split(" ~ ")[0] else "VARIANT"
    return {"level": level, "priceNames": names, "gap": str(gap) if gap is not None else None}


def sku_attention(
    connection: Connection,
    *,
    tenant_id: str,
    outlet_id: UUID | None = None,
    dealer_id: UUID | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Every Journey with open SKU or pricing work, one row each, with its
    tasks and how badly its price contradicts its words."""
    rows = connection.execute(
        text(_ATTENTION_SQL),
        {
            "tenant_id": tenant_id,
            "outlet_id": outlet_id,
            "dealer_id": dealer_id,
            "task_types": [task for task, _ in REASONS.values()],
            "open_statuses": list(_OPEN_STATUSES),
        },
    ).mappings().all()

    items: dict[UUID, dict[str, Any]] = {}
    for row in rows:
        item = items.get(row["journey_id"])
        if item is None:
            contradiction = _contradiction(row["pinned_name"], row["price_names"], row["price_gap"])
            item = items[row["journey_id"]] = {
                "journeyId": row["journey_id"],
                "journeyReference": row["journey_reference"],
                "customer": row["customer"],
                "pcName": row["pc_name"],
                "dealerName": row["dealer_name"],
                "outletName": row["outlet_name"],
                "bookedModel": row["booked_model"],
                "bookedVariant": row["booked_variant"],
                "pricedOn": row["priced_on"],
                "pricingDateFromAPerson": bool(row["date_applied_by_a_person"]),
                "hasAPriceBook": bool(row["has_a_price_book"]),
                "pinnedSkuId": row["product_sku_id"],
                "pinnedSkuCode": row["pinned_sku_code"],
                "pinnedVehicle": row["pinned_name"],
                "selectionStatus": row["selection_status"],
                "matchBasis": row["match_basis"],
                "figureRead": str(row["figure_read"]) if row["figure_read"] is not None else None,
                "figureDocument": row["price_document"],
                "contradiction": contradiction,
                "reasons": [],
                "tasks": [],
            }
        reason = _BY_TASK.get(row["task_type"])
        if reason and reason not in item["reasons"]:
            item["reasons"].append(reason)
        item["tasks"].append({
            "taskId": row["task_id"],
            "taskType": row["task_type"],
            "severity": row["severity"],
            "status": row["task_status"],
            "title": row["title"],
            "raisedAt": row["task_raised"],
        })

    ordered = sorted(
        items.values(),
        # Worst first: a contradiction over a different model, then the
        # Journeys nothing can price, then the ones awaiting a choice.
        key=lambda i: (
            0 if (i["contradiction"] or {}).get("level") == "MODEL" else 1,
            min((_ORDER.index(r) for r in i["reasons"]), default=len(_ORDER)),
            i["journeyReference"] or "",
        ),
    )
    summary = {reason: 0 for reason in REASONS}
    contradicted = {"MODEL": 0, "VARIANT": 0}
    for item in ordered:
        for reason in item["reasons"]:
            summary[reason] += 1
        level = (item["contradiction"] or {}).get("level")
        if level:
            contradicted[level] += 1
    return {
        "needsAttention": len(ordered),
        "byReason": summary,
        "reasonLabels": {reason: label for reason, (_task, label) in REASONS.items()},
        "contradicted": contradicted,
        "items": ordered[: max(1, min(limit, 500))],
    }


@router.get("/sku/attention")
def read_sku_attention(
    tenant_id: str,
    human_principal: Annotated[HumanPrincipal, Depends(get_human_principal)],
    authorization_client: Annotated[
        SecurityAuthorizationClient, Depends(get_security_authorization_client)
    ],
    connection: Annotated[Connection, Depends(get_connection)],
    outlet_id: UUID | None = None,
    dealer_id: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict[str, Any]:
    """The SKU work outstanding for this Project: Journeys with several
    options, with no price, with no usable date, and those whose price
    contradicts the vehicle set on them.

    Read-only. A Team Lead and a Reviewer both hold audit.journey.read, and
    both set a SKU through the same confirm-sku endpoint the picker uses."""
    authorize_p2(
        connection,
        tenant_id=tenant_id,
        journey_id=None,
        human_principal=human_principal,
        authorization_client=authorization_client,
        permission_key=_READ_PERMISSION,
    )
    return sku_attention(
        connection, tenant_id=tenant_id, outlet_id=outlet_id, dealer_id=dealer_id, limit=limit,
    )
