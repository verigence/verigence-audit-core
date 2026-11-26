"""Read-only: what the Excess Discount list holds for one customer's bookings and what
the Deal gives now. Prints references and figures only."""
from __future__ import annotations

import json
import os
from uuid import UUID

from sqlalchemy import create_engine, text

from audit_core import uc03_discount_given as dg
from audit_core.db import set_tenant_context
from audit_core.uc03_discount_variance import _tallied, discount_variance_rows
from audit_core.uc03_p2_journey360 import deal
from audit_core.uc03_statement_totals import charges_in_doubt

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"  # Mahindra Orissa
PATTERN = "%SANTOSH%PANI%"
KEEP = ("key", "label", "billed", "booking", "current", "standard", "entitled", "eligibility", "potential",
        "sameAs", "inPrice", "fromStatement", "billedFrom", "evidenceStatus", "grid", "status", "note", "effective")


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})
with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    journeys = c.execute(text("""
        SELECT DISTINCT j.journey_id, j.journey_reference, j.created_at_utc
        FROM auditcore.journeys j
        LEFT JOIN auditcore.customers cu ON cu.tenant_id = j.tenant_id AND cu.customer_id = j.customer_id
        WHERE j.tenant_id = :t AND (cu.legal_name ILIKE :p OR cu.display_name ILIKE :p OR EXISTS (
            SELECT 1 FROM auditcore.journey_document_extracted_fields f
            WHERE f.tenant_id = j.tenant_id AND f.journey_id = j.journey_id AND f.field_key = 'customer_name'
              AND jsonb_typeof(f.effective_value) = 'string' AND f.effective_value #>> '{}' ILIKE :p))
        ORDER BY j.created_at_utc"""), {"t": T, "p": PATTERN}).mappings().all()
    print("journeys found:", len(journeys))
    for row in journeys:
        jid = UUID(str(row["journey_id"]))
        print("\n" + "=" * 100)
        print("JOURNEY", row["journey_reference"], jid, "created", row["created_at_utc"])
        params = {"t": T, "j": jid}
        rt = c.execute(text("""
            SELECT r.current_stage, r.fact_version, r.price_variance_fact_version, r.price_variance,
                   r.discount_variance, r.updated_at_utc
            FROM auditcore.p2_journey_runtime r WHERE r.tenant_id = :t AND r.journey_id = :j"""), params).mappings().first()
        print("runtime:", j(dict(rt)) if rt else None)
        for s in c.execute(text("""
            SELECT stage_code, business_status, closure_disposition, business_completed_at_utc
            FROM auditcore.journey_stage_states WHERE tenant_id = :t AND journey_id = :j"""), params).mappings():
            print("stage:", j(dict(s)))
        print("classification:", j([dict(x) for x in c.execute(text(
            "SELECT deal_type FROM auditcore.journey_classifications WHERE tenant_id = :t AND journey_id = :j"),
            params).mappings()]))
        for t in c.execute(text("""
            SELECT source_code, task_status, assigned_role_code, title, left(description, 600) AS description,
                   created_at_utc, updated_at_utc
            FROM auditcore.p2_tasks WHERE tenant_id = :t AND journey_id = :j
              AND (source_code ILIKE '%DISCOUNT%' OR source_code ILIKE '%KIT%' OR title ILIKE '%discount%'
                   OR description ILIKE '%discount%')
            ORDER BY created_at_utc"""), params).mappings():
            print("task:", j(dict(t)))
        try:
            view = deal(c, tenant_id=T, journey_id=jid)
        except Exception as exc:  # noqa: BLE001
            print("DEAL FAILED:", repr(exc))
            continue
        summary = view.get("summary") or {}
        print("summary:", j({k: v for k, v in summary.items() if k != "sources"}))
        print("tallied:", _tallied(view), "standards_known:", dg.standards_known(view))
        for group in view.get("categories") or []:
            for comp in group.get("components") or []:
                if comp.get("current") is not None or comp.get("billed") is not None:
                    print("  component:", j({k: comp.get(k) for k in ("key", "label", "billed", "current", "standard")}))
        for d in view.get("discounts") or []:
            slim = {k: d.get(k) for k in KEEP if d.get(k) is not None}
            slim["scheme"] = {k: (d.get("scheme") or {}).get(k) for k in ("code", "name", "category")} if d.get("scheme") else None
            slim["sources"] = [{k: s.get(k) for k in ("document", "documentType", "amount", "documentId")}
                               for s in d.get("sources") or []]
            slim["counted"] = dg.counted(d)
            slim["has_figure"] = dg.has_figure(d)
            slim["unknown_reason"] = dg.unknown_reason(d)
            print("  discount:", j(slim))
        doubt = charges_in_doubt(c, tenant_id=T, journey_id=jid)
        print("ndc in doubt:", j(doubt))
        for item in dg.assess(view.get("discounts") or [], doubt):
            print("  assess:", j({"key": item["row"].get("key"), "label": item["row"].get("label"),
                                  **{k: v for k, v in item.items() if k != "row"}}))
        print("rows computed now:", j(discount_variance_rows(view, doubt)))

