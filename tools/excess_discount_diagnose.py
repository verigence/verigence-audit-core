"""Read-only: why two bookings' grid tier and insurance decision disagree. Nothing is written."""
from __future__ import annotations

import json
import os
from uuid import UUID

from sqlalchemy import create_engine, text

from audit_core import uc03_insurance_source as ins
from audit_core.db import set_tenant_context
from audit_core.uc03_account_statement import statement_reading
from audit_core.uc03_deal_reconciliation import _grid_tier
from audit_core.uc03_deal_source_history import active_source_rows

T = "afa9e32b-e62f-4cbc-be2b-7ab7ad5ef572"
engine = create_engine(os.environ["DATABASE_URL"], connect_args={"options": "-c statement_timeout=120000"})


def j(value):
    return json.dumps(value, default=str, sort_keys=True)


with engine.connect() as c:
    c.execute(text("SET TRANSACTION READ ONLY"))
    set_tenant_context(c, T)
    for ref in ("ADI-BM-261114-009", "UTK-PP-261023-009"):
        jid = UUID(str(c.execute(text("SELECT journey_id FROM auditcore.journeys WHERE tenant_id = :t AND journey_reference = :r"),
                                 {"t": T, "r": ref}).scalar_one()))
        params = {"t": T, "j": jid}
        print("\n####", ref)
        policy = ins._policy(c, tenant_id=T, journey_id=jid)
        print("policy:", j({k: v for k, v in (policy or {}).items() if k != "policy_reference"}))
        print("resolve:", j(ins.resolve(c, tenant_id=T, journey_id=jid)))
        print("document_verdict:", j(ins.document_verdict(c, tenant_id=T, journey_id=jid)))
        print("ndc_says_own:", ins.ndc_says_own(c, tenant_id=T, journey_id=jid),
              "| ndc_without_premium:", ins.ndc_without_premium(c, tenant_id=T, journey_id=jid),
              "| premium_on_statement:", ins.premium_on_dealer_statement(c, tenant_id=T, journey_id=jid),
              "| premium_on_invoice:", ins.premium_on_dealer_invoice(c, tenant_id=T, journey_id=jid))
        print("statement_reading:", j({k: v for k, v in statement_reading(c, tenant_id=T, journey_id=jid).items()
                                       if k in ("read", "onFile", "status")}))
        print("grid tier now:", _grid_tier(c, tenant_id=T, journey_id=jid))
        print("commercial lines:", j([dict(x) for x in c.execute(text("""
            SELECT component_key, actual_amount, actual_source_kind FROM auditcore.commercial_lines
            WHERE tenant_id = :t AND journey_id = :j
              AND component_key IN ('insurance_amount', 'additional_warranty_amount')"""), params).mappings()]))
        for s in active_source_rows(c, tenant_id=T, journey_id=jid):
            if str(s["component_key"]) in ("insurance_amount", "additional_warranty_amount"):
                print("  source value:", j({k: s[k] for k in ("component_key", "source_document_type", "amount")}))
        print("discount application:", j([dict(x) for x in c.execute(text("""
            SELECT standard_eligible_amount, actual_discount_amount, eligibility_result,
                   COALESCE(details->'reconciliation'->'grid', details->'grid') AS grid, updated_at_utc
            FROM auditcore.discount_applications
            WHERE tenant_id = :t AND journey_id = :j AND discount_key = 'ADDITIONAL_DISCOUNT'"""), params).mappings()]))
