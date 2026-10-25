"""uc03_journey_review.py -- the all-Journey review: what the system holds for
every Journey, side by side, so a wrong figure can be seen and traced.

Asked by the product owner on 2026-10-25: for every Journey, is the SKU
identified, does the price by SKU match the booking form, what is the
ex-showroom on the invoice, is insurance uploaded / in the invoice / in-house
or outside, do the invoice discounts match the SKU's, any scrappage certificate,
any credit note, and what the price-breakdown documents (cost sheet, ledger,
bank letter, no-dues certificate) carry. FACTS ONLY: nothing here changes a
Journey, a calculation or a task; it reads the records the Deal reads.

The same queries feed the SuperAdmin Reports page (GET .../admin/reports/
journey-review) and the worker diagnostics (rv_*), so the two cannot
differ.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import Engine, text

from audit_core.db import set_tenant_context
from audit_core.dependencies import (
    HumanAdminRequest,
    get_engine,
    require_super_admin_request,
)

router = APIRouter(prefix="/v1/tenants/{tenant_id}/admin/reports", tags=["admin-reports"])


def _active_doc_fields(document_type: str, pattern: str | None = None, *, width: int = 48) -> str:
    """One compact ``field=value | ...`` string of the ACTIVE document(s) of a
    type on the Journey -- the documents the Deal itself reads (evidence with
    association_status ACTIVE), optionally only the fields matching a regex."""
    keep = f"AND f.field_key ~ '{pattern}'" if pattern else ""
    return f"""(SELECT string_agg(f.field_key || '=' || left(f.effective_value #>> '{{}}', {width}), ' | '
                                  ORDER BY f.field_key)
                  FROM auditcore.evidence e
                  JOIN auditcore.journey_document_extracted_fields f
                    ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
                   AND f.di_document_id = e.di_document_id
                 WHERE e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                   AND e.association_status = 'ACTIVE' AND e.document_type_key = '{document_type}'
                   AND f.effective_value IS NOT NULL {keep})"""


# Review of every Journey (2026-10-25: "review all the journeys ... no changes,
# only fact finding"): SKU, price, invoices, insurance, discounts, scrappage and
# the price-breakdown documents, one row per Journey (invoices: per document).
REVIEW_SQL: dict[str, str] = {
    "journey": """
        SELECT j.journey_id::text AS journey_id, j.journey_reference AS ref,
               COALESCE(NULLIF(c.display_name, ''), j.journey_reference) AS customer,
               j.created_by_display_name AS pc, o.outlet_name AS outlet, r.current_stage AS stage,
               to_char(j.created_at_utc, 'YYYY-MM-DD') AS created,
               jp.model_name_snapshot AS booked_model, jp.variant_name_snapshot AS booked_variant,
               s.sku_code, jp.selection_status AS sel_status, jp.selection_method AS sel_method,
               jp.selection_score AS sel_score, jp.vehicle_category AS category,
               b.booking_date::text AS booking_date, b.pricing_effective_on::text AS pricing_on,
               plv.version_no AS pl_version, plv.effective_from::text AS pl_from,
               (SELECT pli.standard_amount::text FROM auditcore.price_list_items pli
                 WHERE pli.tenant_id = j.tenant_id AND pli.price_list_version_id = j.price_list_version_id
                   AND pli.product_sku_id = jp.product_sku_id AND pli.component_key = 'EX_SHOWROOM'
                 LIMIT 1) AS std_ex_showroom,
               (SELECT string_agg(sv.source_document_type || '=' || sv.amount::text, ' | '
                                  ORDER BY sv.source_document_type)
                  FROM auditcore.commercial_line_source_values sv
                 WHERE sv.tenant_id = j.tenant_id AND sv.journey_id = j.journey_id
                   AND sv.line_kind = 'COMMERCIAL' AND sv.component_key = 'ex_showroom_price') AS ex_showroom_sources,
               (SELECT cl.actual_amount::text || ' @' || COALESCE(split_part(cl.source_reference, ':', 1), '')
                  FROM auditcore.commercial_lines cl
                 WHERE cl.tenant_id = j.tenant_id AND cl.journey_id = j.journey_id
                   AND cl.component_key = 'ex_showroom_price' LIMIT 1) AS deal_ex_showroom,
               r.price_variance::text AS price_variance
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers c ON c.tenant_id = j.tenant_id AND c.customer_id = j.customer_id
        LEFT JOIN auditcore.dealer_outlets o
          ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
        LEFT JOIN auditcore.journey_products jp ON jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
        LEFT JOIN auditcore.product_skus s ON s.product_sku_id = jp.product_sku_id
        LEFT JOIN auditcore.bookings b ON b.tenant_id = j.tenant_id AND b.journey_id = j.journey_id
        LEFT JOIN auditcore.price_list_versions plv
          ON plv.tenant_id = j.tenant_id AND plv.price_list_version_id = j.price_list_version_id
        LEFT JOIN auditcore.p2_journey_runtime r ON r.tenant_id = j.tenant_id AND r.journey_id = j.journey_id
        WHERE j.tenant_id = :t
        ORDER BY j.journey_reference
    """,
    "docs": """
        SELECT j.journey_reference AS ref,
               (SELECT string_agg(x.t || 'x' || x.n, ', ' ORDER BY x.t)
                  FROM (SELECT e.document_type_key AS t, count(*) AS n FROM auditcore.evidence e
                         WHERE e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                           AND e.association_status = 'ACTIVE'
                         GROUP BY e.document_type_key) x) AS active_docs,
               (SELECT string_agg(x.t || 'x' || x.n, ', ' ORDER BY x.t)
                  FROM (SELECT e.document_type_key || '/' || e.association_status AS t, count(*) AS n
                          FROM auditcore.evidence e
                         WHERE e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                           AND e.association_status <> 'ACTIVE'
                         GROUP BY 1) x) AS other_docs,
               (SELECT string_agg(DISTINCT left(d.original_filename, 50), ' | ')
                  FROM auditcore.document_capture_v2_documents d
                 WHERE d.tenant_id = j.tenant_id AND d.journey_id = j.journey_id
                   AND d.original_filename ~* '(due|clear|noc|ledger|statement|break)') AS notable_files
        FROM auditcore.journeys j
        WHERE j.tenant_id = :t
        ORDER BY j.journey_reference
    """,
    "invoices": """
        SELECT j.journey_reference AS ref, e.document_type_key AS type, e.association_status AS assoc,
               to_char(e.linked_at_utc, 'MM-DD HH24:MI') AS linked, left(e.di_document_id::text, 8) AS doc,
               max(CASE WHEN f.field_key = 'invoice_number' THEN f.effective_value #>> '{}' END) AS inv_no,
               max(CASE WHEN f.field_key = 'invoice_date' THEN f.effective_value #>> '{}' END) AS inv_date,
               max(CASE WHEN f.field_key = 'gross_amount_before_discount' THEN f.effective_value #>> '{}' END) AS gross,
               max(CASE WHEN f.field_key = 'invoice_discount_amount' THEN f.effective_value #>> '{}' END) AS discount,
               max(CASE WHEN f.field_key = 'taxable_amount' THEN f.effective_value #>> '{}' END) AS taxable,
               max(CASE WHEN f.field_key = 'cgst_rate' THEN f.effective_value #>> '{}' END) AS cgst_rate,
               max(CASE WHEN f.field_key = 'sgst_rate' THEN f.effective_value #>> '{}' END) AS sgst_rate,
               max(CASE WHEN f.field_key = 'igst_rate' THEN f.effective_value #>> '{}' END) AS igst_rate,
               max(CASE WHEN f.field_key = 'tcs_amount' THEN f.effective_value #>> '{}' END) AS tcs,
               max(CASE WHEN f.field_key = 'cgst_amount' THEN f.effective_value #>> '{}' END) AS cgst_amt,
               max(CASE WHEN f.field_key = 'sgst_amount' THEN f.effective_value #>> '{}' END) AS sgst_amt,
               max(CASE WHEN f.field_key = 'igst_amount' THEN f.effective_value #>> '{}' END) AS igst_amt,
               max(CASE WHEN f.field_key = 'cess_amount' THEN f.effective_value #>> '{}' END) AS cess_amt,
               max(CASE WHEN f.field_key = 'invoice_nature' THEN left(f.effective_value #>> '{}', 30) END) AS nature,
               max(CASE WHEN f.field_key = 'financed_by' THEN left(f.effective_value #>> '{}', 30) END) AS financed_by,
               max(CASE WHEN f.field_key = 'buyer_name' THEN left(f.effective_value #>> '{}', 30) END) AS buyer,
               max(CASE WHEN f.field_key = 'round_off_amount' THEN f.effective_value #>> '{}' END) AS round_off,
               max(CASE WHEN f.field_key = 'grand_total_amount' THEN f.effective_value #>> '{}' END) AS grand_total,
               max(CASE WHEN f.field_key = 'model_name_raw' THEN left(f.effective_value #>> '{}', 40) END) AS model_raw,
               max(CASE WHEN f.field_key = 'variant_raw' THEN left(f.effective_value #>> '{}', 40) END) AS variant_raw,
               max(CASE WHEN f.field_key = 'chassis_number' THEN f.effective_value #>> '{}' END) AS chassis,
               (count(f.field_key) - count(DISTINCT f.field_key)) AS dup_field_rows,
               left(max(CASE WHEN f.field_key = 'line_items' THEN f.effective_value #>> '{}' END), 1100) AS lines
        FROM auditcore.evidence e
        JOIN auditcore.journeys j ON j.tenant_id = e.tenant_id AND j.journey_id = e.journey_id
        LEFT JOIN auditcore.journey_document_extracted_fields f
          ON f.tenant_id = e.tenant_id AND f.journey_id = e.journey_id
         AND f.di_document_id = e.di_document_id AND f.effective_value IS NOT NULL
        WHERE e.tenant_id = :t
          AND e.document_type_key IN ('customer_invoice_dms', 'tax_invoice_tally', 'credit_note',
                                      'wholesale_invoice', 'accessory_invoice_dms', 'accessory_invoice_tally')
        GROUP BY j.journey_reference, e.document_type_key, e.association_status, e.linked_at_utc, e.di_document_id
        ORDER BY j.journey_reference, e.linked_at_utc
    """,
    "insurance": f"""
        SELECT j.journey_reference AS ref, o.outlet_name AS outlet,
               ir.insurer_name, ir.policy_reference, ir.insurance_by, ir.self_insurance_flag::text AS self_flag,
               ir.insurance_source, ir.agent_intermediary_name AS record_intermediary,
               ir.standard_premium_amount::text AS std_premium, ir.actual_premium_amount::text AS record_premium,
               ir.actual_status_code AS record_status,
               {_active_doc_fields('insurance_cover', '^(insurer_name|premium_amount|idv_amount|policy_type|agent_intermediary_name|agent_intermediary_code|misp_code|issue_date|policy_start_date)$', width=60)} AS cover_doc,
               (SELECT string_agg(sv.source_document_type || '=' || sv.amount::text, ' | '
                                  ORDER BY sv.source_document_type)
                  FROM auditcore.commercial_line_source_values sv
                 WHERE sv.tenant_id = j.tenant_id AND sv.journey_id = j.journey_id
                   AND sv.line_kind = 'COMMERCIAL' AND sv.component_key = 'insurance_amount') AS insurance_sources,
               (SELECT cl.actual_amount::text || ' @' || COALESCE(split_part(cl.source_reference, ':', 1), '')
                  FROM auditcore.commercial_lines cl
                 WHERE cl.tenant_id = j.tenant_id AND cl.journey_id = j.journey_id
                   AND cl.component_key = 'insurance_amount' LIMIT 1) AS deal_insurance,
               (SELECT pli.standard_amount::text FROM auditcore.price_list_items pli
                  JOIN auditcore.journey_products jp ON jp.product_sku_id = pli.product_sku_id
                 WHERE pli.tenant_id = j.tenant_id AND pli.price_list_version_id = j.price_list_version_id
                   AND jp.tenant_id = j.tenant_id AND jp.journey_id = j.journey_id
                   AND pli.component_key = 'INSURANCE' LIMIT 1) AS std_insurance,
               {_active_doc_fields('booking_docket', '^insurance')} AS booking_insurance
        FROM auditcore.journeys j
        LEFT JOIN auditcore.dealer_outlets o
          ON o.tenant_id = j.tenant_id AND o.dealer_id = j.dealer_id AND o.outlet_id = j.outlet_id
        LEFT JOIN LATERAL (
            SELECT * FROM auditcore.insurance_records i
             WHERE i.tenant_id = j.tenant_id AND i.journey_id = j.journey_id
             ORDER BY i.updated_at_utc DESC LIMIT 1) ir ON true
        WHERE j.tenant_id = :t
        ORDER BY j.journey_reference
    """,
    "discounts": f"""
        SELECT j.journey_reference AS ref,
               (SELECT string_agg(da.discount_key || ' elig=' || COALESCE(da.standard_eligible_amount::text, '-')
                                  || ',a=' || COALESCE(da.actual_discount_amount::text, '-')
                                  || ',' || COALESCE(da.eligibility_result, '-'), ' | ' ORDER BY da.discount_key)
                  FROM auditcore.discount_applications da
                 WHERE da.tenant_id = j.tenant_id AND da.journey_id = j.journey_id) AS applications,
               (SELECT string_agg(sv.component_key || '=' || sv.amount::text || '@' || sv.source_document_type,
                                  ' | ' ORDER BY sv.component_key, sv.source_document_type)
                  FROM auditcore.commercial_line_source_values sv
                 WHERE sv.tenant_id = j.tenant_id AND sv.journey_id = j.journey_id
                   AND sv.line_kind = 'DISCOUNT') AS discount_sources,
               (SELECT count(*) FROM auditcore.evidence e
                 WHERE e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                   AND e.association_status = 'ACTIVE'
                   AND e.document_type_key = 'scrappage_certificate_of_deposit') AS scrappage_certs,
               (SELECT count(*) FROM auditcore.evidence e
                 WHERE e.tenant_id = j.tenant_id AND e.journey_id = j.journey_id
                   AND e.association_status = 'ACTIVE' AND e.document_type_key = 'credit_note') AS credit_notes,
               {_active_doc_fields('booking_docket', '(discount|bonus|scrap|exchange)')} AS booking_discounts,
               {_active_doc_fields('cost_sheet', '(discount|bonus|scrap|exchange)')} AS cost_sheet_discounts
        FROM auditcore.journeys j
        WHERE j.tenant_id = :t
        ORDER BY j.journey_reference
    """,
    "pricedocs": f"""
        SELECT j.journey_reference AS ref,
               {_active_doc_fields('cost_sheet', width=40)} AS cost_sheet,
               {_active_doc_fields('booking_docket', '(price|amount|total|ex_showroom|road|rto|accessor)', width=30)} AS booking_docket,
               {_active_doc_fields('bank_approval_letter', '(considered|invoice_or_proforma|sanctioned|margin)', width=30)} AS bank_letter,
               {_active_doc_fields('customer_ledger', width=30)} AS ledger,
               {_active_doc_fields('no_dues_certificate', width=30)} AS no_dues
        FROM auditcore.journeys j
        WHERE j.tenant_id = :t
        ORDER BY j.journey_reference
    """,
}

# Queries that give one row per Journey, merged on the Journey reference; the
# invoice query gives one row per invoice-type document and is nested instead.
_PER_JOURNEY = ("journey", "docs", "insurance", "discounts", "pricedocs")


def _cell(value: Any) -> Any:
    return value if value is None or isinstance(value, (str, int, float, bool)) else str(value)


def journey_review(engine: Engine, tenant_id: str) -> dict[str, Any]:
    """Every Journey of the Project with the facts held for it. Read-only."""
    results: dict[str, list[dict[str, Any]]] = {}
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION READ ONLY"))
        set_tenant_context(connection, tenant_id)
        for name, sql in REVIEW_SQL.items():
            results[name] = [
                {k: _cell(v) for k, v in row.items()}
                for row in connection.execute(text(sql), {"t": tenant_id}).mappings()
            ]
    journeys: dict[str, dict[str, Any]] = {}
    for name in _PER_JOURNEY:
        for row in results[name]:
            ref = row.get("ref")
            merged = journeys.setdefault(ref, {"invoices": []})
            merged.update({k: v for k, v in row.items() if k != "ref"})
            merged["ref"] = ref
    for row in results["invoices"]:
        journeys.setdefault(row.get("ref"), {"ref": row.get("ref"), "invoices": []})["invoices"].append(
            {k: v for k, v in row.items() if k != "ref"}
        )
    rows = sorted(journeys.values(), key=lambda r: str(r.get("ref")))
    return {
        "tenantId": tenant_id,
        "generatedAtUtc": datetime.now(UTC).isoformat(),
        "journeyCount": len(rows),
        "journeys": rows,
    }


@router.get("/journey-review")
def read_journey_review(
    tenant_id: str,
    admin_request: Annotated[HumanAdminRequest, Depends(require_super_admin_request)],
    engine: Annotated[Engine, Depends(get_engine)],
) -> dict[str, Any]:
    """SuperAdmin only. The all-Journey review, live from the Project's records."""
    del admin_request
    return journey_review(engine, tenant_id)