# Second pass: how the dealer grid tier (in-house insurance or not) was decided.
from audit_core import uc03_insurance_source as ins  # noqa: E402
from audit_core.uc03_account_statement import line_rows, load_ledger_documents  # noqa: E402
from audit_core.uc03_deal_reconciliation import _grid_tier  # noqa: E402
from audit_core.uc03_deal_source_history import active_source_rows  # noqa: E402

with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    for row in journeys:
        jid = UUID(str(row["journey_id"]))
        params = {"t": T, "j": jid}
        print("\n" + "#" * 100)
        print("INSURANCE / TIER", row["journey_reference"])
        policy = ins._policy(c, tenant_id=T, journey_id=jid)
        if policy:
            policy = {k: v for k, v in policy.items() if k not in ("policy_reference",)}
        print("insurance record:", j(policy))
        print("insurance record source_kind etc:", j([dict(x) for x in c.execute(text("""
            SELECT insurance_source, source_kind, insurance_source_set_at_utc, insurance_source_set_by_actor_id,
                   insurance_by, premium_amount, updated_at_utc
            FROM auditcore.insurance_records WHERE tenant_id = :t AND journey_id = :j"""), params).mappings()]))
        print("resolve:", j(ins.resolve(c, tenant_id=T, journey_id=jid)))
        print("document_decision:", j(ins.document_decision(c, tenant_id=T, journey_id=jid)))
        print("ndc_says_own:", j(ins.ndc_says_own(c, tenant_id=T, journey_id=jid)))
        print("premium_on_dealer_statement:", ins.premium_on_dealer_statement(c, tenant_id=T, journey_id=jid))
        print("premium_on_dealer_invoice:", ins.premium_on_dealer_invoice(c, tenant_id=T, journey_id=jid))
        print("dealer_dump_policy:", j(ins.dealer_dump_policy(c, tenant_id=T, journey_id=jid)))
        print("grid tier now:", _grid_tier(c, tenant_id=T, journey_id=jid))
        print("commercial lines:", j([dict(x) for x in c.execute(text("""
            SELECT component_key, actual_amount, standard_amount FROM auditcore.commercial_lines
            WHERE tenant_id = :t AND journey_id = :j
              AND component_key IN ('insurance_amount', 'additional_warranty_amount')"""), params).mappings()]))
        for s in active_source_rows(c, tenant_id=T, journey_id=jid):
            if str(s["component_key"]) in ("insurance_amount", "additional_warranty_amount", "ADDITIONAL_DISCOUNT"):
                print("  source value:", j(dict(s)))
        print("discount application:", j([dict(x) for x in c.execute(text("""
            SELECT discount_key, standard_eligible_amount, actual_discount_amount, eligibility_result,
                   details, updated_at_utc
            FROM auditcore.discount_applications
            WHERE tenant_id = :t AND journey_id = :j AND discount_key = 'ADDITIONAL_DISCOUNT'"""), params).mappings()]))
        for document_type in sorted(ins._STATEMENT_TYPES):
            for document in load_ledger_documents(c, tenant_id=T, journey_id=jid, document_type=document_type):
                print("  statement copy:", document_type, document.get("document_id") or document.get("documentId"))
                for line in line_rows(document["fields"].get("line_items")):
                    print("    row:", j({k: line.get(k) for k in ("particulars_raw", "debit_amount", "credit_amount", "amount")}))
        for t in c.execute(text("""
            SELECT task_type, source_code, task_status, assigned_role_code, title, left(description, 400) AS description,
                   created_at_utc, updated_at_utc
            FROM auditcore.p2_tasks WHERE tenant_id = :t AND journey_id = :j
              AND (task_type ILIKE '%INSURANCE%' OR source_code ILIKE '%INSURANCE%' OR title ILIKE '%insurance%')
            ORDER BY created_at_utc"""), params).mappings():
            print("  insurance task:", j(dict(t)))
